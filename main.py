import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, or_, select, update
from database import init_db, AsyncSessionLocal, Account
from api import router as api_router
from proxy import router as proxy_router, fetch_account_credit, receive_daily_credit
from config import settings

# 配置全局日志
logging.basicConfig(
    level=settings.get_log_level(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# 后台任务引用
_unban_task = None
_reset_counts_task = None
_auto_register_task = None
_points_update_task = None

# 积分任务并发组件
CREDIT_EXECUTOR_MAX_WORKERS = 64
_credit_executor: ThreadPoolExecutor | None = None
_credit_update_lock = asyncio.Lock()
_daily_credit_lock = asyncio.Lock()

async def unban_accounts_task():
    """后台任务：每分钟检查并解禁到期账户"""
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # 查询需要解禁的账户
                now = datetime.now()
                query = select(Account).where(
                    and_(
                        Account.is_banned == True,
                        Account.ban_until <= now
                    )
                )
                result = await session.execute(query)
                accounts_to_unban = result.scalars().all()
                
                # 解禁账户
                for account in accounts_to_unban:
                    account.is_banned = False
                    account.ban_until = None
                
                if accounts_to_unban:
                    await session.commit()
                    print(f"[AutoUnban] 解禁了 {len(accounts_to_unban)} 个账户")
        except Exception as e:
            print(f"[AutoUnban] 错误: {e}")
        
        await asyncio.sleep(60)  # 每60秒执行一次

async def _auto_update_expired_sessions():
    """自动更新过期 session：查询超过指定天数的账户并批量调用 API 更新"""
    if not settings.REGISTER_API_URL or not settings.REGISTER_API_KEY:
        print("[SessionUpdate] Dreamina-register API 未配置，跳过 session 更新")
        return
    
    threshold_days = settings.SESSION_UPDATE_DAYS
    batch_size = settings.SESSION_UPDATE_BATCH_SIZE
    now = datetime.now()
    threshold_date = now - timedelta(days=threshold_days)
    
    # 查询需要更新的账户
    async with AsyncSessionLocal() as session:
        query = select(Account).where(
            (Account.session_id_updated_at == None) | 
            (Account.session_id_updated_at < threshold_date)
        )
        result = await session.execute(query)
        expired_accounts = result.scalars().all()
    
    if not expired_accounts:
        print(f"[SessionUpdate] 没有需要更新的账户（阈值: {threshold_days} 天）")
        return
    
    print(f"[SessionUpdate] 发现 {len(expired_accounts)} 个账户需要更新 session")
    
    base_url = settings.REGISTER_API_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.REGISTER_API_KEY}"}
    
    success_count = 0
    fail_count = 0
    
    # 按批次处理
    for i in range(0, len(expired_accounts), batch_size):
        batch = expired_accounts[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(expired_accounts) + batch_size - 1) // batch_size
        print(f"[SessionUpdate] 处理批次 {batch_num}/{total_batches}...")
        
        for account in batch:
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        f"{base_url}/session/update",
                        json={"email": account.email, "password": account.password},
                        headers=headers
                    )
                    resp.raise_for_status()
                    result_data = resp.json()
                
                new_session_id = result_data.get("session_id")
                if new_session_id:
                    # 提取 region 和清理 session_id 前缀
                    new_region = _get_region_from_session_id(new_session_id)
                    clean_session_id = _strip_region_prefix(new_session_id)
                    
                    # 更新数据库
                    async with AsyncSessionLocal() as db_session:
                        db_account = await db_session.get(Account, account.id)
                        if db_account:
                            db_account.session_id = clean_session_id
                            db_account.session_id_updated_at = datetime.now()
                            db_account.region = new_region
                            await db_session.commit()
                    
                    success_count += 1
                    print(f"[SessionUpdate] ✓ {account.email} session 更新成功")
                else:
                    fail_count += 1
                    print(f"[SessionUpdate] ✗ {account.email} 无返回 session_id")
            except Exception as e:
                fail_count += 1
                print(f"[SessionUpdate] ✗ {account.email} 更新失败: {e}")
        
        # 批次之间短暂延迟，避免请求过于密集
        if i + batch_size < len(expired_accounts):
            await asyncio.sleep(2)
    
    print(f"[SessionUpdate] 更新完成: 成功 {success_count}, 失败 {fail_count}")

def _get_region_from_session_id(session_id: str) -> str:
    """从 session_id 前缀提取 region"""
    if session_id.startswith("us-"):
        return "us"
    elif session_id.startswith("hk-"):
        return "hk"
    elif session_id.startswith("jp-"):
        return "jp"
    elif session_id.startswith("sg-"):
        return "sg"
    return "us"

def _strip_region_prefix(session_id: str) -> str:
    """移除 session_id 的 region 前缀"""
    for prefix in ["us-", "hk-", "jp-", "sg-"]:
        if session_id.startswith(prefix):
            return session_id[len(prefix):]
    return session_id

def _get_credit_task_runtime_config() -> dict[str, int]:
    """读取积分任务配置快照，避免运行中配置争用。"""
    thread_count = max(1, int(getattr(settings, "CREDIT_TASK_THREADS", 5) or 5))
    thread_count = min(thread_count, CREDIT_EXECUTOR_MAX_WORKERS)
    commit_batch_size = max(1, int(getattr(settings, "CREDIT_DB_COMMIT_BATCH_SIZE", 20) or 20))
    commit_batch_size = min(commit_batch_size, 500)
    return {
        "threads": thread_count,
        "commit_batch_size": commit_batch_size,
    }


def _ensure_credit_executor() -> ThreadPoolExecutor:
    """按需创建积分任务线程池。"""
    global _credit_executor
    if _credit_executor is None:
        _credit_executor = ThreadPoolExecutor(
            max_workers=CREDIT_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="credit-worker",
        )
    return _credit_executor


async def _shutdown_credit_executor() -> None:
    """关闭线程池，避免线程泄漏。"""
    global _credit_executor
    if _credit_executor is None:
        return

    executor = _credit_executor
    _credit_executor = None

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, executor.shutdown, True)


async def _run_fetch_credit_in_thread(
    session_id: str,
    region: str,
    executor: ThreadPoolExecutor,
) -> float | None:
    """在线程中执行积分查询（仅网络调用）。"""
    loop = asyncio.get_running_loop()

    def _worker() -> float | None:
        return asyncio.run(fetch_account_credit(session_id, region))

    return await loop.run_in_executor(executor, _worker)


async def _run_receive_daily_credit_and_fetch_in_thread(
    session_id: str,
    region: str,
    executor: ThreadPoolExecutor,
) -> tuple[int | None, float | None]:
    """在线程中执行领取与积分查询（仅网络调用）。"""
    loop = asyncio.get_running_loop()

    def _worker() -> tuple[int | None, float | None]:
        quota = asyncio.run(receive_daily_credit(session_id, region))
        if quota is not None and quota > 0:
            credit = asyncio.run(fetch_account_credit(session_id, region))
            return quota, credit
        return quota, None

    return await loop.run_in_executor(executor, _worker)


async def _iter_completed_tasks(tasks: list[asyncio.Task[Any]]) -> AsyncIterator[Any]:
    """按完成顺序遍历任务结果。"""
    for finished in asyncio.as_completed(tasks):
        yield await finished


async def _flush_credit_updates(updates: list[dict[str, Any]], mode: str) -> tuple[int, int]:
    """按批次落库积分更新。"""
    if not updates:
        return 0, 0

    pending_success = 0
    pending_fail = 0
    logs: list[str] = []

    async with AsyncSessionLocal() as db_session:
        try:
            for item in updates:
                stmt = (
                    update(Account)
                    .where(Account.id == item["id"])
                    .values(points=item["points"])
                )
                await db_session.execute(stmt)

                email = item.get("email", "<unknown>")
                if item.get("ok"):
                    pending_success += 1
                    logs.append(f"[{mode}] ✓ {email} 积分更新: {item['points']}")
                else:
                    pending_fail += 1
                    logs.append(f"[{mode}] ✗ {email} {item.get('reason', '积分查询失败')}，积分设为 0")

            await db_session.commit()
            for line in logs:
                print(line)
            return pending_success, pending_fail
        except Exception as e:
            await db_session.rollback()
            print(f"[{mode}] 批次提交失败（{len(updates)} 条）: {e}")
            return 0, len(updates)


async def _flush_daily_credit_updates(updates: list[dict[str, Any]], mode: str) -> tuple[int, int, int]:
    """按批次落库每日积分更新。"""
    if not updates:
        return 0, 0, 0

    pending_success = 0
    pending_fail = 0
    pending_received = 0
    logs: list[str] = []

    async with AsyncSessionLocal() as db_session:
        try:
            for item in updates:
                email = item.get("email", "<unknown>")
                quota = item.get("quota")
                if item.get("status") == "received":
                    pending_success += 1
                    pending_received += int(quota or 0)
                    if item.get("credit") is not None:
                        stmt = (
                            update(Account)
                            .where(Account.id == item["id"])
                            .values(points=item["credit"])
                        )
                        await db_session.execute(stmt)
                        logs.append(f"[{mode}] ✓ {email} 领取积分: {quota}，积分更新: {item['credit']}")
                    else:
                        logs.append(f"[{mode}] ✓ {email} 领取积分: {quota}，积分查询失败")
                elif item.get("status") == "already_received":
                    logs.append(f"[{mode}] ○ {email} 今日已领取")
                else:
                    pending_fail += 1
                    if item.get("set_zero"):
                        stmt = (
                            update(Account)
                            .where(Account.id == item["id"])
                            .values(points=0)
                        )
                        await db_session.execute(stmt)
                    logs.append(f"[{mode}] ✗ {email} {item.get('reason', '领取失败')}")

            await db_session.commit()
            for line in logs:
                print(line)
            return pending_success, pending_fail, pending_received
        except Exception as e:
            await db_session.rollback()
            print(f"[{mode}] 批次提交失败（{len(updates)} 条）: {e}")
            return 0, len(updates), 0


async def _run_with_lock_or_skip(
    lock: asyncio.Lock,
    tag: str,
    coro_factory: Callable[[], Awaitable[None]],
) -> None:
    """防重入执行器：若任务已在运行，则跳过本轮。"""
    if lock.locked():
        print(f"[{tag}] 任务已在运行中，跳过本次触发")
        return

    async with lock:
        await coro_factory()


async def _run_credit_update_impl() -> None:
    """执行积分更新（并发网络 + 单协程分批落库）。"""
    print("[CreditUpdate] 开始批量更新账户积分...")

    async with AsyncSessionLocal() as session:
        query = select(Account).where(
            and_(
                Account.region != "cn",
                Account.session_id.isnot(None)
            )
        )
        result = await session.execute(query)
        accounts = result.scalars().all()

    if not accounts:
        print("[CreditUpdate] 没有需要更新积分的账户")
        return

    runtime = _get_credit_task_runtime_config()
    executor = _ensure_credit_executor()
    semaphore = asyncio.Semaphore(runtime["threads"])

    print(
        f"[CreditUpdate] 发现 {len(accounts)} 个非 CN 区域账户需要更新积分，"
        f"线程数={runtime['threads']}，批量提交={runtime['commit_batch_size']}"
    )

    async def _run_account(account: Account) -> dict[str, Any]:
        async with semaphore:
            try:
                credit = await _run_fetch_credit_in_thread(account.session_id, account.region, executor)
                if credit is None:
                    return {
                        "id": account.id,
                        "email": account.email,
                        "ok": False,
                        "points": 0,
                        "reason": "查询失败",
                    }
                return {
                    "id": account.id,
                    "email": account.email,
                    "ok": True,
                    "points": float(credit),
                }
            except Exception as e:
                return {
                    "id": account.id,
                    "email": account.email,
                    "ok": False,
                    "points": 0,
                    "reason": f"积分查询异常: {e}",
                }

    tasks = [asyncio.create_task(_run_account(account)) for account in accounts]
    pending_updates: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    async for item in _iter_completed_tasks(tasks):
        pending_updates.append(item)
        if len(pending_updates) >= runtime["commit_batch_size"]:
            s, f = await _flush_credit_updates(pending_updates, "CreditUpdate")
            success_count += s
            fail_count += f
            pending_updates.clear()

    if pending_updates:
        s, f = await _flush_credit_updates(pending_updates, "CreditUpdate")
        success_count += s
        fail_count += f

    print(f"[CreditUpdate] 批量更新完成: 成功 {success_count}, 失败 {fail_count}")


async def _run_daily_credit_impl() -> None:
    """执行每日领取（并发网络 + 单协程分批落库）。"""
    print("[DailyCredit] 开始批量领取今日积分...")

    async with AsyncSessionLocal() as session:
        query = select(Account).where(
            and_(
                Account.region != "cn",
                Account.session_id.isnot(None)
            )
        )
        result = await session.execute(query)
        accounts = result.scalars().all()

    if not accounts:
        print("[DailyCredit] 没有需要领取积分的账户")
        return

    runtime = _get_credit_task_runtime_config()
    executor = _ensure_credit_executor()
    semaphore = asyncio.Semaphore(runtime["threads"])

    print(
        f"[DailyCredit] 发现 {len(accounts)} 个非 CN 区域账户需要领取积分，"
        f"线程数={runtime['threads']}，批量提交={runtime['commit_batch_size']}"
    )

    async def _run_account(account: Account) -> dict[str, Any]:
        async with semaphore:
            try:
                quota, credit = await _run_receive_daily_credit_and_fetch_in_thread(
                    account.session_id,
                    account.region,
                    executor,
                )
                if quota is not None and quota > 0:
                    return {
                        "id": account.id,
                        "email": account.email,
                        "status": "received",
                        "quota": int(quota),
                        "credit": float(credit) if credit is not None else None,
                    }
                if quota == 0:
                    return {
                        "id": account.id,
                        "email": account.email,
                        "status": "already_received",
                        "quota": 0,
                    }
                return {
                    "id": account.id,
                    "email": account.email,
                    "status": "failed",
                    "reason": "领取失败",
                    "set_zero": False,
                }
            except Exception as e:
                return {
                    "id": account.id,
                    "email": account.email,
                    "status": "failed",
                    "reason": f"领取异常: {e}",
                    "set_zero": False,
                }

    tasks = [asyncio.create_task(_run_account(account)) for account in accounts]
    pending_updates: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0
    total_received = 0

    async for item in _iter_completed_tasks(tasks):
        pending_updates.append(item)
        if len(pending_updates) >= runtime["commit_batch_size"]:
            s, f, r = await _flush_daily_credit_updates(pending_updates, "DailyCredit")
            success_count += s
            fail_count += f
            total_received += r
            pending_updates.clear()

    if pending_updates:
        s, f, r = await _flush_daily_credit_updates(pending_updates, "DailyCredit")
        success_count += s
        fail_count += f
        total_received += r

    print(
        f"[DailyCredit] 批量领取完成: 成功 {success_count}, 失败 {fail_count}, 共领取 {total_received} 积分"
    )


async def _update_all_accounts_credit():
    """批量更新所有非 CN 区域账户的积分（防重入）。"""

    async def _impl() -> None:
        await _run_credit_update_impl()

    await _run_with_lock_or_skip(_credit_update_lock, "CreditUpdate", _impl)


async def _receive_all_accounts_credit():
    """批量为所有非 CN 区域账户领取今日积分（防重入）。"""

    async def _impl() -> None:
        await _run_daily_credit_impl()

    await _run_with_lock_or_skip(_daily_credit_lock, "DailyCredit", _impl)


async def _reset_all_accounts_credit():
    """将所有账户的积分重置为 0（仅更新非 0 记录）"""
    print("[CreditReset] 开始重置所有账户积分为 0...")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(Account)
            .where(Account.points != 0)
            .values(points=0)
        )
        await session.commit()

    updated_rows = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else None
    if updated_rows is None:
        print("[CreditReset] 账户积分重置完成")
    elif updated_rows == 0:
        print("[CreditReset] 没有账户需要重置积分")
    else:
        print(f"[CreditReset] 已重置 {updated_rows} 个账户的积分为 0")


async def reset_usage_counts_task():
    """后台任务：在设定时间重置所有账户的使用次数"""
    last_reset_date = None

    while True:
        try:
            now = datetime.now()
            reset_time = settings.RESET_COUNTS_TIME

            # 校验 RESET_COUNTS_TIME，要求 HH:MM 且数值范围合法
            time_parts = reset_time.split(":")
            if (
                len(time_parts) != 2
                or len(time_parts[0]) != 2
                or len(time_parts[1]) != 2
                or not time_parts[0].isdigit()
                or not time_parts[1].isdigit()
            ):
                print(f"[ResetCounts] RESET_COUNTS_TIME 配置无效: {reset_time}（应为 HH:MM）")
                await asyncio.sleep(30)
                continue

            reset_hour, reset_minute = map(int, time_parts)
            if not (0 <= reset_hour <= 23 and 0 <= reset_minute <= 59):
                print(f"[ResetCounts] RESET_COUNTS_TIME 配置超出范围: {reset_time}（小时 00-23，分钟 00-59）")
                await asyncio.sleep(30)
                continue

            # 检查是否到达重置时间且今天还未重置
            if (
                now.hour == reset_hour
                and now.minute == reset_minute
                and last_reset_date != now.date()
            ):
                # 动态获取所有以 _count 结尾的字段（排除 error_count）
                count_fields = [
                    col.name for col in Account.__table__.columns
                    if col.name.endswith("_count") and col.name != "error_count"
                ]

                if count_fields:
                    reset_values = {field: 0 for field in count_fields}
                    non_zero_conditions = [getattr(Account, field) != 0 for field in count_fields]

                    async with AsyncSessionLocal() as session:
                        result = await session.execute(
                            update(Account)
                            .where(or_(*non_zero_conditions))
                            .values(**reset_values)
                        )
                        await session.commit()

                    reset_rows = (
                        result.rowcount
                        if result.rowcount is not None and result.rowcount >= 0
                        else None
                    )
                    if reset_rows is None:
                        print(f"[ResetCounts] 已执行计数字段重置，字段数 {len(count_fields)}")
                    elif reset_rows == 0:
                        print(f"[ResetCounts] 没有账户需要重置计数字段（字段数 {len(count_fields)}）")
                    else:
                        print(f"[ResetCounts] 已重置 {reset_rows} 个账户的 {len(count_fields)} 个计数字段")
                else:
                    print("[ResetCounts] 未发现可重置的计数字段，跳过使用次数重置")

                last_reset_date = now.date()

                # Session 自动更新：查询过期账户并批量更新
                await _auto_update_expired_sessions()

                # 先将所有账户积分重置为 0
                await _reset_all_accounts_credit()

                # 批量领取今日积分（CN 区域跳过）
                await _receive_all_accounts_credit()

                # # 批量更新所有账户积分（CN 区域跳过）
                # await _update_all_accounts_credit()
        except Exception as e:
            print(f"[ResetCounts] 错误: {e}")

        await asyncio.sleep(30)  # 每30秒检查一次

async def auto_register_task():
    """后台任务：按配置间隔自动注册新账户"""
    from config import Settings
    
    while True:
        try:
            # 重新加载配置以获取最新设置
            current_settings = Settings.load_config()
            
            if not current_settings.AUTO_REGISTER_ENABLED:
                # 自动注册未开启，等待后重新检查
                await asyncio.sleep(60)
                continue
            
            if not current_settings.REGISTER_API_URL or not current_settings.REGISTER_API_KEY:
                print("[AutoRegister] Dreamina-register API 未配置，跳过自动注册")
                await asyncio.sleep(current_settings.AUTO_REGISTER_INTERVAL)
                continue
            
            print("[AutoRegister] 开始自动注册新账户...")
            
            base_url = current_settings.REGISTER_API_URL.rstrip("/")
            headers = {"Authorization": f"Bearer {current_settings.REGISTER_API_KEY}"}
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: 创建注册任务
                try:
                    resp = await client.post(
                        f"{base_url}/register",
                        json={"mail_type": current_settings.REGISTER_MAIL_TYPE},
                        headers=headers
                    )
                    resp.raise_for_status()
                    task_data = resp.json()
                    task_id = task_data.get("task_id")
                except Exception as e:
                    print(f"[AutoRegister] 创建注册任务失败: {e}")
                    await asyncio.sleep(current_settings.AUTO_REGISTER_INTERVAL)
                    continue
                
                if not task_id:
                    print("[AutoRegister] 未获取到 task_id")
                    await asyncio.sleep(current_settings.AUTO_REGISTER_INTERVAL)
                    continue
                
                print(f"[AutoRegister] 注册任务已创建: {task_id}")
                
                # Step 2: 轮询任务状态
                max_attempts = 60
                poll_interval = 10
                
                for attempt in range(max_attempts):
                    try:
                        resp = await client.get(
                            f"{base_url}/tasks/{task_id}",
                            headers=headers
                        )
                        resp.raise_for_status()
                        task_status = resp.json()
                    except Exception as e:
                        print(f"[AutoRegister] 轮询任务状态失败: {e}")
                        break
                    
                    status = task_status.get("status")
                    
                    if status == "completed":
                        result = task_status.get("result", {})
                        email = result.get("email")
                        password = result.get("password")
                        session_id = result.get("session_id")
                        
                        if not email or not password or not session_id:
                            print("[AutoRegister] 注册结果不完整")
                            break
                        
                        # 提取 region 并清理 session_id
                        region = _get_region_from_session_id(session_id)
                        clean_session_id = _strip_region_prefix(session_id)
                        
                        # 保存到数据库
                        async with AsyncSessionLocal() as db_session:
                            from sqlalchemy import select
                            existing = await db_session.execute(
                                select(Account).where(Account.email == email)
                            )
                            if existing.scalar_one_or_none():
                                print(f"[AutoRegister] 邮箱已存在: {email}")
                                break
                            
                            new_account = Account(
                                email=email,
                                password=password,
                                session_id=clean_session_id,
                                region=region,
                                points=current_settings.DEFAULT_POINTS,
                                session_id_updated_at=datetime.now()
                            )
                            db_session.add(new_account)
                            await db_session.commit()
                            await db_session.refresh(new_account)
                            account_id = new_account.id
                        
                        print(f"[AutoRegister] ✓ 账户注册成功: {email}")
                        
                        # 为新注册的账户领取今日积分并更新积分数值
                        if region.lower() != "cn":
                            try:
                                # 领取今日积分
                                quota = await receive_daily_credit(clean_session_id, region)
                                if quota is not None and quota > 0:
                                    print(f"[AutoRegister] ✓ {email} 领取积分: {quota}")
                                elif quota == 0:
                                    print(f"[AutoRegister] ○ {email} 今日已领取")
                                else:
                                    print(f"[AutoRegister] ✗ {email} 领取积分失败")
                                
                                # 查询并更新最新积分
                                credit = await fetch_account_credit(clean_session_id, region)
                                if credit is not None:
                                    async with AsyncSessionLocal() as update_session:
                                        db_account = await update_session.get(Account, account_id)
                                        if db_account:
                                            db_account.points = credit
                                            await update_session.commit()
                                    print(f"[AutoRegister] ✓ {email} 积分更新: {credit}")
                            except Exception as e:
                                print(f"[AutoRegister] ✗ {email} 积分操作异常: {e}")
                        break
                    
                    elif status == "failed":
                        error_msg = task_status.get("error", "未知错误")
                        print(f"[AutoRegister] ✗ 注册失败: {error_msg}")
                        break
                    
                    # 仍在处理，等待后继续轮询
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(poll_interval)
                else:
                    print("[AutoRegister] 注册超时")
            
            # 等待配置的间隔时间
            await asyncio.sleep(current_settings.AUTO_REGISTER_INTERVAL)
            
        except Exception as e:
            print(f"[AutoRegister] 错误: {e}")
            await asyncio.sleep(60)

async def points_update_task():
    """后台任务：按配置间隔定时更新所有账户积分"""
    from config import Settings
    
    while True:
        try:
            # 重新加载配置以获取最新设置
            current_settings = Settings.load_config()
            
            if not current_settings.POINTS_UPDATE_ENABLED:
                # 定时积分更新未开启，等待后重新检查
                await asyncio.sleep(60)
                continue
            
            print(f"[PointsUpdate] 定时积分更新任务开始，间隔: {current_settings.POINTS_UPDATE_INTERVAL} 秒")
            
            # 执行积分更新
            await _update_all_accounts_credit()
            
            # 等待配置的间隔时间
            await asyncio.sleep(current_settings.POINTS_UPDATE_INTERVAL)
            
        except Exception as e:
            print(f"[PointsUpdate] 错误: {e}")
            await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unban_task, _reset_counts_task, _auto_register_task, _points_update_task
    # Startup
    await init_db()
    _ensure_credit_executor()
    _unban_task = asyncio.create_task(unban_accounts_task())
    _reset_counts_task = asyncio.create_task(reset_usage_counts_task())
    _auto_register_task = asyncio.create_task(auto_register_task())
    _points_update_task = asyncio.create_task(points_update_task())
    print("[AutoUnban] 后台任务已启动")
    print(f"[ResetCounts] 后台任务已启动，重置时间: {settings.RESET_COUNTS_TIME}")
    print("[AutoRegister] 后台任务已启动")
    print("[PointsUpdate] 后台任务已启动")
    yield
    # Shutdown
    if _unban_task:
        _unban_task.cancel()
        try:
            await _unban_task
        except asyncio.CancelledError:
            pass
        print("[AutoUnban] 后台任务已停止")
    if _reset_counts_task:
        _reset_counts_task.cancel()
        try:
            await _reset_counts_task
        except asyncio.CancelledError:
            pass
        print("[ResetCounts] 后台任务已停止")
    if _auto_register_task:
        _auto_register_task.cancel()
        try:
            await _auto_register_task
        except asyncio.CancelledError:
            pass
        print("[AutoRegister] 后台任务已停止")
    if _points_update_task:
        _points_update_task.cancel()
        try:
            await _points_update_task
        except asyncio.CancelledError:
            pass
        print("[PointsUpdate] 后台任务已停止")
    await _shutdown_credit_executor()
    print("[CreditExecutor] 线程池已停止")

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Routers
app.include_router(api_router)
app.include_router(proxy_router)

# Serve Static Files (Frontend)
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)
