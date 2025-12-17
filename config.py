import json
import logging
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field

# 配置文件路径
CONFIG_FILE = Path(__file__).parent / "config.json"


class Settings(BaseSettings):
    """应用配置"""
    # 基础设置
    ADMIN_PASSWORD: str = Field(default="admin")
    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./dreamina.db")
    UPSTREAM_BASE_URL: str = Field(default="http://localhost:8080")
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=5100)
    PROXY_TIMEOUT: int = Field(default=300)
    
    # 日志等级
    LOG_LEVEL: str = Field(default="info")
    
    # 模型使用限制
    LIMIT_JIMENG_4_0: int = Field(default=60)
    LIMIT_JIMENG_4_1: int = Field(default=60)
    LIMIT_NANOBANANA: int = Field(default=60)
    LIMIT_NANOBANANAPRO: int = Field(default=60)
    LIMIT_VIDEO_3_0: int = Field(default=60)
    
    # Dreamina-register API 设置
    REGISTER_API_URL: Optional[str] = Field(default=None)
    REGISTER_API_KEY: Optional[str] = Field(default=None)
    REGISTER_MAIL_TYPE: str = Field(default="moemail")
    DEFAULT_POINTS: float = Field(default=120.0)
    
    # 重置时间设置
    RESET_COUNTS_TIME: str = Field(default="00:00")
    
    # Session 自动更新设置
    SESSION_UPDATE_DAYS: int = Field(default=7)
    SESSION_UPDATE_BATCH_SIZE: int = Field(default=5)
    
    # 自动注册设置
    AUTO_REGISTER_ENABLED: bool = Field(default=False)
    AUTO_REGISTER_INTERVAL: int = Field(default=3600)

    # 错误处理
    ACCOUNT_BAN_DURATION_HOURS: float = Field(default=4.0)
    
    # 定时积分更新设置
    POINTS_UPDATE_ENABLED: bool = Field(default=False)
    POINTS_UPDATE_INTERVAL: int = Field(default=3600)  # 更新间隔（秒），默认1小时
    
    @classmethod
    def load_config(cls) -> "Settings":
        """从 config.json 加载配置"""
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            return cls(**config_data)
        return cls()
    
    def save_config(self) -> None:
        """保存配置到 config.json"""
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=2, ensure_ascii=False)

    def get_log_level(self) -> int:
        """获取日志等级对应的 logging 常量"""
        level_map = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        return level_map.get(self.LOG_LEVEL.lower(), logging.INFO)


# 全局配置实例
settings = Settings.load_config()
