from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---------------- 服务器信息 ----------------
    server_host: str = "192.168.6.178"
    server_port: int = 4999

    # ---------------- 资源 ----------------
    download_path: str = "/data"
    download_works: int = 5
    file_expire_seconds: int = 3600
    x_proxy: Optional[str] = None

    # ---------------- Redis ----------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_pwd: Optional[str] = None

    # ---------------- 计算字段 ----------------
    @property
    def network_url(self) -> str:
        return f"http://{self.server_host}:{self.server_port}"

    @property
    def video_download_path(self) -> str:
        return f"{self.download_path}/video"

    @property
    def img_download_path(self) -> str:
        return f"{self.download_path}/img"

    model_config = SettingsConfigDict(
        env_file = ".env",
        env_file_encoding = "utf-8",
        case_sensitive = False
    )


# 实例化配置
settings = Settings()

# 自动创建下载目录
Path(settings.download_path).mkdir(parents = True, exist_ok = True)
Path(settings.video_download_path).mkdir(parents = True, exist_ok = True)
Path(settings.img_download_path).mkdir(parents = True, exist_ok = True)
