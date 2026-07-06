"""Configuration for Memory MCP Server."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class MemoryConfig:
    """Memory storage configuration."""

    db_path: str
    collection_name: str
    embedding_model: str = "intfloat/multilingual-e5-base"
    enable_bm25: bool = True

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        """Create config from environment variables."""
        default_path = str(Path.home() / ".claude" / "memories" / "memory.db")

        return cls(
            db_path=os.getenv("MEMORY_DB_PATH", default_path),
            collection_name=os.getenv("MEMORY_COLLECTION_NAME", "claude_memories"),
            embedding_model=os.getenv("MEMORY_EMBEDDING_MODEL", "intfloat/multilingual-e5-base"),
            enable_bm25=os.getenv("MEMORY_ENABLE_BM25", "true").lower() != "false",
        )


@dataclass(frozen=True)
class SleepConfig:
    """Sleep (memory consolidation/forgetting) configuration."""

    min_age_days: int = 14  # 対象の最低経過日数
    similarity_threshold: float = 0.85  # 圧縮時の類似度閾値
    decay_retention_threshold: float = 0.4  # この保持スコア以下で減衰
    forget_min_age_days: int = 14  # 忘却の最低経過日数
    forget_max_access: int = 3  # 忘却対象の最大アクセス回数
    protected_importance: int = 4  # この値以上は絶対保護
    protected_emotions: tuple[str, ...] = ("happy", "moved", "excited", "surprised")


@dataclass(frozen=True)
class ServerConfig:
    """MCP Server configuration."""

    name: str = "memory-mcp"
    version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Create config from environment variables."""
        return cls(
            name=os.getenv("MCP_SERVER_NAME", "memory-mcp"),
            version=os.getenv("MCP_SERVER_VERSION", "0.1.0"),
        )
