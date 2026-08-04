# Database Repository Layer
#
# 봇의 모든 DB 접근(SQL)을 이 파일에 모아 추상화한다.
# Cog들은 Repository 인터페이스만 사용하므로, 외부 DB로 교체해도 Cog 코드는 바뀌지 않는다.
#
# 외부 DB(MySQL, Oracle 등)로 교체하는 방법:
#   1. AttendanceRepository / PartyRepository / GuildSettingsRepository를 상속한
#      구현 클래스를 이 파일에 작성 (SQL placeholder가 ?가 아닌 %s인 점 등 방언 차이만 처리)
#   2. 아래 create_* 팩토리에 분기 추가
#   3. 환경 변수 DB_BACKEND=mysql, DB_URL=mysql://user:pass@host:3306/botdb 형태로 설정
#
# ── 멀티 길드 ──
# 포인트·출석·금지어·파티·설정은 스키마의 guild_id로 길드별 격리된다.
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
import time
from abc import ABC, abstractmethod
from contextlib import closing
from typing import Any, Callable, Dict, List, Optional, Tuple
from module.config import DATA_DIR, DB_BACKEND


# ─────────── 연결 정책 ─────────── #

# 봇과 backup 프로세스가 같은 DB 파일을 연다. 기본 5초로는 백업 중 쓰기가 실패할 수 있다.
SQLITE_TIMEOUT_SECONDS = 30.0


def _connect(db_path, *, isolation_level: str | None = "") -> sqlite3.Connection:
    """이 모듈의 유일한 SQLite 연결 지점. timeout/journal 정책을 한 곳에 모은다."""
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=isolation_level,
    )
    # journal_mode는 DB 파일에 영속되므로 매 연결 설정은 멱등하다.
    # WAL이면 쓰기 프로세스가 없어도 백업이 읽을 수 있다(단, 디렉터리가 쓰기 가능해야 함).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


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


async def run_db(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """동기 리포지토리 호출을 스레드로 넘겨 이벤트 루프가 막히지 않게 한다.

    SQLite 쓰기 락은 최대 SQLITE_TIMEOUT_SECONDS(30초)까지 대기할 수 있다.
    이벤트 루프에서 직접 호출하면 그동안 하트비트가 끊겨 봇 전체가 멈춘다.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


def _record_ledger(
    cursor: sqlite3.Cursor, guild_id: int, user_id: int, delta: int, reason: str
) -> None:
    """포인트를 실제로 변경한 트랜잭션 안에서만 호출한다."""
    cursor.execute(
        "INSERT INTO point_ledger (guild_id, user_id, delta, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, delta, reason, int(time.time())),
    )


# ─────────── 인터페이스 ─────────── #

class AttendanceRepository(ABC):
    """길드별 출석/포인트와 인스턴스 전역 AI 사용량 접근 인터페이스."""

    @abstractmethod
    def get_points(self, guild_id: int, user_id: int) -> int:
        """해당 길드에서의 포인트 잔액. 미등록 유저는 0."""

    @abstractmethod
    def add_points(
        self, guild_id: int, user_id: int, amount: int, reason: str = "unspecified"
    ) -> None:
        """포인트를 지급하고 원장에 기록한다."""

    @abstractmethod
    def deduct_points(
        self, guild_id: int, user_id: int, amount: int, reason: str = "unspecified"
    ) -> bool:
        """포인트를 차감한다. 잔액 확인과 차감이 원자적으로 수행되어야 한다.
        성공 시 True(원장 기록), 잔액 부족 시 False(원장 미기록)."""

    @abstractmethod
    def claim_attendance(
        self,
        guild_id: int,
        user_id: int,
        reward: int,
        attendance_date: str,
        reason: str = "attendance",
    ) -> Optional[int]:
        """당일 첫 출석이면 포인트를 지급하고 새 잔액을, 중복이면 None을 반환한다."""

    @abstractmethod
    def get_ledger(
        self, guild_id: int, user_id: int, limit: int = 20
    ) -> List[Tuple[int, str, int]]:
        """최근 포인트 이동 [(delta, reason, created_at), ...]를 최신순으로 반환한다."""

    @abstractmethod
    def increment_forbidden_count(self, guild_id: int, user_id: int) -> None:
        """금지어 경고 횟수를 1 증가시킨다. 유저가 없으면 생성한다."""

    @abstractmethod
    def get_forbidden_count(self, guild_id: int, user_id: int) -> int:
        """금지어 경고 횟수를 반환한다. 미등록 유저는 0."""

    @abstractmethod
    def get_top_rankings(self, guild_id: int, limit: int = 5) -> List[Tuple[int, int]]:
        """해당 길드의 포인트 상위 유저 [(user_id, points), ...]."""

    @abstractmethod
    def consume_ai_usage(
        self, user_id: int, usage_date: str, command: str, limit: int
    ) -> Optional[int]:
        """인스턴스 전역 일일 사용량을 원자적으로 예약하고 새 count를 반환한다."""

    @abstractmethod
    def release_ai_usage(self, user_id: int, usage_date: str, command: str) -> bool:
        """예약한 일일 사용량을 0 아래로 내리지 않고 반환한다."""

    @abstractmethod
    def get_ai_usage(self, user_id: int, usage_date: str, command: str) -> int:
        """인스턴스 전역 일일 사용량을 반환한다."""

    @abstractmethod
    def delete_guild(self, guild_id: int) -> None:
        """봇이 길드에서 제거됐을 때 해당 길드 데이터를 모두 지운다."""


class PartyRepository(ABC):
    """게임 파티 모집 데이터 접근 인터페이스. 모두 길드 단위로 격리된다."""

    @abstractmethod
    def get_party(self, guild_id: int, game: str) -> Optional[Tuple[int]]:
        """파티가 존재하면 (created_at,)을, 없으면 None을 반환한다."""

    @abstractmethod
    def create_party(self, guild_id: int, game: str, created_at: int) -> bool:
        """파티를 생성한다. 새로 만들었으면 True, 이미 있으면 False."""

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
        정원 초과와 역할 중복은 DB가 거부하며, 그 경우 행을 남기지 않는다."""

    @abstractmethod
    def remove_participant(self, guild_id: int, game: str, user_id: int) -> None:
        """참가자를 제거한다."""

    @abstractmethod
    def get_user_party(self, guild_id: int, user_id: int) -> Optional[str]:
        """유저가 그 길드에서 참가 중인 파티의 게임 이름. 없으면 None."""

    @abstractmethod
    def delete_expired_parties(self, cutoff: int) -> List[Tuple[int, str]]:
        """cutoff보다 오래된 파티를 전 길드에서 삭제하고 [(guild_id, game), ...]를 반환한다."""

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
    def get_music_channel(self, guild_id: int) -> Optional[int]:
        """음악 패널 채널 ID. 미설정이면 None."""

    @abstractmethod
    def set_music_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        """음악 패널 채널을 지정한다. None이면 해제."""

    @abstractmethod
    def get_music_panel_msg(self, guild_id: int) -> Optional[int]:
        """음악 패널 메시지 ID. 미설정이면 None."""

    @abstractmethod
    def set_music_panel_msg(self, guild_id: int, message_id: Optional[int]) -> None:
        """음악 패널 메시지 ID를 지정한다. None이면 해제."""

    @abstractmethod
    def get_allow_host_announce(self, guild_id: int) -> bool:
        """파티 호스트 공지를 허용하는지 반환한다."""

    @abstractmethod
    def set_allow_host_announce(self, guild_id: int, allowed: bool) -> None:
        """파티 호스트 공지 허용 여부를 지정한다."""

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


# ─────────── SQLite 구현 ─────────── #

class SQLiteAttendanceRepository(AttendanceRepository):
    _SCHEMA_VERSION = 2

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            _prepare_schema(conn, self._SCHEMA_VERSION, "attendance")
            c = conn.cursor()
            # (guild_id, user_id)가 기본키다. 같은 사람이 서버마다 별도 잔액을 갖는다.
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    points INTEGER NOT NULL DEFAULT 0,
                    last_attendance_date TEXT,
                    forbidden_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

            # 모든 포인트 이동을 append-only로 기록한다. 환불 실패 시 대조 근거가 된다.
            c.execute("""
                CREATE TABLE IF NOT EXISTS point_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_user "
                "ON point_ledger (guild_id, user_id, id DESC)"
            )
            c.execute("""
                CREATE TABLE IF NOT EXISTS ai_usage (
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    command TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK (count >= 0),
                    PRIMARY KEY (user_id, usage_date, command)
                )
            """)
            _require_columns(
                conn,
                "users",
                {"guild_id", "user_id", "points", "last_attendance_date", "forbidden_count"},
            )
            _require_columns(
                conn,
                "point_ledger",
                {"id", "guild_id", "user_id", "delta", "reason", "created_at"},
            )
            _require_columns(
                conn,
                "ai_usage",
                {"user_id", "usage_date", "command", "count"},
            )
            _set_schema_version(conn, self._SCHEMA_VERSION)
            conn.commit()

    def get_points(self, guild_id: int, user_id: int) -> int:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT points FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            result = c.fetchone()
            return result[0] if result else 0

    def consume_ai_usage(
        self, user_id: int, usage_date: str, command: str, limit: int
    ) -> Optional[int]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO ai_usage (user_id, usage_date, command, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, usage_date, command) DO UPDATE SET count = count + 1
                WHERE count < ?
                RETURNING count
                """,
                (user_id, usage_date, command, limit),
            )
            row = cursor.fetchone()
            conn.commit()
            return row[0] if row else None

    def release_ai_usage(self, user_id: int, usage_date: str, command: str) -> bool:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                "UPDATE ai_usage SET count = count - 1 "
                "WHERE user_id = ? AND usage_date = ? AND command = ? AND count > 0",
                (user_id, usage_date, command),
            )
            released = cursor.rowcount > 0
            conn.commit()
            return released

    def get_ai_usage(self, user_id: int, usage_date: str, command: str) -> int:
        with closing(_connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT count FROM ai_usage "
                "WHERE user_id = ? AND usage_date = ? AND command = ?",
                (user_id, usage_date, command),
            ).fetchone()
            conn.commit()
            return row[0] if row else 0

    def add_points(
        self, guild_id: int, user_id: int, amount: int, reason: str = "unspecified"
    ) -> None:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, points) VALUES (?, ?, 0)",
                (guild_id, user_id),
            )
            c.execute(
                "UPDATE users SET points = points + ? WHERE guild_id = ? AND user_id = ?",
                (amount, guild_id, user_id),
            )
            _record_ledger(c, guild_id, user_id, amount, reason)
            conn.commit()

    def deduct_points(
        self, guild_id: int, user_id: int, amount: int, reason: str = "unspecified"
    ) -> bool:
        # 잔액 확인과 차감을 단일 조건부 UPDATE로 처리하여 race condition을 방지한다.
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE users SET points = points - ? "
                "WHERE guild_id = ? AND user_id = ? AND points >= ?",
                (amount, guild_id, user_id, amount),
            )
            charged = c.rowcount > 0
            if charged:  # 실제로 잔액이 줄었을 때만 기록한다.
                _record_ledger(c, guild_id, user_id, -amount, reason)
            conn.commit()
            return charged

    def claim_attendance(
        self,
        guild_id: int,
        user_id: int,
        reward: int,
        attendance_date: str,
        reason: str = "attendance",
    ) -> Optional[int]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (guild_id, user_id, points, last_attendance_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    points = users.points + excluded.points,
                    last_attendance_date = excluded.last_attendance_date
                WHERE users.last_attendance_date IS NULL
                   OR users.last_attendance_date != excluded.last_attendance_date
                RETURNING points
                """,
                (guild_id, user_id, reward, attendance_date),
            )
            row = cursor.fetchone()
            if row:  # 중복 출석은 지급이 없으므로 기록하지 않는다.
                _record_ledger(cursor, guild_id, user_id, reward, reason)
            conn.commit()
            return row[0] if row else None

    def increment_forbidden_count(self, guild_id: int, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO users (guild_id, user_id, points, forbidden_count) "
                "VALUES (?, ?, 0, 0)",
                (guild_id, user_id),
            )
            c.execute(
                "UPDATE users SET forbidden_count = forbidden_count + 1 "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            conn.commit()

    def get_forbidden_count(self, guild_id: int, user_id: int) -> int:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT forbidden_count FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            result = c.fetchone()
            return result[0] if result else 0

    def get_top_rankings(self, guild_id: int, limit: int = 5) -> List[Tuple[int, int]]:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT user_id, points FROM users WHERE guild_id = ? "
                "ORDER BY points DESC LIMIT ?",
                (guild_id, limit),
            )
            return c.fetchall()

    def get_ledger(
        self, guild_id: int, user_id: int, limit: int = 20
    ) -> List[Tuple[int, str, int]]:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT delta, reason, created_at FROM point_ledger "
                "WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, user_id, limit),
            )
            return c.fetchall()

    def delete_guild(self, guild_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM users WHERE guild_id = ?", (guild_id,))
            c.execute("DELETE FROM point_ledger WHERE guild_id = ?", (guild_id,))
            conn.commit()


class SQLitePartyRepository(PartyRepository):
    _SCHEMA_VERSION = 1

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            _prepare_schema(conn, self._SCHEMA_VERSION, "party")
            cursor = conn.cursor()
            # 길드당 게임별로 파티 하나.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    guild_id INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
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
            # 역할 중복은 DB가 거부한다. guild_id를 포함해야 서버 간 간섭이 없다.
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_role
                ON participants (guild_id, game, role) WHERE role IS NOT NULL
            """)
            _require_columns(conn, "parties", {"guild_id", "game", "created_at"})
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

    def create_party(self, guild_id: int, game: str, created_at: int) -> bool:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO parties (guild_id, game, created_at) VALUES (?, ?, ?)",
                (guild_id, game, created_at),
            )
            conn.commit()
            return cursor.rowcount > 0

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

    def remove_participant(self, guild_id: int, game: str, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM participants WHERE guild_id = ? AND game = ? AND user_id = ?",
                (guild_id, game, user_id),
            )
            conn.commit()

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
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT guild_id, game FROM parties WHERE created_at < ?", (cutoff,)
            )
            expired = [(row[0], row[1]) for row in cursor.fetchall()]

            if expired:
                cursor.executemany(
                    "DELETE FROM participants WHERE guild_id = ? AND game = ?", expired
                )
                cursor.execute("DELETE FROM parties WHERE created_at < ?", (cutoff,))
                conn.commit()
            return expired

    def delete_guild(self, guild_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM participants WHERE guild_id = ?", (guild_id,))
            cursor.execute("DELETE FROM parties WHERE guild_id = ?", (guild_id,))
            conn.commit()


class SQLiteGuildSettingsRepository(GuildSettingsRepository):
    # 컬럼명은 아래 상수에서만 나온다. 외부 입력이 SQL 문자열에 섞이지 않는다.
    _PARTY_CHANNEL = "party_channel_id"
    _MUSIC_CHANNEL = "music_channel_id"
    _MUSIC_PANEL_MESSAGE = "music_panel_msg_id"
    _ALLOW_HOST_ANNOUNCE = "allow_host_announce"
    _SCHEMA_VERSION = 2
    _CURRENT_COLUMNS = {
        "guild_id",
        _PARTY_CHANNEL,
        _MUSIC_CHANNEL,
        _MUSIC_PANEL_MESSAGE,
        _ALLOW_HOST_ANNOUNCE,
    }
    _LEGACY_COLUMNS = {"guild_id", "recruit_channel_id", "event_channel_id"}
    _GUILD_SETTINGS_COLUMN_ORDER = (
        "guild_id",
        _PARTY_CHANNEL,
        _MUSIC_CHANNEL,
        _MUSIC_PANEL_MESSAGE,
        _ALLOW_HOST_ANNOUNCE,
    )
    _PARTY_PANELS_COLUMN_ORDER = ("guild_id", "game", "message_id")

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
            if version < self._SCHEMA_VERSION and self._LEGACY_COLUMNS <= columns:
                conn.execute("BEGIN")
                try:
                    conn.execute("ALTER TABLE guild_settings RENAME TO guild_settings_v1")
                    self._create_current_tables(conn)
                    conn.execute(
                        """
                        INSERT INTO guild_settings (guild_id, party_channel_id)
                        SELECT guild_id, recruit_channel_id FROM guild_settings_v1
                        """
                    )
                    conn.execute("DROP TABLE guild_settings_v1")
                    self._require_current_contract(conn)
                    _set_schema_version(conn, self._SCHEMA_VERSION)
                except Exception:
                    conn.rollback()
                    raise
                else:
                    conn.commit()
                return

            with conn:
                self._create_party_panels_table(conn)
                self._require_current_contract(conn)
                _set_schema_version(conn, self._SCHEMA_VERSION)

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
                music_channel_id INTEGER,
                music_panel_msg_id INTEGER,
                allow_host_announce INTEGER NOT NULL DEFAULT 0
                    CHECK (allow_host_announce IN (0, 1))
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

    def get_music_channel(self, guild_id: int) -> Optional[int]:
        return self._get_column(guild_id, self._MUSIC_CHANNEL)

    def set_music_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        self._set_column(guild_id, self._MUSIC_CHANNEL, channel_id)

    def get_music_panel_msg(self, guild_id: int) -> Optional[int]:
        return self._get_column(guild_id, self._MUSIC_PANEL_MESSAGE)

    def set_music_panel_msg(self, guild_id: int, message_id: Optional[int]) -> None:
        self._set_column(guild_id, self._MUSIC_PANEL_MESSAGE, message_id)

    def get_allow_host_announce(self, guild_id: int) -> bool:
        return bool(self._get_column(guild_id, self._ALLOW_HOST_ANNOUNCE))

    def set_allow_host_announce(self, guild_id: int, allowed: bool) -> None:
        self._set_column(guild_id, self._ALLOW_HOST_ANNOUNCE, int(allowed))

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
                SET party_channel_id = CASE WHEN party_channel_id = ? THEN NULL ELSE party_channel_id END,
                    music_panel_msg_id = CASE WHEN music_channel_id = ? THEN NULL ELSE music_panel_msg_id END,
                    music_channel_id = CASE WHEN music_channel_id = ? THEN NULL ELSE music_channel_id END
                WHERE guild_id = ? AND (party_channel_id = ? OR music_channel_id = ?)
                """,
                (channel_id, channel_id, channel_id, guild_id, channel_id, channel_id),
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


# ─────────── 팩토리 ─────────── #

_UNSUPPORTED_MSG = (
    "지원하지 않는 DB 백엔드입니다: '{backend}'. "
    "module/database.py에 {repo} 구현을 추가하고 팩토리에 분기를 등록하세요."
)

def create_attendance_repository() -> AttendanceRepository:
    if DB_BACKEND == "sqlite":
        return SQLiteAttendanceRepository(DATA_DIR / "attendance_data.db")
    # 외부 DB 분기 예시:
    # if DB_BACKEND == "mysql":
    #     return MySQLAttendanceRepository(DB_URL)
    raise NotImplementedError(_UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="AttendanceRepository"))


def create_party_repository() -> PartyRepository:
    if DB_BACKEND == "sqlite":
        return SQLitePartyRepository(DATA_DIR / "party_data.db")
    raise NotImplementedError(_UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="PartyRepository"))


def create_guild_settings_repository() -> GuildSettingsRepository:
    if DB_BACKEND == "sqlite":
        return SQLiteGuildSettingsRepository(DATA_DIR / "guild_settings.db")
    raise NotImplementedError(
        _UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="GuildSettingsRepository")
    )
