# Database Repository Layer
#
# 봇의 모든 DB 접근(SQL)을 이 파일에 모아 추상화한다.
# Cog들은 Repository 인터페이스만 사용하므로, 외부 DB로 교체해도 Cog 코드는 바뀌지 않는다.
#
# 외부 DB(MySQL, Oracle 등)로 교체하는 방법:
#   1. UsageRepository / PartyRepository / GuildSettingsRepository /
#      ProfileRepository를 상속한
#      구현 클래스를 이 파일에 작성 (SQL placeholder가 ?가 아닌 %s인 점 등 방언 차이만 처리)
#   2. 아래 create_* 팩토리에 분기 추가
#   3. 환경 변수 DB_BACKEND=mysql, DB_URL=mysql://user:pass@host:3306/botdb 형태로 설정
#
# ── 멀티 길드 ──
# 금지어 카운트·파티·설정·프로필 UID는 스키마의 guild_id로 길드별 격리된다.
# AI 일일 사용량은 하나의 인스턴스 안에서 유저별 전역이므로 guild_id를 저장하지 않는다.
#
# ── 동시성 ──
# 리포지토리 메서드는 모두 동기(blocking)다. discord.py의 이벤트 루프에서 직접
# 호출하면 백업 프로세스와의 락 경합 시 봇 전체가 멈추므로, Cog는 반드시
# run_db()로 감싸 스레드에서 실행한다. 메서드마다 연결을 새로 열고 닫으므로
# 스레드 간 커넥션 공유 문제는 없다.

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from module.config import (
    AI_USAGE_RETENTION_DAYS,
    DATA_DIR,
    DB_BACKEND,
    ensure_private_directory,
    ensure_private_file,
)

# ─────────── 연결 정책 ─────────── #

# 봇과 backup 프로세스가 같은 DB 파일을 연다. 기본 5초로는 백업 중 쓰기가 실패할 수 있다.
SQLITE_TIMEOUT_SECONDS = 30.0


def _secure_sqlite_paths(db_path: pathlib.Path) -> None:
    db_path = pathlib.Path(db_path)
    ensure_private_directory(db_path.parent)
    for path in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ):
        if path.is_symlink():
            raise PermissionError(f"SQLite 경로는 symlink일 수 없습니다: {path}")
        if path.exists():
            ensure_private_file(path)


def _connect(db_path, *, isolation_level: str | None = "") -> sqlite3.Connection:
    """이 모듈의 유일한 SQLite 연결 지점. timeout/journal 정책을 한 곳에 모은다."""
    db_path = pathlib.Path(db_path)
    _secure_sqlite_paths(db_path)
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=isolation_level,
    )
    try:
        ensure_private_file(db_path)
        # journal_mode는 DB 파일에 영속되므로 매 연결 설정은 멱등하다.
        # WAL이면 쓰기 프로세스가 없어도 백업이 읽을 수 있다(단, 디렉터리가 쓰기 가능해야 함).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _secure_sqlite_paths(db_path)
        return conn
    except BaseException:
        conn.close()
        raise


def _schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _prepare_schema(conn: sqlite3.Connection, current: int, label: str) -> int:
    version = _schema_version(conn)
    if version > current:
        raise RuntimeError(
            f"{label} DB 버전 {version}은 이 코드가 지원하는 {current}보다 높습니다."
        )
    return version


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {version}")


def _require_columns(conn: sqlite3.Connection, table: str, expected: set[str]) -> None:
    actual = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if not expected <= actual:
        missing = ", ".join(sorted(expected - actual))
        raise RuntimeError(f"지원하지 않는 {table} 스키마입니다. 누락 컬럼: {missing}")


async def run_db(operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """동기 리포지토리 호출을 스레드로 넘겨 이벤트 루프가 막히지 않게 한다.

    SQLite 쓰기 락은 최대 SQLITE_TIMEOUT_SECONDS(30초)까지 대기할 수 있다.
    이벤트 루프에서 직접 호출하면 그동안 하트비트가 끊겨 봇 전체가 멈춘다.
    """
    return await asyncio.to_thread(operation, *args, **kwargs)


# ─────────── 인터페이스 ─────────── #

class UsageRepository(ABC):
    """길드별 금지어 카운트와 인스턴스 전역 AI 사용량 접근 인터페이스."""

    @abstractmethod
    def increment_forbidden_count(self, guild_id: int, user_id: int) -> None:
        """금지어 경고 횟수를 1 증가시킨다. 유저가 없으면 생성한다."""

    @abstractmethod
    def get_forbidden_count(self, guild_id: int, user_id: int) -> int:
        """금지어 경고 횟수를 반환한다. 미등록 유저는 0."""

    @abstractmethod
    def consume_ai_usage(
        self,
        user_id: int,
        usage_date: str,
        usage_category: str,
        daily_limit: int,
    ) -> Optional[int]:
        """인스턴스 전역 일일 사용량을 원자적으로 예약하고 새 count를 반환한다."""

    @abstractmethod
    def release_ai_usage(
        self, user_id: int, usage_date: str, usage_category: str
    ) -> bool:
        """예약한 일일 사용량을 0 아래로 내리지 않고 반환한다."""

    @abstractmethod
    def get_ai_usage(
        self, user_id: int, usage_date: str, usage_category: str
    ) -> int:
        """인스턴스 전역 일일 사용량을 반환한다."""

    @abstractmethod
    def delete_user(self, guild_id: int, user_id: int) -> None:
        """멤버가 나갔을 때 해당 길드의 금지어 기록을 지운다."""

    @abstractmethod
    def delete_guild(self, guild_id: int) -> None:
        """봇이 길드에서 제거됐을 때 해당 길드 데이터를 모두 지운다."""


class PartyRepository(ABC):
    """게임 파티 모집 데이터 접근 인터페이스. 모두 길드 단위로 격리된다."""

    @abstractmethod
    def get_party(self, guild_id: int, game: str) -> Optional[Tuple[int]]:
        """파티가 존재하면 (created_at,)을, 없으면 None을 반환한다."""

    @abstractmethod
    def create_party(
        self,
        guild_id: int,
        game: str,
        created_at: int,
        host_id: Optional[int] = None,
    ) -> bool:
        """파티를 생성한다. host_id가 있으면 방장 참가까지 한 트랜잭션에서 처리한다."""

    @abstractmethod
    def get_party_host(self, guild_id: int, game: str) -> Optional[int]:
        """저장된 방장 ID를 반환한다. legacy 빈 파티는 None일 수 있다."""

    @abstractmethod
    def delete_party(self, guild_id: int, game: str) -> None:
        """파티를 삭제한다. 참가자도 함께 정리되어야 한다."""

    @abstractmethod
    def get_participants(self, guild_id: int, game: str) -> Dict[int, Optional[str]]:
        """{user_id: role} 형태로 참가자 목록을 반환한다."""

    @abstractmethod
    def add_participant(
        self,
        guild_id: int,
        game: str,
        user_id: int,
        role: Optional[str] = None,
        max_players: Optional[int] = None,
    ) -> bool:
        """존재하는 파티에만 참가자를 추가하거나 역할을 갱신한다.
        길드 내 단일 파티, 정원, 역할 중복은 DB가 원자적으로 거부한다."""

    @abstractmethod
    def remove_participant(self, guild_id: int, game: str, user_id: int) -> bool:
        """참가자를 제거하고 방장 이탈 시 최소 user_id로 이전한다. 파티가 남으면 True."""

    @abstractmethod
    def get_user_party(self, guild_id: int, user_id: int) -> Optional[str]:
        """유저가 그 길드에서 참가 중인 파티의 게임 이름. 없으면 None."""

    @abstractmethod
    def delete_expired_parties(self, cutoff: int) -> List[Tuple[int, str]]:
        """cutoff보다 오래된 파티를 전 길드에서 삭제하고 [(guild_id, game), ...]를 반환한다."""

    @abstractmethod
    def list_expired_parties(self, cutoff: int) -> List[Tuple[int, str]]:
        """삭제하지 않고 만료 후보를 반환한다."""

    @abstractmethod
    def delete_party_if_expired(self, guild_id: int, game: str, cutoff: int) -> bool:
        """현재 incarnation이 여전히 만료됐을 때만 원자적으로 삭제한다."""

    @abstractmethod
    def delete_guild(self, guild_id: int) -> None:
        """봇이 길드에서 제거됐을 때 해당 길드 데이터를 모두 지운다."""


class GuildSettingsRepository(ABC):
    """길드별 봇 설정과 영속 패널 정보."""

    @abstractmethod
    def get_party_channel(self, guild_id: int) -> Optional[int]:
        """파티 패널 채널 ID. 미설정이면 None."""

    @abstractmethod
    def set_party_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        """파티 패널 채널을 지정한다. None이면 해제."""

    @abstractmethod
    def get_announcement_channel(self, guild_id: int) -> Optional[int]:
        """웹 공지를 보낼 채널 ID. 미설정이면 None."""

    @abstractmethod
    def set_announcement_channel(
        self, guild_id: int, channel_id: Optional[int]
    ) -> None:
        """웹 공지 채널을 지정한다. None이면 해제."""

    @abstractmethod
    def get_event_channel(self, guild_id: int) -> Optional[int]:
        """`/이벤트` 전용 채널 ID. 미설정이면 None."""

    @abstractmethod
    def set_event_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        """`/이벤트` 전용 채널을 지정한다. None이면 채널 제한 해제."""

    @abstractmethod
    def get_allow_host_announce(self, guild_id: int) -> bool:
        """파티 호스트 공지를 허용하는지 반환한다."""

    @abstractmethod
    def set_allow_host_announce(self, guild_id: int, allowed: bool) -> None:
        """파티 호스트 공지 허용 여부를 지정한다."""

    @abstractmethod
    def get_forbidden_filter_enabled(self, guild_id: int) -> bool:
        """금지어 필터를 켜 두었는지 반환한다. 미설정 길드는 켜짐."""

    @abstractmethod
    def set_forbidden_filter_enabled(self, guild_id: int, enabled: bool) -> None:
        """금지어 필터 사용 여부를 지정한다."""

    @abstractmethod
    def set_guild_settings(
        self,
        guild_id: int,
        party_channel_id: Optional[int],
        announcement_channel_id: Optional[int],
        event_channel_id: Optional[int],
        allow_host_announce: bool,
        forbidden_filter_enabled: bool,
    ) -> None:
        """웹 관리에서 다루는 길드 설정을 한 트랜잭션으로 저장한다."""

    @abstractmethod
    def get_party_panels(self, guild_id: int) -> Dict[str, int]:
        """게임별 영속 파티 패널 메시지 ID를 반환한다."""

    @abstractmethod
    def set_party_panel(self, guild_id: int, game: str, message_id: int) -> None:
        """게임의 영속 파티 패널 메시지 ID를 저장한다."""

    @abstractmethod
    def delete_party_panel(self, guild_id: int, game: str) -> None:
        """게임의 영속 파티 패널 정보를 지운다."""

    @abstractmethod
    def clear_channel(self, guild_id: int, channel_id: int) -> None:
        """삭제된 채널을 가리키는 설정을 해제한다."""

    @abstractmethod
    def list_announcement_guild_ids(self) -> List[int]:
        """호스트 공지를 허용한 길드 ID를 반환한다."""

    @abstractmethod
    def delete_guild(self, guild_id: int) -> None:
        """봇이 길드에서 제거됐을 때 해당 길드 설정을 지운다."""


class ProfileRepository(ABC):
    """게임 프로필 UID 매핑. 길드별로 격리된다.

    같은 사람이 서버마다 다른 UID를 쓸 수 있고, 어느 서버에 등록했는지가
    다른 서버로 새면 안 된다. 인스턴스 전역으로 두지 않는 이유다.
    """

    @abstractmethod
    def get_uid(self, guild_id: int, user_id: int, game: str) -> Optional[str]:
        """등록한 UID. 없으면 None."""

    @abstractmethod
    def set_uid(self, guild_id: int, user_id: int, game: str, uid: str) -> None:
        """UID를 등록하거나 교체한다."""

    @abstractmethod
    def delete_uid(self, guild_id: int, user_id: int, game: str) -> bool:
        """등록을 해제한다. 지운 게 있으면 True."""

    @abstractmethod
    def list_uids(self, guild_id: int, user_id: int) -> Dict[str, str]:
        """이 서버에서 이 유저가 등록한 게임별 UID."""

    @abstractmethod
    def delete_user(self, guild_id: int, user_id: int) -> None:
        """멤버가 나갔을 때 해당 길드의 UID 등록을 모두 지운다."""

    @abstractmethod
    def delete_guild(self, guild_id: int) -> None:
        """봇이 길드에서 제거됐을 때 해당 길드 등록을 모두 지운다."""


# ─────────── SQLite 구현 ─────────── #

class SQLiteUsageRepository(UsageRepository):
    _SCHEMA_VERSION = 3

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self._init_db()
        _secure_sqlite_paths(self.db_path)

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            version = _prepare_schema(conn, self._SCHEMA_VERSION, "usage")
            cursor = conn.cursor()
            # (guild_id, user_id)가 기본키다. 금지어 카운트는 서버마다 독립이다.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    forbidden_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ai_usage (
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    command TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK (count >= 0),
                    PRIMARY KEY (user_id, usage_date, command)
                )
            """)
            if version < self._SCHEMA_VERSION:
                self._drop_point_economy(conn)
            _require_columns(
                conn,
                "ai_usage",
                {"user_id", "usage_date", "command", "count"},
            )
            _set_schema_version(conn, self._SCHEMA_VERSION)
            conn.commit()

    def consume_ai_usage(
        self,
        user_id: int,
        usage_date: str,
        usage_category: str,
        daily_limit: int,
    ) -> Optional[int]:
        cutoff = (
            date.fromisoformat(usage_date)
            - timedelta(days=AI_USAGE_RETENTION_DAYS - 1)
        ).isoformat()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("DELETE FROM ai_usage WHERE usage_date < ?", (cutoff,))
            cursor = conn.execute(
                """
                INSERT INTO ai_usage (user_id, usage_date, command, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, usage_date, command) DO UPDATE SET count = count + 1
                WHERE count < ?
                RETURNING count
                """,
                (user_id, usage_date, usage_category, daily_limit),
            )
            row = cursor.fetchone()
            conn.commit()
            return row[0] if row else None

    def release_ai_usage(
        self, user_id: int, usage_date: str, usage_category: str
    ) -> bool:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                "UPDATE ai_usage SET count = count - 1 "
                "WHERE user_id = ? AND usage_date = ? AND command = ? AND count > 0",
                (user_id, usage_date, usage_category),
            )
            released = cursor.rowcount > 0
            if released:
                conn.execute(
                    "DELETE FROM ai_usage "
                    "WHERE user_id = ? AND usage_date = ? AND command = ? AND count = 0",
                    (user_id, usage_date, usage_category),
                )
            conn.commit()
            return released

    def get_ai_usage(
        self, user_id: int, usage_date: str, usage_category: str
    ) -> int:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT count FROM ai_usage "
                "WHERE user_id = ? AND usage_date = ? AND command = ?",
                (user_id, usage_date, usage_category),
            ).fetchone()
            conn.commit()
            return row[0] if row else 0

    @staticmethod
    def _drop_point_economy(conn: sqlite3.Connection) -> None:
        """v3: 포인트 경제를 버린다. users는 금지어 카운트만 남긴다.

        컬럼을 빼려면 테이블을 다시 만들어야 한다(guild_settings와 같은 이유로
        DROP COLUMN을 쓰지 않는다). 마이그레이션 전에 python -m module.export_legacy로
        잔액과 원장을 JSON으로 받아둘 수 있다.
        """
        columns = {row[1] for row in conn.execute('PRAGMA table_info("users")')}
        if {"points", "last_attendance_date"} & columns:
            conn.execute("ALTER TABLE users RENAME TO users_v2")
            conn.execute("""
                CREATE TABLE users (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    forbidden_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            conn.execute(
                "INSERT INTO users (guild_id, user_id, forbidden_count) "
                "SELECT guild_id, user_id, forbidden_count FROM users_v2"
            )
            conn.execute("DROP TABLE users_v2")
        conn.execute("DROP INDEX IF EXISTS idx_ledger_user")
        conn.execute("DROP TABLE IF EXISTS point_ledger")

    def increment_forbidden_count(self, guild_id: int, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, forbidden_count) "
                "VALUES (?, ?, 0)",
                (guild_id, user_id),
            )
            cursor.execute(
                "UPDATE users SET forbidden_count = forbidden_count + 1 "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            conn.commit()

    def get_forbidden_count(self, guild_id: int, user_id: int) -> int:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT forbidden_count FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def delete_guild(self, guild_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE guild_id = ?", (guild_id,))
            conn.commit()

    def delete_user(self, guild_id: int, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            conn.commit()


class SQLitePartyRepository(PartyRepository):
    _SCHEMA_VERSION = 2

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self._init_db()
        _secure_sqlite_paths(self.db_path)

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            version = _prepare_schema(conn, self._SCHEMA_VERSION, "party")
            cursor = conn.cursor()
            # 길드당 게임별로 파티 하나.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    guild_id INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    host_id INTEGER,
                    PRIMARY KEY (guild_id, game)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS participants (
                    guild_id INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    role TEXT,
                    PRIMARY KEY (guild_id, game, user_id)
                )
            """)
            if version < 2 and "host_id" not in {
                row[1] for row in cursor.execute("PRAGMA table_info(parties)")
            }:
                cursor.execute("ALTER TABLE parties ADD COLUMN host_id INTEGER")

            # v1은 한 유저가 여러 게임에 들어갈 수 있었다. game 이름 순으로 하나만 보존한다.
            if version < 2:
                cursor.execute("""
                    DELETE FROM participants
                    WHERE game != (
                        SELECT MIN(other.game)
                        FROM participants AS other
                        WHERE other.guild_id = participants.guild_id
                          AND other.user_id = participants.user_id
                    )
                """)
                cursor.execute("""
                    UPDATE parties
                    SET host_id = (
                        SELECT MIN(user_id)
                        FROM participants
                        WHERE participants.guild_id = parties.guild_id
                          AND participants.game = parties.game
                    )
                """)

            # 역할과 길드 내 단일 활성 파티는 DB가 최종 판정한다.
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_role
                ON participants (guild_id, game, role) WHERE role IS NOT NULL
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_user
                ON participants (guild_id, user_id)
            """)
            _require_columns(
                conn, "parties", {"guild_id", "game", "created_at", "host_id"}
            )
            _require_columns(conn, "participants", {"guild_id", "game", "user_id", "role"})
            _set_schema_version(conn, self._SCHEMA_VERSION)
            conn.commit()

    def get_party(self, guild_id: int, game: str) -> Optional[Tuple[int]]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT created_at FROM parties WHERE guild_id = ? AND game = ?",
                (guild_id, game),
            )
            return cursor.fetchone()

    def create_party(
        self,
        guild_id: int,
        game: str,
        created_at: int,
        host_id: Optional[int] = None,
    ) -> bool:
        conn = _connect(self.db_path, isolation_level=None)
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "INSERT OR IGNORE INTO parties (guild_id, game, created_at, host_id) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, game, created_at, host_id),
            )
            created = cursor.rowcount > 0
            if created and host_id is not None:
                cursor.execute(
                    "INSERT INTO participants (guild_id, game, user_id, role) "
                    "VALUES (?, ?, ?, NULL)",
                    (guild_id, game, host_id),
                )
            conn.commit()
            return created
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_party_host(self, guild_id: int, game: str) -> Optional[int]:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT host_id FROM parties WHERE guild_id = ? AND game = ?",
                (guild_id, game),
            ).fetchone()
            return row[0] if row else None

    def delete_party(self, guild_id: int, game: str) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM participants WHERE guild_id = ? AND game = ?", (guild_id, game)
            )
            cursor.execute(
                "DELETE FROM parties WHERE guild_id = ? AND game = ?", (guild_id, game)
            )
            conn.commit()

    def get_participants(self, guild_id: int, game: str) -> Dict[int, Optional[str]]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, role FROM participants WHERE guild_id = ? AND game = ?",
                (guild_id, game),
            )
            return {row[0]: row[1] for row in cursor.fetchall()}

    def add_participant(
        self,
        guild_id: int,
        game: str,
        user_id: int,
        role: Optional[str] = None,
        max_players: Optional[int] = None,
    ) -> bool:
        # 정원 확인과 쓰기를 BEGIN IMMEDIATE로 묶고, 역할 중복은 유니크 인덱스가 거부한다.
        conn = _connect(self.db_path, isolation_level=None)
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            if not cursor.execute(
                "SELECT 1 FROM parties WHERE guild_id = ? AND game = ?", (guild_id, game)
            ).fetchone():
                conn.rollback()
                return False

            if max_players is not None:
                others = cursor.execute(
                    "SELECT COUNT(*) FROM participants "
                    "WHERE guild_id = ? AND game = ? AND user_id != ?",
                    (guild_id, game, user_id),
                ).fetchone()[0]
                if others >= max_players:
                    conn.rollback()
                    return False

            # 역할 변경은 같은 user_id의 기존 행을 지우고 다시 넣는다.
            cursor.execute(
                "DELETE FROM participants WHERE guild_id = ? AND game = ? AND user_id = ?",
                (guild_id, game, user_id),
            )
            cursor.execute(
                "INSERT INTO participants (guild_id, game, user_id, role) VALUES (?, ?, ?, ?)",
                (guild_id, game, user_id, role),
            )
            cursor.execute(
                "UPDATE parties SET host_id = COALESCE(host_id, ?) "
                "WHERE guild_id = ? AND game = ?",
                (user_id, guild_id, game),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()  # 역할 중복 — 지웠던 자기 행도 함께 복원된다.
            return False
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def remove_participant(self, guild_id: int, game: str, user_id: int) -> bool:
        conn = _connect(self.db_path, isolation_level=None)
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "DELETE FROM participants WHERE guild_id = ? AND game = ? AND user_id = ?",
                (guild_id, game, user_id),
            )
            next_host = cursor.execute(
                "SELECT MIN(user_id) FROM participants WHERE guild_id = ? AND game = ?",
                (guild_id, game),
            ).fetchone()[0]
            if next_host is None:
                cursor.execute(
                    "DELETE FROM parties WHERE guild_id = ? AND game = ?",
                    (guild_id, game),
                )
            else:
                cursor.execute(
                    "UPDATE parties SET host_id = ? "
                    "WHERE guild_id = ? AND game = ? AND host_id = ?",
                    (next_host, guild_id, game, user_id),
                )
            conn.commit()
            return next_host is not None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_user_party(self, guild_id: int, user_id: int) -> Optional[str]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT game FROM participants WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def delete_expired_parties(self, cutoff: int) -> List[Tuple[int, str]]:
        return [
            key
            for key in self.list_expired_parties(cutoff)
            if self.delete_party_if_expired(key[0], key[1], cutoff)
        ]

    def list_expired_parties(self, cutoff: int) -> List[Tuple[int, str]]:
        with closing(_connect(self.db_path)) as conn:
            return [
                (guild_id, game)
                for guild_id, game in conn.execute(
                    "SELECT guild_id, game FROM parties WHERE created_at < ?", (cutoff,)
                )
            ]

    def delete_party_if_expired(self, guild_id: int, game: str, cutoff: int) -> bool:
        conn = _connect(self.db_path, isolation_level=None)
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            expired = cursor.execute(
                "SELECT 1 FROM parties "
                "WHERE guild_id = ? AND game = ? AND created_at < ?",
                (guild_id, game, cutoff),
            ).fetchone()
            if not expired:
                conn.rollback()
                return False
            cursor.execute(
                "DELETE FROM participants WHERE guild_id = ? AND game = ?",
                (guild_id, game),
            )
            cursor.execute(
                "DELETE FROM parties WHERE guild_id = ? AND game = ? AND created_at < ?",
                (guild_id, game, cutoff),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_guild(self, guild_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM participants WHERE guild_id = ?", (guild_id,))
            cursor.execute("DELETE FROM parties WHERE guild_id = ?", (guild_id,))
            conn.commit()


class SQLiteGuildSettingsRepository(GuildSettingsRepository):
    # 컬럼명은 아래 상수에서만 나온다. 외부 입력이 SQL 문자열에 섞이지 않는다.
    _PARTY_CHANNEL = "party_channel_id"
    _ANNOUNCEMENT_CHANNEL = "announcement_channel_id"
    _EVENT_CHANNEL = "event_channel_id"
    _ALLOW_HOST_ANNOUNCE = "allow_host_announce"
    _FORBIDDEN_FILTER = "forbidden_filter_enabled"
    _SCHEMA_VERSION = 6
    _CURRENT_COLUMNS = {
        "guild_id",
        _PARTY_CHANNEL,
        _ALLOW_HOST_ANNOUNCE,
        _FORBIDDEN_FILTER,
        _ANNOUNCEMENT_CHANNEL,
        _EVENT_CHANNEL,
    }
    _LEGACY_COLUMNS = {"guild_id", "recruit_channel_id", "event_channel_id"}
    # v2는 음악 컬럼 둘을 더 갖고 있었다. v3는 v4에서 토글만 빠진 모양이다.
    # 옮겨 담는 컬럼이 양쪽 다 같으므로 한 분기로 처리한다. 부분집합이 아니라
    # 정확히 일치할 때만 받는다 — 모르는 컬럼이 붙은 DB는 계약 위반으로 거부한다.
    _V2_COLUMNS = {
        "guild_id",
        _PARTY_CHANNEL,
        "music_channel_id",
        "music_panel_msg_id",
        _ALLOW_HOST_ANNOUNCE,
    }
    _V3_COLUMNS = {
        "guild_id",
        _PARTY_CHANNEL,
        _ALLOW_HOST_ANNOUNCE,
    }
    _V4_COLUMNS = {
        "guild_id",
        _PARTY_CHANNEL,
        _ALLOW_HOST_ANNOUNCE,
        _FORBIDDEN_FILTER,
    }
    _V5_COLUMNS = _V4_COLUMNS | {_ANNOUNCEMENT_CHANNEL}
    _GUILD_SETTINGS_COLUMN_ORDER = (
        "guild_id",
        _PARTY_CHANNEL,
        _ALLOW_HOST_ANNOUNCE,
        _FORBIDDEN_FILTER,
        _ANNOUNCEMENT_CHANNEL,
        _EVENT_CHANNEL,
    )
    _PARTY_PANELS_COLUMN_ORDER = ("guild_id", "game", "message_id")

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self._init_db()
        _secure_sqlite_paths(self.db_path)

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            version = _prepare_schema(conn, self._SCHEMA_VERSION, "settings")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "guild_settings" not in tables:
                if version:
                    raise RuntimeError("지원하지 않는 guild_settings 스키마입니다.")
                with conn:
                    self._create_current_tables(conn)
                    _set_schema_version(conn, self._SCHEMA_VERSION)
                return

            columns = {
                row[1] for row in conn.execute('PRAGMA table_info("guild_settings")')
            }
            if version < self._SCHEMA_VERSION and columns == self._V5_COLUMNS:
                with conn:
                    conn.execute(
                        "ALTER TABLE guild_settings ADD COLUMN event_channel_id INTEGER"
                    )
                    self._create_party_panels_table(conn)
                    self._require_current_contract(conn)
                    _set_schema_version(conn, self._SCHEMA_VERSION)
                return
            if version < self._SCHEMA_VERSION and columns == self._V4_COLUMNS:
                with conn:
                    conn.execute(
                        "ALTER TABLE guild_settings ADD COLUMN announcement_channel_id INTEGER"
                    )
                    conn.execute(
                        "ALTER TABLE guild_settings ADD COLUMN event_channel_id INTEGER"
                    )
                    self._create_party_panels_table(conn)
                    self._require_current_contract(conn)
                    _set_schema_version(conn, self._SCHEMA_VERSION)
                return
            if version < self._SCHEMA_VERSION and self._LEGACY_COLUMNS <= columns:
                # v1: recruit_channel_id → party_channel_id
                self._rebuild_guild_settings(
                    conn,
                    "guild_settings_v1",
                    """
                    INSERT INTO guild_settings
                        (guild_id, party_channel_id, event_channel_id)
                    SELECT guild_id, recruit_channel_id, event_channel_id
                    FROM guild_settings_v1
                    """,
                )
                return

            if version < self._SCHEMA_VERSION and columns in (
                self._V2_COLUMNS,
                self._V3_COLUMNS,
            ):
                # v2: 음악 컬럼을 버린다. v3: forbidden_filter_enabled를 더한다.
                # 옮겨 담는 컬럼이 같아 한 분기로 충분하다. 새 컬럼은 DEFAULT 1이
                # 채우므로 기존 서버의 금지어 필터는 켜진 상태 그대로 넘어온다.
                # SQLite DROP COLUMN 대신 v1과 같은 rename-and-copy를 쓴다.
                self._rebuild_guild_settings(
                    conn,
                    "guild_settings_old",
                    """
                    INSERT INTO guild_settings
                        (guild_id, party_channel_id, allow_host_announce)
                    SELECT guild_id, party_channel_id, allow_host_announce
                    FROM guild_settings_old
                    """,
                )
                return

            with conn:
                self._create_party_panels_table(conn)
                self._require_current_contract(conn)
                _set_schema_version(conn, self._SCHEMA_VERSION)

    def _rebuild_guild_settings(
        self, conn: sqlite3.Connection, staging: str, copy_sql: str
    ) -> None:
        """현재 스키마로 테이블을 다시 만들고 옮긴다. 실패하면 원본을 되돌린다."""
        conn.execute("BEGIN")
        try:
            conn.execute(f"ALTER TABLE guild_settings RENAME TO {staging}")
            self._create_current_tables(conn)
            conn.execute(copy_sql)
            conn.execute(f"DROP TABLE {staging}")
            self._require_current_contract(conn)
            _set_schema_version(conn, self._SCHEMA_VERSION)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    @classmethod
    def _require_current_contract(cls, conn: sqlite3.Connection) -> None:
        cls._require_table_contract(
            conn,
            "guild_settings",
            cls._GUILD_SETTINGS_COLUMN_ORDER,
            ("guild_id",),
        )
        cls._require_table_contract(
            conn,
            "party_panels",
            cls._PARTY_PANELS_COLUMN_ORDER,
            ("guild_id", "game"),
        )

    @staticmethod
    def _require_table_contract(
        conn: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
        primary_key: tuple[str, ...],
    ) -> None:
        actual = list(conn.execute(f'PRAGMA table_info("{table}")'))
        actual_columns = tuple(row[1] for row in actual)
        actual_primary_key = tuple(
            row[1] for row in sorted(actual, key=lambda row: row[5]) if row[5]
        )
        if actual_columns != columns or actual_primary_key != primary_key:
            raise RuntimeError(f"지원하지 않는 {table} 스키마입니다.")

    @staticmethod
    def _create_current_tables(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                party_channel_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0
                    CHECK (allow_host_announce IN (0, 1)),
                forbidden_filter_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (forbidden_filter_enabled IN (0, 1)),
                announcement_channel_id INTEGER,
                event_channel_id INTEGER
            )
        """)
        SQLiteGuildSettingsRepository._create_party_panels_table(conn)

    @staticmethod
    def _create_party_panels_table(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS party_panels (
                guild_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, game)
            )
        """)

    def _get_column(self, guild_id: int, column: str) -> Optional[int]:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT {column} FROM guild_settings WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            return row[0] if row and row[0] else None

    def _set_column(self, guild_id: int, column: str, channel_id: Optional[int]) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                f"""
                INSERT INTO guild_settings (guild_id, {column}) VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET {column} = excluded.{column}
                """,
                (guild_id, channel_id),
            )
            conn.commit()

    def get_party_channel(self, guild_id: int) -> Optional[int]:
        return self._get_column(guild_id, self._PARTY_CHANNEL)

    def set_party_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        self._set_column(guild_id, self._PARTY_CHANNEL, channel_id)

    def get_announcement_channel(self, guild_id: int) -> Optional[int]:
        return self._get_column(guild_id, self._ANNOUNCEMENT_CHANNEL)

    def set_announcement_channel(
        self, guild_id: int, channel_id: Optional[int]
    ) -> None:
        self._set_column(guild_id, self._ANNOUNCEMENT_CHANNEL, channel_id)

    def get_event_channel(self, guild_id: int) -> Optional[int]:
        return self._get_column(guild_id, self._EVENT_CHANNEL)

    def set_event_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        self._set_column(guild_id, self._EVENT_CHANNEL, channel_id)

    def get_allow_host_announce(self, guild_id: int) -> bool:
        return bool(self._get_column(guild_id, self._ALLOW_HOST_ANNOUNCE))

    def set_allow_host_announce(self, guild_id: int, allowed: bool) -> None:
        self._set_column(guild_id, self._ALLOW_HOST_ANNOUNCE, int(allowed))

    def get_forbidden_filter_enabled(self, guild_id: int) -> bool:
        # 미등록 길드는 행이 없다. 기본값은 켜짐이라 _get_column의 falsy 처리에
        # 기댈 수 없으므로 여기서만 직접 읽는다.
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT {self._FORBIDDEN_FILTER} FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            return True if row is None else bool(row[0])

    def set_forbidden_filter_enabled(self, guild_id: int, enabled: bool) -> None:
        self._set_column(guild_id, self._FORBIDDEN_FILTER, int(enabled))

    def set_guild_settings(
        self,
        guild_id: int,
        party_channel_id: Optional[int],
        announcement_channel_id: Optional[int],
        event_channel_id: Optional[int],
        allow_host_announce: bool,
        forbidden_filter_enabled: bool,
    ) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, party_channel_id, announcement_channel_id,
                    event_channel_id, allow_host_announce,
                    forbidden_filter_enabled
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    party_channel_id = excluded.party_channel_id,
                    announcement_channel_id = excluded.announcement_channel_id,
                    event_channel_id = excluded.event_channel_id,
                    allow_host_announce = excluded.allow_host_announce,
                    forbidden_filter_enabled = excluded.forbidden_filter_enabled
                """,
                (
                    guild_id,
                    party_channel_id,
                    announcement_channel_id,
                    event_channel_id,
                    int(allow_host_announce),
                    int(forbidden_filter_enabled),
                ),
            )
            conn.commit()

    def get_party_panels(self, guild_id: int) -> Dict[str, int]:
        with closing(_connect(self.db_path)) as conn:
            return {
                game: message_id
                for game, message_id in conn.execute(
                    "SELECT game, message_id FROM party_panels WHERE guild_id = ?",
                    (guild_id,),
                )
            }

    def set_party_panel(self, guild_id: int, game: str, message_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO party_panels (guild_id, game, message_id) VALUES (?, ?, ?)
                ON CONFLICT(guild_id, game) DO UPDATE SET message_id = excluded.message_id
                """,
                (guild_id, game, message_id),
            )
            conn.commit()

    def delete_party_panel(self, guild_id: int, game: str) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM party_panels WHERE guild_id = ? AND game = ?",
                (guild_id, game),
            )
            conn.commit()

    def clear_channel(self, guild_id: int, channel_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                """
                UPDATE guild_settings
                SET party_channel_id = CASE
                        WHEN party_channel_id = ? THEN NULL ELSE party_channel_id END,
                    announcement_channel_id = CASE
                        WHEN announcement_channel_id = ? THEN NULL
                        ELSE announcement_channel_id END,
                    event_channel_id = CASE
                        WHEN event_channel_id = ? THEN NULL ELSE event_channel_id END
                WHERE guild_id = ?
                  AND (party_channel_id = ? OR announcement_channel_id = ?
                       OR event_channel_id = ?)
                """,
                (
                    channel_id,
                    channel_id,
                    channel_id,
                    guild_id,
                    channel_id,
                    channel_id,
                    channel_id,
                ),
            )
            conn.commit()

    def list_announcement_guild_ids(self) -> List[int]:
        with closing(_connect(self.db_path)) as conn:
            return [
                guild_id
                for (guild_id,) in conn.execute(
                    "SELECT guild_id FROM guild_settings WHERE allow_host_announce = 1"
                )
            ]

    def delete_guild(self, guild_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute("DELETE FROM party_panels WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
            conn.commit()


class SQLiteProfileRepository(ProfileRepository):
    _SCHEMA_VERSION = 1
    _COLUMN_ORDER = ("guild_id", "user_id", "game", "uid")

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self._init_db()
        _secure_sqlite_paths(self.db_path)

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            _prepare_schema(conn, self._SCHEMA_VERSION, "profile")
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS game_uids (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        game TEXT NOT NULL,
                        uid TEXT NOT NULL,
                        PRIMARY KEY (guild_id, user_id, game)
                    )
                """)
                actual = list(conn.execute('PRAGMA table_info("game_uids")'))
                columns = tuple(row[1] for row in actual)
                primary_key = tuple(
                    row[1] for row in sorted(actual, key=lambda row: row[5]) if row[5]
                )
                if columns != self._COLUMN_ORDER or primary_key != (
                    "guild_id",
                    "user_id",
                    "game",
                ):
                    raise RuntimeError("지원하지 않는 game_uids 스키마입니다.")
                _set_schema_version(conn, self._SCHEMA_VERSION)

    def get_uid(self, guild_id: int, user_id: int, game: str) -> Optional[str]:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT uid FROM game_uids WHERE guild_id = ? AND user_id = ? AND game = ?",
                (guild_id, user_id, game),
            ).fetchone()
            return row[0] if row else None

    def set_uid(self, guild_id: int, user_id: int, game: str, uid: str) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO game_uids (guild_id, user_id, game, uid) VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, game) DO UPDATE SET uid = excluded.uid
                """,
                (guild_id, user_id, game, uid),
            )
            conn.commit()

    def delete_uid(self, guild_id: int, user_id: int, game: str) -> bool:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                "DELETE FROM game_uids WHERE guild_id = ? AND user_id = ? AND game = ?",
                (guild_id, user_id, game),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_uids(self, guild_id: int, user_id: int) -> Dict[str, str]:
        with closing(_connect(self.db_path)) as conn:
            return {
                game: uid
                for game, uid in conn.execute(
                    "SELECT game, uid FROM game_uids WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            }

    def delete_user(self, guild_id: int, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM game_uids WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            conn.commit()

    def delete_guild(self, guild_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute("DELETE FROM game_uids WHERE guild_id = ?", (guild_id,))
            conn.commit()


# ─────────── 팩토리 ─────────── #

_UNSUPPORTED_MSG = (
    "지원하지 않는 DB 백엔드입니다: '{backend}'. "
    "module/database.py에 {repo} 구현을 추가하고 팩토리에 분기를 등록하세요."
)

def create_usage_repository() -> UsageRepository:
    if DB_BACKEND == "sqlite":
        return SQLiteUsageRepository(DATA_DIR / "attendance_data.db")
    # 외부 DB 분기 예시:
    # if DB_BACKEND == "mysql":
    #     return MySQLUsageRepository(DB_URL)
    raise NotImplementedError(_UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="UsageRepository"))


def create_party_repository() -> PartyRepository:
    if DB_BACKEND == "sqlite":
        return SQLitePartyRepository(DATA_DIR / "party_data.db")
    raise NotImplementedError(_UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="PartyRepository"))


def create_profile_repository() -> ProfileRepository:
    if DB_BACKEND == "sqlite":
        return SQLiteProfileRepository(DATA_DIR / "profile_data.db")
    raise NotImplementedError(
        _UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="ProfileRepository")
    )


def create_guild_settings_repository() -> GuildSettingsRepository:
    if DB_BACKEND == "sqlite":
        return SQLiteGuildSettingsRepository(DATA_DIR / "guild_settings.db")
    raise NotImplementedError(
        _UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="GuildSettingsRepository")
    )
