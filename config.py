import json
import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

CONFIG_FILE = "config.json"

class Settings(BaseSettings):
    ADMIN_PASSWORD: str = Field(default="admin123", description="Password for both API proxy and Frontend login")
    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./dreamina.db", description="Database connection string")
    UPSTREAM_BASE_URL: str = Field(default="https://api.dreamina.com", description="Upstream API URL")
    PORT: int = Field(default=5100, description="Server port")
    HOST: str = Field(default="0.0.0.0", description="Server host")
    
    # Proxy settings
    PROXY_TIMEOUT: int = Field(default=60, description="Proxy request timeout in seconds")
    
    # Model Usage Thresholds
    LIMIT_JIMENG_4_0: int = Field(default=1000, description="Daily limit for jimeng-4.0")
    LIMIT_JIMENG_4_1: int = Field(default=1000, description="Daily limit for jimeng-4.1")
    LIMIT_NANOBANANA: int = Field(default=1000, description="Daily limit for nanobanana")
    LIMIT_NANOBANANAPRO: int = Field(default=1000, description="Daily limit for nanobananapro")
    LIMIT_VIDEO_3_0: int = Field(default=1000, description="Daily limit for video-3.0")
    
    # Dreamina-register API settings
    REGISTER_API_URL: str = Field(default="http://localhost:8000", description="Dreamina-register API URL")
    REGISTER_API_KEY: str = Field(default="", description="Dreamina-register API Key")
    REGISTER_MAIL_TYPE: str = Field(default="moemail", description="Mail type for registration (moemail/tempmailhub)")
    DEFAULT_POINTS: float = Field(default=120.0, description="Default points for new accounts")
    RESET_COUNTS_TIME: str = Field(default="00:00", description="Daily reset time for usage counts (HH:MM format)")
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def load_config(cls) -> "Settings":
        """
        Load settings with priority: Config File > Environment Variables > Defaults
        """
        # Start with defaults and env vars
        settings = cls()
        
        # Override with config file if it exists
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
                    # Update settings with file values
                    # We iterate over fields to ensure we only update valid settings
                    updated_values = {}
                    for key, value in file_config.items():
                        if hasattr(settings, key):
                            updated_values[key] = value
                    
                    # Create a new instance with updated values
                    settings = settings.model_copy(update=updated_values)
            except Exception as e:
                print(f"Error loading config file: {e}")
        
        return settings

    def save_config(self):
        """
        Save current settings to config.json
        """
        config_dict = self.model_dump()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, indent=4)
        except Exception as e:
            print(f"Error saving config file: {e}")

# Global settings instance
settings = Settings.load_config()
