# Database Repository Layer
#
# 봇의 모든 DB 접근(SQL)을 이 파일에 모아 추상화한다.
# Cog들은 Repository 인터페이스만 사용하므로, 외부 DB로 교체해도 Cog 코드는 바뀌지 않는다.
#
# 외부 DB(MySQL, Oracle 등)로 교체하는 방법:
#   1. AttendanceRepository / PartyRepository를 상속한 구현 클래스를 이 파일에 작성 (database.py)
#      (예: MySQLAttendanceRepository — SQL placeholder가 ?가 아닌 %s인 점 등 방언 차이만 처리하면 됨)
#   2. 아래 create_attendance_repository / create_party_repository 팩토리에 분기 추가
#   3. 환경 변수 DB_BACKEND=mysql, DB_URL=mysql://user:pass@host:3306/botdb 형태로 설정
#
# 모든 메서드는 동기(synchronous)로 호출된다. discord.py의 단일 이벤트 루프에서
# 짧은 쿼리만 수행하므로 현재 규모에서는 문제없지만, 외부 DB(네트워크 왕복)로
# 전환할 때는 커넥션 풀 사용을 권장한다.

from __future__ import annotations

from datetime import datetime, timezone
import pathlib
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from typing import Dict, List, Optional, Tuple
from module.config import DATA_DIR, DB_BACKEND


# ─────────── 연결 정책 ─────────── #

# 봇과 backup 프로세스가 같은 DB 파일을 연다. 기본 5초로는 백업 중 쓰기가 실패할 수 있다.
SQLITE_TIMEOUT_SECONDS = 30.0


def _connect(db_path, *, isolation_level: str | None = "") -> sqlite3.Connection:
    """이 모듈의 유일한 SQLite 연결 지점. timeout/journal 정책을 한 곳에 모은다."""
    return sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=isolation_level,
    )


# ─────────── 인터페이스 ─────────── #

class AttendanceRepository(ABC):
    """출석/포인트/금지어 카운트 데이터 접근 인터페이스."""

    @abstractmethod
    def get_points(self, user_id: int) -> int:
        """유저의 현재 포인트를 반환한다. 미등록 유저는 0."""

    @abstractmethod
    def add_points(self, user_id: int, amount: int) -> None:
        """포인트를 지급한다. 유저가 없으면 생성한다."""

    @abstractmethod
    def deduct_points(self, user_id: int, amount: int) -> bool:
        """포인트를 차감한다. 잔액 확인과 차감이 원자적으로 수행되어야 한다.
        성공 시 True, 잔액 부족 시 False."""

    @abstractmethod
    def claim_attendance(
        self,
        user_id: int,
        reward: int,
        attendance_date: str,
    ) -> Optional[int]:
        """당일 첫 출석이면 포인트를 지급하고 새 잔액을, 중복이면 None을 반환한다."""

    @abstractmethod
    def increment_forbidden_count(self, user_id: int) -> None:
        """금지어 경고 횟수를 1 증가시킨다. 유저가 없으면 생성한다."""

    @abstractmethod
    def get_forbidden_count(self, user_id: int) -> int:
        """금지어 경고 횟수를 반환한다. 미등록 유저는 0."""

    @abstractmethod
    def get_top_rankings(self, limit: int = 5) -> List[Tuple[int, int]]:
        """포인트 상위 유저 [(user_id, points), ...]를 반환한다."""

    @abstractmethod
    def play_luckybox(self, user_id: int, bet: int, today_str: str, multiplier: float):
        """럭키박스 베팅 전체(일일 제한 확인 -> 차감 -> 지급 -> 카운트 갱신)를
        단일 트랜잭션으로 처리한다. 동시 요청이 겹쳐도 잔액/횟수가 어긋나지 않아야 한다.

        Returns:
            ("limit", None)                              - 하루 3회 제한 초과
            ("insufficient", {"points": 보유 포인트})      - 잔액 부족
            ("ok", {"result_amount": 획득량, "final_points": 최종 잔액})
        """


class PartyRepository(ABC):
    """게임 파티 모집 데이터 접근 인터페이스."""

    @abstractmethod
    def get_party(self, game: str) -> Optional[Tuple[int]]:
        """파티가 존재하면 (created_at,)을, 없으면 None을 반환한다."""

    @abstractmethod
    def create_party(self, game: str, created_at: int) -> None:
        """파티를 생성한다. 이미 존재하면 무시한다."""

    @abstractmethod
    def delete_party(self, game: str) -> None:
        """파티를 삭제한다. 참가자도 함께 정리되어야 한다."""

    @abstractmethod
    def get_participants(self, game: str) -> Dict[int, Optional[str]]:
        """{user_id: role} 형태로 참가자 목록을 반환한다."""

    @abstractmethod
    def add_participant(self, game: str, user_id: int, role: Optional[str] = None) -> bool:
        """존재하는 파티에만 참가자를 추가하거나 역할을 갱신한다."""

    @abstractmethod
    def remove_participant(self, game: str, user_id: int) -> None:
        """참가자를 제거한다."""

    @abstractmethod
    def get_user_party(self, user_id: int) -> Optional[str]:
        """유저가 참가 중인 파티의 게임 이름을 반환한다. 없으면 None."""

    @abstractmethod
    def delete_expired_parties(self, cutoff: int) -> List[str]:
        """cutoff보다 오래된 파티를 삭제하고, 삭제된 게임 이름 목록을 반환한다."""


# ─────────── SQLite 구현 ─────────── #

class SQLiteAttendanceRepository(AttendanceRepository):
    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    points INTEGER DEFAULT 0,
                    last_attendance_date TEXT,
                    forbidden_count INTEGER DEFAULT 0,
                    luckybox_count INTEGER DEFAULT 0,
                    last_luckybox_date VARCHAR(20)
                )
            """)

            # Migration for existing tables
            # SQLite는 "ADD COLUMN IF NOT EXISTS"를 지원하지 않으므로 직접 확인 후 추가
            c.execute("PRAGMA table_info(users)")
            existing_columns = {row[1] for row in c.fetchall()}
            migrations = (
                ("luckybox_count", "ALTER TABLE users ADD COLUMN luckybox_count INTEGER DEFAULT 0"),
                ("last_luckybox_date", "ALTER TABLE users ADD COLUMN last_luckybox_date VARCHAR(20)"),
            )
            for column, ddl in migrations:
                if column not in existing_columns:
                    c.execute(ddl)

            conn.commit()

    def get_points(self, user_id: int) -> int:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
            result = c.fetchone()
            return result[0] if result else 0

    def add_points(self, user_id: int, amount: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO users (user_id, points) VALUES (?, 0)", (user_id,))
            c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()

    def deduct_points(self, user_id: int, amount: int) -> bool:
        # 잔액 확인과 차감을 단일 조건부 UPDATE로 처리하여 race condition을 방지한다.
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE users SET points = points - ? WHERE user_id = ? AND points >= ?",
                (amount, user_id, amount)
            )
            conn.commit()
            return c.rowcount > 0

    def claim_attendance(
        self,
        user_id: int,
        reward: int,
        attendance_date: str,
    ) -> Optional[int]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (user_id, points, last_attendance_date)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    points = users.points + excluded.points,
                    last_attendance_date = excluded.last_attendance_date
                WHERE users.last_attendance_date IS NULL
                   OR users.last_attendance_date != excluded.last_attendance_date
                RETURNING points
                """,
                (user_id, reward, attendance_date),
            )
            row = cursor.fetchone()
            conn.commit()
            return row[0] if row else None

    def increment_forbidden_count(self, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO users (user_id, points, forbidden_count) VALUES (?, 0, 0)", (user_id,))
            c.execute("UPDATE users SET forbidden_count = forbidden_count + 1 WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_forbidden_count(self, user_id: int) -> int:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("SELECT forbidden_count FROM users WHERE user_id = ?", (user_id,))
            result = c.fetchone()
            return result[0] if result else 0

    def get_top_rankings(self, limit: int = 5) -> List[Tuple[int, int]]:
        with closing(_connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, points FROM users ORDER BY points DESC LIMIT ?", (limit,))
            return c.fetchall()

    def play_luckybox(self, user_id: int, bet: int, today_str: str, multiplier: float):
        result_amount = int(bet * multiplier)
        conn = _connect(self.db_path, isolation_level=None)
        try:
            c = conn.cursor()
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT OR IGNORE INTO users (user_id, points) VALUES (?, 0)", (user_id,))
            c.execute(
                "SELECT points, luckybox_count, last_luckybox_date FROM users WHERE user_id = ?",
                (user_id,)
            )
            points, count, last_date = c.fetchone()

            if last_date != today_str:
                count = 0  # 날짜가 바뀌면 카운트 초기화

            if count >= 3:
                conn.rollback()
                return "limit", None

            if points < bet:
                conn.rollback()
                return "insufficient", {"points": points}

            new_points = points - bet + result_amount
            c.execute(
                "UPDATE users SET points = ?, luckybox_count = ?, last_luckybox_date = ? WHERE user_id = ?",
                (new_points, count + 1, today_str, user_id)
            )
            conn.commit()
            return "ok", {"result_amount": result_amount, "final_points": new_points}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class SQLitePartyRepository(PartyRepository):
    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            # Parties table: game is the primary key (one party per game)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS parties (
                    game TEXT PRIMARY KEY,
                    created_at TIMESTAMP
                )
            """)
            # Participant table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS participants (
                    game TEXT,
                    user_id INTEGER,
                    role TEXT,
                    PRIMARY KEY (game, user_id),
                    FOREIGN KEY (game) REFERENCES parties (game) ON DELETE CASCADE
                )
            """)
            cursor.execute("SELECT game, created_at FROM parties")
            for game, value in cursor.fetchall():
                if value is None:
                    timestamp = 0  # Legacy NULL is immediately eligible for normal expiry.
                elif isinstance(value, int):
                    continue
                elif isinstance(value, float):
                    timestamp = int(value)
                else:
                    if isinstance(value, bytes):
                        value = value.decode()
                    parsed = datetime.fromisoformat(str(value))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    timestamp = int(parsed.timestamp())
                cursor.execute(
                    "UPDATE parties SET created_at = ? WHERE game = ?",
                    (timestamp, game),
                )
            cursor.execute(
                """
                DELETE FROM participants
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM parties
                    WHERE parties.game = participants.game
                )
                """
            )
            conn.commit()

    def get_party(self, game: str) -> Optional[Tuple[int]]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT created_at FROM parties WHERE game = ?", (game,))
            return cursor.fetchone()

    def create_party(self, game: str, created_at: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO parties (game, created_at) VALUES (?, ?)",
                           (game, created_at))
            conn.commit()

    def delete_party(self, game: str) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            # SQLite의 FK CASCADE는 기본적으로 꺼져 있으므로 참가자를 직접 정리한다
            cursor.execute("DELETE FROM participants WHERE game = ?", (game,))
            cursor.execute("DELETE FROM parties WHERE game = ?", (game,))
            conn.commit()

    def get_participants(self, game: str) -> Dict[int, Optional[str]]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, role FROM participants WHERE game = ?", (game,))
            return {row[0]: row[1] for row in cursor.fetchall()}

    def add_participant(self, game: str, user_id: int, role: Optional[str] = None) -> bool:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO participants (game, user_id, role)
                SELECT ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM parties WHERE game = ?)
            """, (game, user_id, role, game))
            conn.commit()
            return cursor.rowcount > 0

    def remove_participant(self, game: str, user_id: int) -> None:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM participants WHERE game = ? AND user_id = ?", (game, user_id))
            conn.commit()

    def get_user_party(self, user_id: int) -> Optional[str]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT game FROM participants WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    def delete_expired_parties(self, cutoff: int) -> List[str]:
        with closing(_connect(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT game FROM parties WHERE created_at < ?", (cutoff,))
            expired_games = [row[0] for row in cursor.fetchall()]

            if expired_games:
                placeholders = ",".join("?" * len(expired_games))
                cursor.execute(f"DELETE FROM participants WHERE game IN ({placeholders})", expired_games)
                cursor.execute("DELETE FROM parties WHERE created_at < ?", (cutoff,))
                conn.commit()
            return expired_games


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
    # 외부 DB 분기 예시:
    # if DB_BACKEND == "mysql":
    #     return MySQLPartyRepository(DB_URL)
    raise NotImplementedError(_UNSUPPORTED_MSG.format(backend=DB_BACKEND, repo="PartyRepository"))
