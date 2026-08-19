# Configuration Module
import json
import os
import stat
import tempfile
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
SETTINGS_FILES: tuple[str, ...] = (
    "persona.json",
    "forbidden_words.json",
    "games.json",
)
MAX_SETTINGS_BYTES = 256 * 1024


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
        except (OSError, ValueError, RecursionError) as exc:
            print(f"⚠️ 설정 파일을 읽을 수 없습니다: {path} ({exc})")
            continue
    return default


def _canonicalize_games(raw: object, *, strict: bool = False) -> dict:
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("games.json 최상단은 객체여야 합니다.")
        return {}
    games = {}
    for name, info in raw.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 89:
            message = f"games.json의 게임 이름이 1~89자가 아닙니다: {name}"
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message}")
            continue
        if not isinstance(info, dict):
            message = f"games.json 항목이 객체가 아닙니다: {name}"
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message}")
            continue
        max_players = info.get("max_players")
        roles = info.get("roles", [])
        if type(max_players) is not int or not 1 <= max_players <= 25:
            message = f"games.json의 max_players가 1~25의 정수가 아닙니다: {name}"
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message}")
            continue
        if (
            not isinstance(roles, list)
            or len(roles) > 25
            or not all(isinstance(role, str) and 1 <= len(role) <= 100 for role in roles)
            or len(", ".join(roles)) > 1_024
        ):
            message = (
                "games.json의 roles가 최대 25개·각 1~100자·합계 1,024자 이내가 "
                f"아닙니다: {name}"
            )
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message}")
            continue
        games[name] = {"max_players": max_players, "roles": roles}
    return games


def _validate_settings_document(name: str, document: object) -> object:
    """실서비스 loader와 공유하는 canonicalizer로 문서를 검증한다."""
    if name == "persona.json":
        from module.hyacine_chat_cog import canonicalize_persona

        canonicalize_persona(document, strict=True)
        return document
    elif name == "forbidden_words.json":
        from module.forbiddenfilter_cog import canonicalize_forbidden_document

        return canonicalize_forbidden_document(document, strict=True)
    elif name == "games.json":
        _canonicalize_games(document, strict=True)
        return document
    else:
        raise ValueError("허용되지 않은 설정 파일입니다.")


def _open_settings_directory(*, create: bool) -> int:
    if create:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(SETTINGS_DIR, flags)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError(
                "SETTINGS_DIR은 현재 프로세스 소유이며 group/world 쓰기 불가여야 합니다."
            )
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _verify_temporary_entry(directory_fd: int, name: str, file_fd: int) -> None:
    entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    opened = os.fstat(file_fd)
    if (
        not stat.S_ISREG(entry.st_mode)
        or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RuntimeError("설정 임시 파일 entry가 교체되었습니다.")


def _reject_symlink(directory_fd: int, name: str) -> None:
    try:
        mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ValueError("심볼릭 링크 설정 파일은 사용할 수 없습니다.")


def read_settings_bytes(name: str) -> bytes | None:
    """고정한 settings directory에서 symlink를 따르지 않고 현재 설정을 읽는다."""
    if name not in SETTINGS_FILES:
        raise ValueError("허용되지 않은 설정 파일입니다.")
    try:
        directory_fd = _open_settings_directory(create=False)
    except FileNotFoundError:
        return None
    try:
        for candidate in (name, name.replace(".json", ".example.json")):
            try:
                file_fd = os.open(
                    candidate,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                continue
            except OSError:
                return None
            with os.fdopen(file_fd, "rb") as fp:
                if not stat.S_ISREG(os.fstat(fp.fileno()).st_mode):
                    return None
                data = fp.read(MAX_SETTINGS_BYTES + 1)
            return data if len(data) <= MAX_SETTINGS_BYTES else None
        return None
    finally:
        os.close(directory_fd)


def atomic_write_settings(name: str, document: object) -> None:
    """허용된 설정 JSON을 검증한 뒤 같은 디렉터리에서 원자 교체한다."""
    if name not in SETTINGS_FILES:
        raise ValueError("허용되지 않은 설정 파일입니다.")

    submitted = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    if len(submitted) > MAX_SETTINGS_BYTES:
        raise ValueError("설정 파일이 너무 큽니다.")
    validated = _validate_settings_document(name, document)
    payload = (json.dumps(validated, ensure_ascii=False, indent=2) + "\n").encode()
    if len(payload) > MAX_SETTINGS_BYTES:
        raise ValueError("설정 파일이 너무 큽니다.")

    directory_fd = _open_settings_directory(create=True)
    temporary_name = None
    temporary_path = None
    temporary_file = None
    temporary_verified = False
    try:
        _reject_symlink(directory_fd, name)
        temporary_file = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=SETTINGS_DIR,
            prefix=f".{name}.",
            delete=False,
        )
        temporary_path = Path(temporary_file.name)
        temporary_name = temporary_path.name
        with temporary_file as fp:
            _verify_temporary_entry(directory_fd, temporary_name, fp.fileno())
            temporary_verified = True
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
            temporary_verified = False
            _verify_temporary_entry(directory_fd, temporary_name, fp.fileno())
            temporary_verified = True
        _reject_symlink(directory_fd, name)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        temporary_path = None
        os.fsync(directory_fd)
    finally:
        try:
            if temporary_file is not None and not temporary_file.closed:
                temporary_file.close()
            try:
                if temporary_name is not None and temporary_verified:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                elif temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory_fd)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN") or None
CHAT_MODEL_LIGHT = os.getenv("CHAT_MODEL_LIGHT", "gpt-5.6-terra")
CHAT_MODEL_DEEP = os.getenv("CHAT_MODEL_DEEP", "gpt-5.6-sol")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
LIMIT_LIGHT = _int_from_env("LIMIT_LIGHT", 10)
LIMIT_DEEP = _int_from_env("LIMIT_DEEP", 3)
LIMIT_IMAGE = _int_from_env("LIMIT_IMAGE", 3)
# 채널/길드 ID는 환경변수에 두지 않는다. 봇이 여러 서버에 설치되면 운영자가 남의
# 서버 채널 ID를 알 수 없으므로, 각 서버 관리자가 /설정으로 지정하고 DB에 저장한다.
BACKUP_INTERVAL_SECONDS = _int_from_env("BACKUP_INTERVAL_SECONDS", 21600)
BACKUP_RETENTION_DAYS = _int_from_env("BACKUP_RETENTION_DAYS", 30)
# 일일 한도가 총량을 묶고, 쿨다운은 그 한도를 한 번에 소진하는 연타를 막는다.
AI_COOLDOWN_SECONDS = _int_from_env("AI_COOLDOWN_SECONDS", 15)
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()


def validate_config() -> None:
    """부팅을 막을 값만 검사한다.

    AI 키는 필수가 아니다. 없으면 main.py가 해당 확장을 로드하지 않을 뿐이고,
    파티·이벤트만 쓰려는 운영자가 결제를 붙일 이유가 없다.
    """
    if not DISCORD_TOKEN:
        raise RuntimeError("필수 환경변수가 없습니다: DISCORD_TOKEN")
    if BACKUP_INTERVAL_SECONDS <= 0:
        raise RuntimeError("BACKUP_INTERVAL_SECONDS는 양의 정수여야 합니다.")
    if BACKUP_RETENTION_DAYS <= 0:
        raise RuntimeError("BACKUP_RETENTION_DAYS는 양의 정수여야 합니다.")
    if AI_COOLDOWN_SECONDS <= 0:
        raise RuntimeError("AI_COOLDOWN_SECONDS는 양의 정수여야 합니다.")
    for name, value in (
        ("LIMIT_LIGHT", LIMIT_LIGHT),
        ("LIMIT_DEEP", LIMIT_DEEP),
        ("LIMIT_IMAGE", LIMIT_IMAGE),
    ):
        if value <= 0:
            raise RuntimeError(f"{name}는 양의 정수여야 합니다.")
    if DB_BACKEND != "sqlite":
        raise RuntimeError("현재 지원하는 DB_BACKEND는 sqlite뿐입니다.")


def load_games() -> dict:
    """settings/games.json → games.example.json 순으로 읽는다.

    형태가 어긋난 항목은 버린다. playwith_cog가 max_players/roles를 무조건
    참조하므로, 잘못된 항목 하나가 파티 기능 전체를 깨뜨리면 안 된다.
    """
    raw = load_settings_json("games.json", "games.example.json", default={})
    return _canonicalize_games(raw)


GAMES = load_games()
