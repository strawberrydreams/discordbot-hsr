"""삭제 예정 데이터를 JSON으로 내보낸다.

실행: python -m module.export_legacy [출력파일]

포인트·출석·음악 기능을 제거하는 마이그레이션 전에 실행한다. 원본 DB는 읽지도
쓰지도 않고, SQLite 온라인 backup API로 스냅숏을 뜬 뒤 그 사본에서 읽으므로
봇이 켜져 있는 상태에서도 안전하고 일관된 결과가 나온다.

이미 마이그레이션이 끝난 DB에 대고 실행해도 죽지 않는다. 사라진 테이블·컬럼은
null로 기록되고 나머지는 그대로 나온다. 두 번 실행해도 원본은 그대로다.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from module.backup import _backup_one
from module.config import BACKUP_DIR, DATA_DIR


# (섹션 이름, 테이블, 뽑을 컬럼) — DB 파일별로 묶는다.
EXPORTS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "attendance_data.db": (
        (
            "users",
            "users",
            ("guild_id", "user_id", "points", "last_attendance_date"),
        ),
        (
            "point_ledger",
            "point_ledger",
            ("id", "guild_id", "user_id", "delta", "reason", "created_at"),
        ),
    ),
    "guild_settings.db": (
        (
            "music_settings",
            "guild_settings",
            ("guild_id", "music_channel_id", "music_panel_msg_id"),
        ),
    ),
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _read_table(
    conn: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> list[dict] | None:
    """테이블이나 컬럼이 이미 없으면 None. 없는 것과 비어 있는 것을 구분한다."""
    present = _table_columns(conn, table)
    if not present or not set(columns) <= present:
        return None
    # 컬럼명은 이 모듈의 상수에서만 오므로 외부 입력이 SQL에 섞이지 않는다.
    selection = ", ".join(f'"{column}"' for column in columns)
    rows = conn.execute(f'SELECT {selection} FROM "{table}"').fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _export_database(
    db_path: Path, requests: tuple[tuple[str, str, tuple[str, ...]], ...]
) -> tuple[int | None, dict[str, list[dict] | None]]:
    """스냅숏을 떠서 읽는다. 원본은 읽기 전용으로만 연다."""
    if not db_path.exists():
        return None, {section: None for section, _, _ in requests}

    with tempfile.TemporaryDirectory(prefix="legacy_export_") as directory:
        snapshot = Path(directory) / db_path.name
        _backup_one(db_path, snapshot)
        with closing(sqlite3.connect(snapshot)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            sections = {
                section: _read_table(conn, table, columns)
                for section, table, columns in requests
            }
    return version, sections


def build_export(data_dir: Path | None = None) -> dict:
    """삭제 예정 데이터를 담은 문서를 만든다."""
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    document: dict = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "schema_versions": {},
        "sections": {},
    }
    for filename, requests in EXPORTS.items():
        version, sections = _export_database(root / filename, requests)
        document["schema_versions"][filename] = version
        document["sections"].update(sections)
    return document


def write_export(destination: Path, data_dir: Path | None = None) -> Path:
    """기존 파일은 덮어쓰지 않는다. 앞서 받아둔 export를 날리면 안 된다."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"이미 존재하는 파일입니다: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(build_export(data_dir), ensure_ascii=False, indent=2) + "\n"
    destination.write_text(payload, encoding="utf-8")
    return destination


def _default_destination() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return BACKUP_DIR / f"legacy-export-{stamp}.json"


def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_destination()
    written = write_export(destination)
    document = json.loads(written.read_text(encoding="utf-8"))
    for section, rows in document["sections"].items():
        if rows is None:
            print(f"⏭️ {section}: 대상 없음 (테이블/컬럼이 이미 없음)")
        else:
            print(f"📦 {section}: {len(rows)}행")
    print(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
