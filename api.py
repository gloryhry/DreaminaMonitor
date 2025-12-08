from typing import List, Optional
from datetime import datetime, timedelta
import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from pydantic import BaseModel, Field

from database import get_db, Account
from config import settings

router = APIRouter(prefix="/api")

# Pydantic Models
class AccountCreate(BaseModel):
    email: str
    password: str
    region: str = "us"
    session_id: Optional[str] = None
    points: float = 0.0

class AccountUpdate(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    region: Optional[str] = None
    session_id: Optional[str] = None
    points: Optional[float] = None
    is_banned: Optional[bool] = None
    ban_until: Optional[datetime] = None

class BulkAccountCreate(BaseModel):
    credentials: str  # 每行 email:password 格式
    session_ids: str  # 每行一个 session_id，与 credentials 行数对应
    region: str = "us"
    points: float = 0.0

class AccountResponse(BaseModel):
    id: int
    email: str
    # password: str # Do not return full password
    region: str
    session_id: Optional[str]
    points: float
    session_id_updated_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    jimeng_4_0_count: int
    jimeng_4_1_count: int
    nanobanana_count: int
    nanobananapro_count: int
    video_3_0_count: int
    error_count: int
    is_banned: bool
    ban_until: Optional[datetime]
    
    class Config:
        from_attributes = True

class PaginatedAccounts(BaseModel):
    total: int
    page: int
    size: int
    items: List[AccountResponse]

class SettingsUpdate(BaseModel):
    ADMIN_PASSWORD: Optional[str] = None
    DATABASE_URL: Optional[str] = None
    UPSTREAM_BASE_URL: Optional[str] = None
    PORT: Optional[int] = None
    HOST: Optional[str] = None
    PROXY_TIMEOUT: Optional[int] = None
    LIMIT_JIMENG_4_0: Optional[int] = None
    LIMIT_JIMENG_4_1: Optional[int] = None
    LIMIT_NANOBANANA: Optional[int] = None
    LIMIT_NANOBANANAPRO: Optional[int] = None
    LIMIT_VIDEO_3_0: Optional[int] = None
    # Dreamina-register API settings
    REGISTER_API_URL: Optional[str] = None
    REGISTER_API_KEY: Optional[str] = None
    REGISTER_MAIL_TYPE: Optional[str] = None
    DEFAULT_POINTS: Optional[float] = None
    RESET_COUNTS_TIME: Optional[str] = None
    # Session auto-update settings
    SESSION_UPDATE_DAYS: Optional[int] = None
    SESSION_UPDATE_BATCH_SIZE: Optional[int] = None
    # Auto-register settings
    AUTO_REGISTER_ENABLED: Optional[bool] = None
    AUTO_REGISTER_INTERVAL: Optional[int] = None
    # Error handling settings
    ACCOUNT_BAN_DURATION_HOURS: Optional[float] = None
    # Logging settings
    LOG_LEVEL: Optional[str] = None

# Account Endpoints
@router.get("/accounts", response_model=PaginatedAccounts)
async def get_accounts(
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=1000),
    region: Optional[str] = None,
    email: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    query = select(Account)
    
    if region:
        query = query.where(Account.region == region)
    if email:
        query = query.where(Account.email.contains(email))
    
    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query)
    
    # Pagination
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    accounts = result.scalars().all()
    
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": accounts
    }

@router.post("/accounts", response_model=AccountResponse)
async def create_account(account: AccountCreate, db: AsyncSession = Depends(get_db)):
    # Check if email exists
    existing = await db.execute(select(Account).where(Account.email == account.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    new_account = Account(**account.model_dump())
    if new_account.session_id:
        new_account.session_id_updated_at = datetime.now()
        
    db.add(new_account)
    await db.commit()
    await db.refresh(new_account)
    return new_account

@router.put("/accounts/{id}", response_model=AccountResponse)
async def update_account(id: int, account: AccountUpdate, db: AsyncSession = Depends(get_db)):
    db_account = await db.get(Account, id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    update_data = account.model_dump(exclude_unset=True)
    
    if "session_id" in update_data and update_data["session_id"] != db_account.session_id:
        update_data["session_id_updated_at"] = datetime.now()
        
    for key, value in update_data.items():
        setattr(db_account, key, value)
        
    await db.commit()
    await db.refresh(db_account)
    return db_account

@router.delete("/accounts/{id}")
async def delete_account(id: int, db: AsyncSession = Depends(get_db)):
    db_account = await db.get(Account, id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    await db.delete(db_account)
    await db.commit()
    return {"message": "Account deleted"}

@router.post("/accounts/bulk")
async def bulk_create_accounts(data: BulkAccountCreate, db: AsyncSession = Depends(get_db)):
    """
    批量创建账户。
    - credentials: 每行 email:password 格式
    - session_ids: 每行一个 session_id，与 credentials 行数对应
    - 自动跳过重复的 email（输入内重复或已存在于数据库）
    """
    # 解析 credentials 和 session_ids
    cred_lines = [line.strip() for line in data.credentials.strip().split('\n') if line.strip()]
    session_lines = [line.strip() for line in data.session_ids.strip().split('\n') if line.strip()]
    
    if len(cred_lines) != len(session_lines):
        raise HTTPException(
            status_code=400, 
            detail=f"credentials 行数 ({len(cred_lines)}) 与 session_ids 行数 ({len(session_lines)}) 不匹配"
        )
    
    # 解析 email:password
    parsed_accounts = []
    parse_errors = []
    for i, line in enumerate(cred_lines, 1):
        if ':' not in line:
            parse_errors.append(f"第 {i} 行格式错误: '{line}' (应为 email:password)")
            continue
        parts = line.split(':', 1)
        email = parts[0].strip()
        password = parts[1].strip()
        if not email or not password:
            parse_errors.append(f"第 {i} 行 email 或 password 为空")
            continue
        parsed_accounts.append({
            "email": email,
            "password": password,
            "session_id": session_lines[i-1]
        })
    
    if parse_errors:
        raise HTTPException(status_code=400, detail={"parse_errors": parse_errors})
    
    # 检查输入内重复的 email
    seen_emails = set()
    duplicate_in_input = []
    unique_accounts = []
    for acc in parsed_accounts:
        if acc["email"] in seen_emails:
            duplicate_in_input.append(acc["email"])
        else:
            seen_emails.add(acc["email"])
            unique_accounts.append(acc)
    
    # 查询数据库中已存在的 email
    if unique_accounts:
        emails_to_check = [acc["email"] for acc in unique_accounts]
        result = await db.execute(
            select(Account.email).where(Account.email.in_(emails_to_check))
        )
        existing_emails = set(result.scalars().all())
    else:
        existing_emails = set()
    
    # 过滤掉已存在的 email
    duplicate_in_db = []
    accounts_to_create = []
    for acc in unique_accounts:
        if acc["email"] in existing_emails:
            duplicate_in_db.append(acc["email"])
        else:
            accounts_to_create.append(acc)
    
    # 批量创建账户
    created_count = 0
    for acc in accounts_to_create:
        new_account = Account(
            email=acc["email"],
            password=acc["password"],
            session_id=acc["session_id"],
            region=data.region,
            points=data.points,
            session_id_updated_at=datetime.now()
        )
        db.add(new_account)
        created_count += 1
    
    if created_count > 0:
        await db.commit()
    
    return {
        "message": f"成功创建 {created_count} 个账户",
        "created_count": created_count,
        "skipped": {
            "duplicate_in_input": duplicate_in_input,
            "duplicate_in_db": duplicate_in_db
        },
        "total_skipped": len(duplicate_in_input) + len(duplicate_in_db)
    }

@router.post("/accounts/{id}/ban")
async def ban_account(id: int, duration_hours: float = 12, db: AsyncSession = Depends(get_db)):
    db_account = await db.get(Account, id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    db_account.is_banned = True
    db_account.ban_until = datetime.now() + timedelta(hours=duration_hours)
    await db.commit()
    return {"message": f"Account banned for {duration_hours} hours"}

@router.post("/accounts/{id}/unban")
async def unban_account(id: int, db: AsyncSession = Depends(get_db)):
    db_account = await db.get(Account, id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    db_account.is_banned = False
    db_account.ban_until = None
    await db.commit()
    return {"message": "Account unbanned"}

# Dreamina-register API Endpoints
def _get_region_from_session_id(session_id: str) -> str:
    """Extract region from session_id prefix (e.g., 'us-xxx' -> 'us')"""
    if session_id.startswith("us-"):
        return "us"
    elif session_id.startswith("hk-"):
        return "hk"
    elif session_id.startswith("jp-"):
        return "jp"
    elif session_id.startswith("sg-"):
        return "sg"
    else:
        # Default to 'us' if no recognized prefix
        return "us"

def _strip_region_prefix(session_id: str) -> str:
    """Remove region prefix from session_id (e.g., 'us-abc123' -> 'abc123')"""
    for prefix in ["us-", "hk-", "jp-", "sg-"]:
        if session_id.startswith(prefix):
            return session_id[len(prefix):]
    return session_id

@router.post("/register-account")
async def register_account(db: AsyncSession = Depends(get_db)):
    """
    Register a new account via Dreamina-register API.
    Polls the task status every 10 seconds until completed or failed (max 60 attempts).
    """
    if not settings.REGISTER_API_URL or not settings.REGISTER_API_KEY:
        raise HTTPException(status_code=400, detail="Dreamina-register API not configured")
    
    base_url = settings.REGISTER_API_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.REGISTER_API_KEY}"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Create registration task
        try:
            resp = await client.post(
                f"{base_url}/register",
                json={"mail_type": settings.REGISTER_MAIL_TYPE},
                headers=headers
            )
            resp.raise_for_status()
            task_data = resp.json()
            task_id = task_data.get("task_id")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Register API error: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create registration task: {str(e)}")
        
        if not task_id:
            raise HTTPException(status_code=500, detail="No task_id returned from register API")
        
        # Step 2: Poll task status every 10 seconds (max 60 attempts = 10 minutes)
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
                raise HTTPException(status_code=500, detail=f"Failed to poll task status: {str(e)}")
            
            status = task_status.get("status")
            
            if status == "completed":
                result = task_status.get("result", {})
                email = result.get("email")
                password = result.get("password")
                session_id = result.get("session_id")
                
                if not email or not password or not session_id:
                    raise HTTPException(status_code=500, detail="Incomplete registration result")
                
                # Determine region from session_id prefix
                region = _get_region_from_session_id(session_id)
                # Strip region prefix before storing in database
                clean_session_id = _strip_region_prefix(session_id)
                
                # Check if email already exists
                existing = await db.execute(select(Account).where(Account.email == email))
                if existing.scalar_one_or_none():
                    raise HTTPException(status_code=400, detail="Email already registered in database")
                
                # Create account in database
                new_account = Account(
                    email=email,
                    password=password,
                    session_id=clean_session_id,
                    region=region,
                    points=settings.DEFAULT_POINTS,
                    session_id_updated_at=datetime.now()
                )
                db.add(new_account)
                await db.commit()
                await db.refresh(new_account)
                
                return {
                    "message": "Account registered successfully",
                    "account": {
                        "id": new_account.id,
                        "email": new_account.email,
                        "region": new_account.region,
                        "points": new_account.points
                    }
                }
            
            elif status == "failed":
                error_msg = task_status.get("error", "Unknown error")
                raise HTTPException(status_code=500, detail=f"Registration failed: {error_msg}")
            
            # Still processing, wait before next poll
            if attempt < max_attempts - 1:
                await asyncio.sleep(poll_interval)
        
        # Max attempts reached
        raise HTTPException(status_code=408, detail="Registration timed out after 60 polling attempts")

@router.post("/accounts/{id}/update-session")
async def update_account_session(id: int, db: AsyncSession = Depends(get_db)):
    """
    Update session_id for an account via Dreamina-register API.
    """
    if not settings.REGISTER_API_URL or not settings.REGISTER_API_KEY:
        raise HTTPException(status_code=400, detail="Dreamina-register API not configured")
    
    # Get account from database
    db_account = await db.get(Account, id)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    base_url = settings.REGISTER_API_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.REGISTER_API_KEY}"}
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{base_url}/session/update",
                json={"email": db_account.email, "password": db_account.password},
                headers=headers
            )
            resp.raise_for_status()
            result = resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Session update API error: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update session: {str(e)}")
        
        new_session_id = result.get("session_id")
        if not new_session_id:
            raise HTTPException(status_code=500, detail="No session_id returned from API")
        
        # Strip region prefix before storing in database
        new_region = _get_region_from_session_id(new_session_id)
        clean_session_id = _strip_region_prefix(new_session_id)
        
        # Update database
        old_session_id = db_account.session_id
        db_account.session_id = clean_session_id
        db_account.session_id_updated_at = datetime.now()
        db_account.region = new_region
        
        await db.commit()
        await db.refresh(db_account)
        
        return {
            "message": "Session updated successfully",
            "old_session_id": old_session_id,
            "new_session_id": clean_session_id,
            "session_id_updated_at": db_account.session_id_updated_at.isoformat()
        }

# Settings Endpoints
@router.get("/settings")
async def get_settings():
    return settings.model_dump()

@router.post("/settings")
async def update_settings(new_settings: SettingsUpdate):
    # Update global settings
    current_settings = settings.model_dump()
    update_data = new_settings.model_dump(exclude_unset=True)
    
    # Apply updates
    updated_settings = settings.model_copy(update=update_data)
    
    # Save to file
    updated_settings.save_config()
    
    # Update global instance (hacky but works for simple case)
    # Ideally we should reload or use a dependency, but for now we update the global
    for key, value in update_data.items():
        setattr(settings, key, value)
        
    return settings.model_dump()
