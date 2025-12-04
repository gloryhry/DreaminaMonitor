from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, Boolean, DateTime, func
from config import settings

# Create async engine
engine = create_async_engine(settings.DATABASE_URL, echo=False)

# Create async session factory
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password: Mapped[str] = mapped_column(String)
    region: Mapped[str] = mapped_column(String, default="us")
    session_id: Mapped[str] = mapped_column(String, nullable=True)
    points: Mapped[float] = mapped_column(default=0.0)
    
    # Timestamps
    session_id_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    # Usage counters
    jimeng_4_0_count: Mapped[int] = mapped_column(default=0)
    jimeng_4_1_count: Mapped[int] = mapped_column(default=0)
    nanobanana_count: Mapped[int] = mapped_column(default=0)
    nanobananapro_count: Mapped[int] = mapped_column(default=0)
    video_3_0_count: Mapped[int] = mapped_column(default=0)

    # Error and Ban handling
    error_count: Mapped[int] = mapped_column(default=0)
    is_banned: Mapped[bool] = mapped_column(default=False)
    ban_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
