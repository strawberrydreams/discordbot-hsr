# Configuration Module
import json
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
SETTINGS_DIR = _path_from_env("SETTINGS_DIR", "settings")


def load_settings_json(*names: str, default):
    """settings/ 아래 JSON을 읽는다.

    names를 순서대로 시도해 처음 성공한 것을 반환한다. 실서비스 파일이 없으면
    커밋된 *.example.json으로 떨어지는 용도다. 전부 실패하면 default를 돌려준다.
    운영자 설정 하나가 없다고 봇 전체가 죽으면 안 된다.
    """
    for name in names:
        path = SETTINGS_DIR / name
        try:
            with path.open(encoding="utf-8") as fp:
                return json.load(fp)
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            print(f"⚠️ 설정 파일을 읽을 수 없습니다: {path} ({exc})")
            continue
    return default

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# 채널/길드 ID는 환경변수에 두지 않는다. 봇이 여러 서버에 설치되면 운영자가 남의
# 서버 채널 ID를 알 수 없으므로, 각 서버 관리자가 /설정으로 지정하고 DB에 저장한다.
BACKUP_INTERVAL_SECONDS = _int_from_env("BACKUP_INTERVAL_SECONDS", 21600)
BACKUP_RETENTION_DAYS = _int_from_env("BACKUP_RETENTION_DAYS", 30)
# 포인트 잔액이 지속 사용량을 묶고, 쿨다운은 누적 잔액을 한 번에 소진하는 버스트를 막는다.
AI_COOLDOWN_SECONDS = _int_from_env("AI_COOLDOWN_SECONDS", 15)
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
    if BACKUP_INTERVAL_SECONDS <= 0:
        raise RuntimeError("BACKUP_INTERVAL_SECONDS는 양의 정수여야 합니다.")
    if BACKUP_RETENTION_DAYS <= 0:
        raise RuntimeError("BACKUP_RETENTION_DAYS는 양의 정수여야 합니다.")
    if AI_COOLDOWN_SECONDS <= 0:
        raise RuntimeError("AI_COOLDOWN_SECONDS는 양의 정수여야 합니다.")
    if DB_BACKEND != "sqlite":
        raise RuntimeError("현재 지원하는 DB_BACKEND는 sqlite뿐입니다.")


def load_games() -> dict:
    """settings/games.json → games.example.json 순으로 읽는다.

    형태가 어긋난 항목은 버린다. playwith_cog가 max_players/roles를 무조건
    참조하므로, 잘못된 항목 하나가 파티 기능 전체를 깨뜨리면 안 된다.
    """
    raw = load_settings_json("games.json", "games.example.json", default={})
    if not isinstance(raw, dict):
        return {}
    games = {}
    for name, info in raw.items():
        if not isinstance(info, dict):
            print(f"⚠️ games.json 항목이 객체가 아닙니다: {name}")
            continue
        max_players = info.get("max_players")
        roles = info.get("roles", [])
        if not isinstance(max_players, int) or max_players <= 0:
            print(f"⚠️ games.json의 max_players가 양의 정수가 아닙니다: {name}")
            continue
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            print(f"⚠️ games.json의 roles가 문자열 배열이 아닙니다: {name}")
            continue
        games[name] = {"max_players": max_players, "roles": roles}
    return games


GAMES = load_games()
