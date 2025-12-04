from typing import List, Optional
from datetime import datetime, timedelta
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

@router.post("/accounts/{id}/ban")
async def ban_account(id: int, duration_hours: int = 12, db: AsyncSession = Depends(get_db)):
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
