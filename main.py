import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, and_
from database import init_db, AsyncSessionLocal, Account
from api import router as api_router
from proxy import router as proxy_router
from config import settings

# 后台任务引用
_unban_task = None
_reset_counts_task = None

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
        except Exception as e:
            print(f"[ResetCounts] 错误: {e}")
        
        await asyncio.sleep(30)  # 每30秒检查一次

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _unban_task, _reset_counts_task
    # Startup
    await init_db()
    _unban_task = asyncio.create_task(unban_accounts_task())
    _reset_counts_task = asyncio.create_task(reset_usage_counts_task())
    print("[AutoUnban] 后台任务已启动")
    print(f"[ResetCounts] 后台任务已启动，重置时间: {settings.RESET_COUNTS_TIME}")
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
