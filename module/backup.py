from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
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
)
from module.database import SQLITE_TIMEOUT_SECONDS

DATABASES = {
    "attendance_data.db": {"users", "point_ledger"},
    "party_data.db": {"parties", "participants"},
    "guild_settings.db": {"guild_settings"},
}


@contextmanager
def pid_file(path: Path):
    if sys.platform != "darwin":
        yield
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    temporary_path = path.with_name(f"{path.name}.tmp")
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"PID 파일이 이미 사용 중입니다: {path}") from exc

        try:
            with temporary_path.open("w", encoding="ascii") as temporary:
                temporary.write(f"{os.getpid()}\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            try:
                yield
            finally:
                path.unlink(missing_ok=True)
        finally:
            temporary_path.unlink(missing_ok=True)


def _require_positive(name: str, value: int) -> None:
    if value <= 0:
        raise RuntimeError(f"{name}는 양의 정수여야 합니다.")


def _validate_backup_settings() -> None:
    _require_positive("BACKUP_INTERVAL_SECONDS", BACKUP_INTERVAL_SECONDS)
    _require_positive("BACKUP_RETENTION_DAYS", BACKUP_RETENTION_DAYS)


def verify_database(path: Path, required_tables: set[str]) -> dict[str, int]:
    if not path.is_file():
        raise RuntimeError(f"DB 파일이 없습니다: {path}")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(f"SQLite 무결성 검사 실패: {path}")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = required_tables - tables
            if missing:
                raise RuntimeError(
                    f"필수 테이블이 없습니다: {', '.join(sorted(missing))}"
                )
            return {
                table: conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in sorted(required_tables)
            }
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"SQLite 파일을 읽을 수 없습니다: {path}") from exc


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


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _temporary_path(prefix: str) -> Path:
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
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for created in missing:
        _fsync_directory(created)
    if missing:
        _fsync_directory(directory)


def create_backup_set(now: datetime | None = None) -> Path:
    _validate_backup_settings()
    _create_backup_directory()
    with (BACKUP_DIR / ".backup.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _create_backup_set(now)


def _create_backup_set(now: datetime | None) -> Path:
    created_at = _utc(now)
    timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    destinations = [
        *(BACKUP_DIR / f"{timestamp}-{source}" for source in DATABASES),
        BACKUP_DIR / f"{timestamp}-manifest.json",
    ]
    if any(path.exists() for path in destinations):
        raise RuntimeError(f"같은 시각의 백업이 이미 있습니다: {timestamp}")
    temporary_paths: list[Path] = []
    pending: list[tuple[Path, Path, str, dict[str, int]]] = []

    try:
        for source_name, required_tables in DATABASES.items():
            source = DATA_DIR / source_name
            final = BACKUP_DIR / f"{timestamp}-{source_name}"
            temporary = _temporary_path(f"{timestamp}-{source_name}")
            temporary_paths.append(temporary)
            _backup_one(source, temporary)
            counts = verify_database(temporary, required_tables)
            pending.append((temporary, final, source_name, counts))

        for temporary, _, _, _ in pending:
            _fsync_file(temporary)
        for temporary, final, _, _ in pending:
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
        }
        manifest_path = BACKUP_DIR / f"{timestamp}-manifest.json"
        manifest_temporary = _temporary_path(f"{timestamp}-manifest.json")
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

    prune_backups(now=created_at)
    return manifest_path


def _load_manifest(manifest_path: Path) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = manifest["databases"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"백업 manifest를 읽을 수 없습니다: {manifest_path}") from exc

    if not isinstance(items, list):
        raise RuntimeError(f"잘못된 백업 manifest입니다: {manifest_path}")

    sources: set[str] = set()
    backups: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError(f"잘못된 백업 manifest입니다: {manifest_path}")
        source = item.get("source")
        backup = item.get("backup")
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
    return manifest


def verify_backup_set(manifest_path: Path) -> dict[str, dict[str, int]]:
    manifest_path = Path(manifest_path)
    manifest = _load_manifest(manifest_path)
    verified: dict[str, dict[str, int]] = {}

    for item in manifest["databases"]:
        source_name = item["source"]
        backup = manifest_path.parent / item["backup"]
        try:
            size = backup.stat().st_size
            checksum = _sha256(backup)
        except OSError as exc:
            raise RuntimeError(f"백업 파일을 읽을 수 없습니다: {backup}") from exc
        if size != item.get("size"):
            raise RuntimeError(f"백업 파일 크기가 일치하지 않습니다: {backup}")
        if checksum != item.get("sha256"):
            raise RuntimeError(f"백업 체크섬이 일치하지 않습니다: {backup}")
        counts = verify_database(backup, DATABASES[source_name])
        if counts != item.get("tables"):
            raise RuntimeError(f"백업 테이블 정보가 일치하지 않습니다: {backup}")
        verified[source_name] = counts

    return verified


def restore_test(manifest_path: Path) -> None:
    manifest_path = Path(manifest_path)
    manifest = _load_manifest(manifest_path)
    verify_backup_set(manifest_path)
    with tempfile.TemporaryDirectory(prefix="hsr_restore_") as temp_dir:
        temp_root = Path(temp_dir)
        for item in manifest["databases"]:
            source = manifest_path.parent / item["backup"]
            restored = temp_root / item["source"]
            shutil.copy2(source, restored)
            verify_database(restored, DATABASES[item["source"]])


def _under_data_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(DATA_DIR.resolve())
        return True
    except ValueError:
        return False


def prune_backups(now: datetime | None = None) -> int:
    _require_positive("BACKUP_RETENTION_DAYS", BACKUP_RETENTION_DAYS)
    cutoff = _utc(now) - timedelta(days=BACKUP_RETENTION_DAYS)
    deleted = 0

    for manifest_path in BACKUP_DIR.glob("*-manifest.json"):
        timestamp = manifest_path.name.removesuffix("-manifest.json")
        try:
            created_at = datetime.strptime(
                timestamp,
                "%Y%m%dT%H%M%SZ",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created_at >= cutoff:
            continue

        try:
            manifest = _load_manifest(manifest_path)
            if any(
                item["backup"] != f"{timestamp}-{item['source']}"
                for item in manifest["databases"]
            ):
                continue
            verify_backup_set(manifest_path)
            files = [
                manifest_path.parent / item["backup"]
                for item in manifest["databases"]
            ]
        except RuntimeError:
            continue
        if any(_under_data_dir(path) for path in files):
            continue

        for path in files:
            path.unlink()
        manifest_path.unlink()
        deleted += 1

    return deleted


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
