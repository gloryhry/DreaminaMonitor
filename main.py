import asyncio
import logging
from datetime import datetime, timedelta
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, and_
from database import init_db, AsyncSessionLocal, Account
from api import router as api_router
from proxy import router as proxy_router
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
    elif session_id.startswith("eu-"):
        return "eu"
    elif session_id.startswith("asia-"):
        return "asia"
    return "us"

def _strip_region_prefix(session_id: str) -> str:
    """移除 session_id 的 region 前缀"""
    for prefix in ["us-", "eu-", "asia-"]:
        if session_id.startswith(prefix):
            return session_id[len(prefix):]
    return session_id

async def reset_usage_counts_task():
    """后台任务：在设定时间重置所有账户的使用次数"""
    last_reset_date = None
    while True:
        try:
            now = datetime.now()
            reset_time = settings.RESET_COUNTS_TIME
            reset_hour, reset_minute = map(int, reset_time.split(":"))
            
            # 检查是否到达重置时间且今天还未重置
            if (now.hour == reset_hour and 
                now.minute == reset_minute and 
                last_reset_date != now.date()):
                
                async with AsyncSessionLocal() as session:
                    result = await session.execute(select(Account))
                    accounts = result.scalars().all()
                    
                    # 动态获取所有以 _count 结尾的字段（排除 error_count）
                    count_fields = [
                        col.name for col in Account.__table__.columns 
                        if col.name.endswith('_count') and col.name != 'error_count'
                    ]
                    
                    for account in accounts:
                        for field in count_fields:
                            setattr(account, field, 0)
                    
                    await session.commit()
                    last_reset_date = now.date()
                    print(f"[ResetCounts] 已重置 {len(accounts)} 个账户的 {len(count_fields)} 个计数字段")
                
                # Session 自动更新：查询过期账户并批量更新
                await _auto_update_expired_sessions()
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
                        
                        print(f"[AutoRegister] ✓ 账户注册成功: {email}")
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unban_task, _reset_counts_task, _auto_register_task
    # Startup
    await init_db()
    _unban_task = asyncio.create_task(unban_accounts_task())
    _reset_counts_task = asyncio.create_task(reset_usage_counts_task())
    _auto_register_task = asyncio.create_task(auto_register_task())
    print("[AutoUnban] 后台任务已启动")
    print(f"[ResetCounts] 后台任务已启动，重置时间: {settings.RESET_COUNTS_TIME}")
    print("[AutoRegister] 后台任务已启动")
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
