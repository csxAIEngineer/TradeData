"""连接配置。环境变量与 .env.example 同名。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "docker-compose.yml").is_file():
            return parent
    raise FileNotFoundError("找不到仓库根目录（缺少 docker-compose.yml）")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(repo_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "quant"
    postgres_password: str = "quant_local_dev"
    postgres_db: str = "ledger"

    clickhouse_host: str = "127.0.0.1"
    clickhouse_http_port: int = 8123
    clickhouse_user: str = "quant"
    clickhouse_password: str = "quant_local_dev"
    clickhouse_db: str = "market"

    arctic_uri: str = ""
    arctic_path: Path = Path("var/arctic")

    def arctic_connection_string(self) -> str:
        if self.arctic_uri.strip():
            return self.arctic_uri.strip()
        path = self.arctic_path
        if not path.is_absolute():
            path = repo_root() / path
        path.mkdir(parents=True, exist_ok=True)
        return "lmdb://" + path.resolve().as_posix()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
