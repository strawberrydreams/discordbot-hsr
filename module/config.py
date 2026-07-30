# Configuration Module
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_ENV_FILE = PROJECT_ROOT / ".env.secrets"
RUNTIME_ENV_FILE = PROJECT_ROOT / ".env.runtime"


def _load_env_files(project_root: Path = PROJECT_ROOT) -> None:
    load_dotenv(project_root / ".env.secrets")
    load_dotenv(project_root / ".env.runtime")


_load_env_files()


def _path_from_env(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _int_from_env(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        if default is None:
            raise RuntimeError(f"{name} 환경변수가 필요합니다.")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 환경변수는 정수여야 합니다.") from exc


DATA_DIR = _path_from_env("DATA_DIR", "runtime/data")
BACKUP_DIR = _path_from_env("BACKUP_DIR", "runtime/backups")
FORBIDDEN_WORDS_FILE = _path_from_env(
    "FORBIDDEN_WORDS_FILE",
    str(DATA_DIR / "forbidden_words.json"),
)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
RECRUIT_CHANNEL_ID = _int_from_env("RECRUIT_CHANNEL_ID", 0)
EVENT_CHANNEL_ID = _int_from_env("EVENT_CHANNEL_ID", 0)
DISCORD_GUILD_ID = _int_from_env("DISCORD_GUILD_ID", 0)
BACKUP_INTERVAL_SECONDS = _int_from_env("BACKUP_INTERVAL_SECONDS", 21600)
BACKUP_RETENTION_DAYS = _int_from_env("BACKUP_RETENTION_DAYS", 30)
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()


def validate_config() -> None:
    missing = [
        name
        for name, value in (
            ("DISCORD_TOKEN", DISCORD_TOKEN),
            ("OPENAI_API_KEY", OPENAI_API_KEY),
            ("GOOGLE_API_KEY", GOOGLE_API_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"필수 환경변수가 없습니다: {', '.join(missing)}")
    if RECRUIT_CHANNEL_ID <= 0:
        raise RuntimeError("RECRUIT_CHANNEL_ID는 양의 정수여야 합니다.")
    if EVENT_CHANNEL_ID <= 0:
        raise RuntimeError("EVENT_CHANNEL_ID는 양의 정수여야 합니다.")
    if DISCORD_GUILD_ID <= 0:
        raise RuntimeError("DISCORD_GUILD_ID는 양의 정수여야 합니다.")
    if BACKUP_INTERVAL_SECONDS <= 0:
        raise RuntimeError("BACKUP_INTERVAL_SECONDS는 양의 정수여야 합니다.")
    if BACKUP_RETENTION_DAYS <= 0:
        raise RuntimeError("BACKUP_RETENTION_DAYS는 양의 정수여야 합니다.")
    if DB_BACKEND != "sqlite":
        raise RuntimeError("현재 지원하는 DB_BACKEND는 sqlite뿐입니다.")


GAMES = {
    "League of Legends": {
        "max_players": 5,
        "roles": ["탑", "정글", "미드", "원딜", "서포터"],
    },
    "PUBG": {
        "max_players": 4,
        "roles": [],
    },
    "Overwatch": {
        "max_players": 5,
        "roles": ["딜러1", "딜러2", "탱커", "힐러1", "힐러2"],
    },
}
