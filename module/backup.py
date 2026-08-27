from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from module.config import (
    BACKUP_DIR,
    BACKUP_INTERVAL_SECONDS,
    BACKUP_RETENTION_DAYS,
    DATA_DIR,
    SETTINGS_DIR,
    SETTINGS_FILES,
    ensure_private_directory,
    ensure_private_file,
)
from module.database import (
    SQLITE_TIMEOUT_SECONDS,
    SQLiteGuildSettingsRepository,
    SQLitePartyRepository,
    SQLiteProfileRepository,
    SQLiteUsageRepository,
)

DATABASES = {
    "attendance_data.db": {"users", "ai_usage"},
    "party_data.db": {"parties", "participants"},
    "guild_settings.db": {"guild_settings", "party_panels"},
    "profile_data.db": {"game_uids"},
}
_SQLITE_REPOSITORIES = {
    "attendance_data.db": SQLiteUsageRepository,
    "party_data.db": SQLitePartyRepository,
    "guild_settings.db": SQLiteGuildSettingsRepository,
    "profile_data.db": SQLiteProfileRepository,
}
_CURRENT_SCHEMA_VERSIONS = {
    name: repository._SCHEMA_VERSION
    for name, repository in _SQLITE_REPOSITORIES.items()
}
_LEGACY_ATTENDANCE_TABLES = {"users", "point_ledger"}
_LEGACY_PARTY_TABLES = {"parties", "participants"}
_LEGACY_GUILD_SETTINGS_TABLES = {"guild_settings"}


@contextmanager
def pid_file(pid_path: Path):
    if sys.platform != "darwin":
        yield
        return

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = pid_path.with_name(f"{pid_path.name}.lock")
    temporary_path = pid_path.with_name(f"{pid_path.name}.tmp")
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"PID 파일이 이미 사용 중입니다: {pid_path}"
            ) from exc

        try:
            with temporary_path.open("w", encoding="ascii") as temporary:
                temporary.write(f"{os.getpid()}\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, pid_path)
            try:
                yield
            finally:
                pid_path.unlink(missing_ok=True)
        finally:
            temporary_path.unlink(missing_ok=True)


def _require_positive(config_name: str, configured_value: int) -> None:
    if configured_value <= 0:
        raise RuntimeError(f"{config_name}는 양의 정수여야 합니다.")


def _validate_backup_settings() -> None:
    _require_positive("BACKUP_INTERVAL_SECONDS", BACKUP_INTERVAL_SECONDS)
    _require_positive("BACKUP_RETENTION_DAYS", BACKUP_RETENTION_DAYS)


def verify_database(
    database_path: Path,
    required_tables: set[str],
    *,
    source_name: str,
    allow_legacy: bool = False,
) -> dict[str, int]:
    if not database_path.is_file():
        raise RuntimeError(f"DB 파일이 없습니다: {database_path}")
    try:
        with closing(
            sqlite3.connect(
                f"file:{database_path}?mode=ro",
                uri=True,
                timeout=SQLITE_TIMEOUT_SECONDS,
            )
        ) as conn:
            integrity_result = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity_result != ("ok",):
                raise RuntimeError(
                    f"SQLite 무결성 검사 실패: {database_path}"
                )
            supported_version = _CURRENT_SCHEMA_VERSIONS[source_name]
            schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if schema_version > supported_version:
                raise RuntimeError(
                    f"{source_name} DB 버전 {schema_version}은 지원 버전 "
                    f"{supported_version}보다 높습니다."
                )
            if schema_version < supported_version and not allow_legacy:
                raise RuntimeError(
                    f"{source_name} DB 버전 {schema_version}은 현재 버전 "
                    f"{supported_version}이 아닙니다."
                )
            if (
                allow_legacy
                and source_name == "attendance_data.db"
                and schema_version in (0, 1)
            ):
                required_tables = _LEGACY_ATTENDANCE_TABLES
            if (
                allow_legacy
                and source_name == "guild_settings.db"
                and schema_version in (0, 1)
            ):
                required_tables = _LEGACY_GUILD_SETTINGS_TABLES
            if (
                allow_legacy
                and source_name == "party_data.db"
                and schema_version in (0, 1)
            ):
                required_tables = _LEGACY_PARTY_TABLES
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing_tables = required_tables - table_names
            if missing_tables:
                raise RuntimeError(
                    "필수 테이블이 없습니다: "
                    f"{', '.join(sorted(missing_tables))}"
                )
            return {
                table: conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in sorted(required_tables)
            }
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"SQLite 파일을 읽을 수 없습니다: {database_path}"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_one(source: Path, temporary: Path) -> None:
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS)) as source_conn:
        with closing(sqlite3.connect(temporary, timeout=SQLITE_TIMEOUT_SECONDS)) as target_conn:
            source_conn.backup(target_conn)
            target_conn.commit()


def _copy_setting(source: Path, temporary: Path) -> bool:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("설정 파일 안전 복사를 지원하지 않는 운영체제입니다.")
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | no_follow)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"설정 파일 symlink는 백업하지 않습니다: {source}") from exc
        raise RuntimeError(f"설정 파일을 열 수 없습니다: {source}") from exc
    try:
        source_mode = os.fstat(descriptor).st_mode
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError(f"설정 파일을 검사할 수 없습니다: {source}") from exc
    if not stat.S_ISREG(source_mode):
        os.close(descriptor)
        raise RuntimeError(f"설정 경로가 일반 파일이 아닙니다: {source}")
    with os.fdopen(descriptor, "rb") as input_file:
        with temporary.open("wb") as output_file:
            shutil.copyfileobj(input_file, output_file)
    return True


def _open_settings_stage(path: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise RuntimeError("안전한 복구 stage를 지원하지 않는 운영체제입니다.")
    try:
        descriptor = os.open(path, os.O_RDONLY | directory | no_follow)
    except OSError as exc:
        raise RuntimeError(f"복구 stage 설정 경로가 안전하지 않습니다: {path}") from exc
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    raise RuntimeError(f"복구 stage 설정 경로가 안전하지 않습니다: {path}")


def _copy_staged_setting(source: Path, name: str, directory_descriptor: int) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise RuntimeError(f"복구 stage 설정 파일을 만들 수 없습니다: {name}") from exc
    with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
        shutil.copyfileobj(input_file, output_file)


def _as_utc(now: datetime | None) -> datetime:
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _create_temporary_path(prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=BACKUP_DIR,
        prefix=f".{prefix}-",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(name)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as fp:
        os.fsync(fp.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_backup_directory() -> None:
    missing = []
    directory = BACKUP_DIR
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    ensure_private_directory(BACKUP_DIR)
    for created in missing:
        _fsync_directory(created)
    if missing:
        _fsync_directory(directory)


def create_backup_set(now: datetime | None = None) -> Path:
    _validate_backup_settings()
    _create_backup_directory()
    lock_path = BACKUP_DIR / ".backup.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(lock_descriptor, "a+b") as lock:
        ensure_private_file(lock_path)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _create_backup_set(now)


def _create_backup_set(now: datetime | None) -> Path:
    created_at = _as_utc(now)
    timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    destinations = [
        *(BACKUP_DIR / f"{timestamp}-{source}" for source in DATABASES),
        *(BACKUP_DIR / f"{timestamp}-setting-{source}" for source in SETTINGS_FILES),
        BACKUP_DIR / f"{timestamp}-manifest.json",
    ]
    if any(path.exists() for path in destinations):
        raise RuntimeError(f"같은 시각의 백업이 이미 있습니다: {timestamp}")
    manifest_path = destinations[-1]
    temporary_paths: list[Path] = []
    pending: list[tuple[Path, Path, str, dict[str, int]]] = []
    pending_settings: list[tuple[Path, Path, str]] = []

    try:
        for source_name, required_tables in DATABASES.items():
            source = DATA_DIR / source_name
            final = BACKUP_DIR / f"{timestamp}-{source_name}"
            temporary = _create_temporary_path(f"{timestamp}-{source_name}")
            temporary_paths.append(temporary)
            _backup_one(source, temporary)
            counts = verify_database(
                temporary,
                required_tables,
                source_name=source_name,
            )
            pending.append((temporary, final, source_name, counts))

        for source_name in SETTINGS_FILES:
            source = SETTINGS_DIR / source_name
            final = BACKUP_DIR / f"{timestamp}-setting-{source_name}"
            temporary = _create_temporary_path(
                f"{timestamp}-setting-{source_name}"
            )
            temporary_paths.append(temporary)
            if not _copy_setting(source, temporary):
                continue
            pending_settings.append((temporary, final, source_name))

        for temporary, _, _, _ in pending:
            _fsync_file(temporary)
        for temporary, _, _ in pending_settings:
            _fsync_file(temporary)
        for temporary, final, _, _ in pending:
            os.replace(temporary, final)
        for temporary, final, _ in pending_settings:
            os.replace(temporary, final)
        _fsync_directory(BACKUP_DIR)

        manifest = {
            "created_at": created_at.isoformat(),
            "databases": [
                {
                    "source": source_name,
                    "backup": final.name,
                    "size": final.stat().st_size,
                    "sha256": _sha256(final),
                    "tables": counts,
                }
                for _, final, source_name, counts in pending
            ],
            "settings": [
                {
                    "source": source_name,
                    "backup": final.name,
                    "size": final.stat().st_size,
                    "sha256": _sha256(final),
                }
                for _, final, source_name in pending_settings
            ],
        }
        manifest_temporary = _create_temporary_path(
            f"{timestamp}-manifest.json"
        )
        temporary_paths.append(manifest_temporary)
        with manifest_temporary.open("w", encoding="utf-8") as fp:
            json.dump(manifest, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(manifest_temporary, manifest_path)
        _fsync_directory(BACKUP_DIR)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
        # manifest가 backup set의 유일한 commit point다. 공개되기 전 실패는 이미
        # rename된 data/settings 파일까지 지워 다음 실행과 prune에 고아를 남기지 않는다.
        if not manifest_path.exists():
            removed = False
            for _, final, _, _ in pending:
                if final.exists():
                    final.unlink()
                    removed = True
            for _, final, _ in pending_settings:
                if final.exists():
                    final.unlink()
                    removed = True
            if removed:
                _fsync_directory(BACKUP_DIR)

    prune_backups(now=created_at)
    return manifest_path


def _load_manifest(manifest_path: Path) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        database_entries = manifest["databases"]
        setting_entries = manifest.get("settings", [])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"백업 manifest를 읽을 수 없습니다: {manifest_path}") from exc

    if not isinstance(database_entries, list) or not isinstance(
        setting_entries, list
    ):
        raise RuntimeError(f"잘못된 백업 manifest입니다: {manifest_path}")

    sources: set[str] = set()
    backups: set[str] = set()
    for database_entry in database_entries:
        if not isinstance(database_entry, dict):
            raise RuntimeError(f"잘못된 백업 manifest입니다: {manifest_path}")
        source = database_entry.get("source")
        backup = database_entry.get("backup")
        if (
            not isinstance(source, str)
            or source not in DATABASES
            or source in sources
        ):
            raise RuntimeError(f"잘못된 백업 DB 항목입니다: {source}")
        if (
            not isinstance(backup, str)
            or Path(backup).name != backup
            or backup in backups
        ):
            raise RuntimeError(f"잘못된 백업 파일 경로입니다: {backup}")
        sources.add(source)
        backups.add(backup)

    if sources != set(DATABASES):
        raise RuntimeError(f"완전하지 않은 백업 manifest입니다: {manifest_path}")
    timestamp = manifest_path.name.removesuffix("-manifest.json")
    setting_sources: set[str] = set()
    for setting_entry in setting_entries:
        if not isinstance(setting_entry, dict):
            raise RuntimeError(f"잘못된 백업 manifest입니다: {manifest_path}")
        source = setting_entry.get("source")
        backup = setting_entry.get("backup")
        if (
            not isinstance(source, str)
            or source not in SETTINGS_FILES
            or Path(source).name != source
            or source in setting_sources
        ):
            raise RuntimeError(f"잘못된 백업 설정 항목입니다: {source}")
        if (
            not isinstance(backup, str)
            or Path(backup).name != backup
            or backup != f"{timestamp}-setting-{source}"
            or backup in backups
        ):
            raise RuntimeError(f"잘못된 백업 파일 경로입니다: {backup}")
        setting_sources.add(source)
        backups.add(backup)
    manifest["settings"] = setting_entries
    return manifest


def verify_backup_set(manifest_path: Path) -> dict[str, dict[str, int]]:
    manifest_path = Path(manifest_path)
    manifest = _load_manifest(manifest_path)
    verified: dict[str, dict[str, int]] = {}

    for database_entry in manifest["databases"]:
        source_name = database_entry["source"]
        backup = manifest_path.parent / database_entry["backup"]
        try:
            size = backup.stat().st_size
            checksum = _sha256(backup)
        except OSError as exc:
            raise RuntimeError(f"백업 파일을 읽을 수 없습니다: {backup}") from exc
        if size != database_entry.get("size"):
            raise RuntimeError(f"백업 파일 크기가 일치하지 않습니다: {backup}")
        if checksum != database_entry.get("sha256"):
            raise RuntimeError(f"백업 체크섬이 일치하지 않습니다: {backup}")
        counts = verify_database(
            backup,
            DATABASES[source_name],
            source_name=source_name,
            allow_legacy=True,
        )
        if counts != database_entry.get("tables"):
            raise RuntimeError(f"백업 테이블 정보가 일치하지 않습니다: {backup}")
        verified[source_name] = counts

    for setting_entry in manifest["settings"]:
        backup = manifest_path.parent / setting_entry["backup"]
        try:
            size = backup.stat().st_size
            checksum = _sha256(backup)
        except OSError as exc:
            raise RuntimeError(f"백업 파일을 읽을 수 없습니다: {backup}") from exc
        if size != setting_entry.get("size"):
            raise RuntimeError(f"백업 파일 크기가 일치하지 않습니다: {backup}")
        if checksum != setting_entry.get("sha256"):
            raise RuntimeError(f"백업 체크섬이 일치하지 않습니다: {backup}")

    return verified


def stage_restore(manifest_path: Path, stage: Path) -> None:
    manifest_path = Path(manifest_path)
    manifest = _load_manifest(manifest_path)
    verify_backup_set(manifest_path)
    stage = Path(stage)
    destinations = [
        stage / database_entry["source"]
        for database_entry in manifest["databases"]
    ]
    setting_destinations = [
        stage / "settings" / setting_entry["source"]
        for setting_entry in manifest["settings"]
    ]
    settings_stage = stage / "settings"
    if setting_destinations and (
        settings_stage.is_symlink()
        or (settings_stage.exists() and not settings_stage.is_dir())
    ):
        raise RuntimeError(f"복구 stage 설정 경로가 안전하지 않습니다: {settings_stage}")
    if any(path.exists() for path in destinations):
        raise RuntimeError(f"복구 stage에 DB 파일이 이미 있습니다: {stage}")
    if any(path.exists() for path in setting_destinations):
        raise RuntimeError(f"복구 stage에 설정 파일이 이미 있습니다: {stage}")
    ensure_private_directory(stage)
    settings_descriptor = None
    if setting_destinations:
        ensure_private_directory(settings_stage)
        settings_descriptor = _open_settings_stage(settings_stage)
    try:
        for database_entry, restored in zip(
            manifest["databases"], destinations
        ):
            source_name = database_entry["source"]
            source = manifest_path.parent / database_entry["backup"]
            shutil.copy2(source, restored)
            _SQLITE_REPOSITORIES[source_name](restored)
            verify_database(
                restored,
                DATABASES[source_name],
                source_name=source_name,
            )
        if settings_descriptor is not None:
            for setting_entry in manifest["settings"]:
                _copy_staged_setting(
                    manifest_path.parent / setting_entry["backup"],
                    setting_entry["source"],
                    settings_descriptor,
                )
    finally:
        if settings_descriptor is not None:
            os.close(settings_descriptor)


def restore_test(manifest_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hsr_restore_") as temporary_directory:
        stage_restore(manifest_path, Path(temporary_directory))


def _under_data_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(DATA_DIR.resolve())
        return True
    except ValueError:
        return False


def prune_backups(now: datetime | None = None) -> int:
    _require_positive("BACKUP_RETENTION_DAYS", BACKUP_RETENTION_DAYS)
    retention_cutoff = _as_utc(now) - timedelta(days=BACKUP_RETENTION_DAYS)
    deleted_set_count = 0

    for manifest_path in BACKUP_DIR.glob("*-manifest.json"):
        timestamp = manifest_path.name.removesuffix("-manifest.json")
        try:
            created_at = datetime.strptime(
                timestamp,
                "%Y%m%dT%H%M%SZ",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created_at >= retention_cutoff:
            continue

        try:
            manifest = _load_manifest(manifest_path)
            if any(
                database_entry["backup"]
                != f"{timestamp}-{database_entry['source']}"
                for database_entry in manifest["databases"]
            ) or any(
                setting_entry["backup"]
                != f"{timestamp}-setting-{setting_entry['source']}"
                for setting_entry in manifest["settings"]
            ):
                continue
            verify_backup_set(manifest_path)
            files = [
                manifest_path.parent / database_entry["backup"]
                for database_entry in manifest["databases"]
            ]
            files.extend(
                manifest_path.parent / setting_entry["backup"]
                for setting_entry in manifest["settings"]
            )
        except RuntimeError:
            continue
        if any(_under_data_dir(path) for path in files):
            continue

        for path in files:
            path.unlink()
        manifest_path.unlink()
        deleted_set_count += 1

    return deleted_set_count


def latest_manifest() -> Path:
    manifests = sorted(BACKUP_DIR.glob("*-manifest.json"))
    if not manifests:
        raise RuntimeError("검증할 백업 manifest가 없습니다.")
    return manifests[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("create", "verify", "restore-test", "loop"),
    )
    args = parser.parse_args()

    if args.command == "create":
        print(create_backup_set())
        return 0
    if args.command == "verify":
        verify_backup_set(latest_manifest())
        return 0
    if args.command == "restore-test":
        restore_test(latest_manifest())
        return 0

    _validate_backup_settings()
    with pid_file(BACKUP_DIR / ".backup.pid"):
        while True:
            try:
                print(create_backup_set())
            except Exception as exc:
                print(f"백업 실패: {exc}", flush=True)
                time.sleep(min(60, BACKUP_INTERVAL_SECONDS))
                continue
            time.sleep(BACKUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
