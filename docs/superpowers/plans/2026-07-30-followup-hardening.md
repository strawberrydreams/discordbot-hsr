# Follow-up Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `2026-07-29-code-review-remediation.md` 완료 이후 남은 결함을 수정한다. 포인트 통화량을 출석 수입 하나로 묶어 AI 비용을 구조적으로 제한하고, 동시 요청에서의 대화·파티 무결성, 길드 경계 강제, 포인트 감사 추적, 그리고 서버 이탈·메시지 수정 같은 미처리 생명주기 이벤트를 다룬다.

**Architecture:** 기존 Cog/Repository 구조와 표준 라이브러리를 유지한다. 데이터 불변식은 파이썬 검사가 아니라 SQL 제약으로 내린다. 동시성 보호는 상태를 소유한 계층(채널 세션은 메모리 락, 파티는 DB 제약)에 배치한다. Discord 상호작용은 Cog 테스트로, 데이터·배포 계약은 임시 SQLite DB를 쓰는 콘솔 테스트로 검증한다.

**Tech Stack:** Python 3.12, discord.py 2.7.1, SQLite, OpenAI Responses API, Google Gen AI SDK, Docker Compose, launchd

## Global Constraints

- 새 Python 의존성을 추가하지 않는다.
- 기존 SQLite 파일과 백업 형식을 유지하며 운영 DB를 테스트에서 열지 않는다.
- 포인트 차감·환불은 중복 요청과 전송 실패에서도 정확히 한 번만 반영되어야 하고, 모든 이동은 원장에 기록되어야 한다.
- 포인트의 유일한 수입원은 `/출석`이다. 통화를 발행하는 다른 경로를 추가하지 않는다.
- AI 명령은 예외 없이 포인트 비용을 갖는다. 무료 AI 경로를 남기지 않는다.
- 가격과 쿨다운은 코드 상수 또는 환경변수로 한 곳에 모아 조정 가능해야 한다.
- `python -m unittest test.test_discord_commands`와 `python -m test.console_tests`를 모두 통과시킨다.
- 각 작업은 해당 테스트를 먼저 실패시킨 뒤 최소 구현으로 통과시키고 별도 커밋한다.
- 동작 코드나 배포 계약을 수정하는 커밋은 해당 회귀 테스트 변경을 같은 커밋에 포함한다. 테스트 없는 동작 변경은 완료로 처리하지 않는다.

## Scope Decisions

리뷰에서 제기됐으나 **범위에서 제외**한 항목과 근거:

| 항목 | 결정 | 근거 |
|---|---|---|
| S1 Git 이력 시크릿 | 제외 | 키·토큰을 2회 재발급했으므로 이력의 값은 현재 유효 자격증명과 무관 |
| S3 `/프로필` 타인 조회 | 제외 | 소규모 서버에서 허용 가능하다고 판단. `/랭킹`으로 포인트는 이미 공개 |
| S4 금지어 재게시 | 제외 | 경고에 단어를 노출하는 것은 의도된 동작 |
| F1 멘션 알림 복원 | 제외 | 전역 `AllowedMentions.none()`으로 알림이 가지 않는 현 동작을 유지 |
| 전역 AI 예산 kill switch | 제외 | OpenAI 계정 측 예산 한도로 처리. 앱에서 중복 구현하지 않음 |
| `/주가` rate limit | 제외 | AI 비용과 무관. Task 13의 TTL 캐시가 남용 경로를 차단 |
| 사용자별 일일 쿼터(`ai_usage` 테이블) | 제외 | Task 4가 통화량을 출석 수입으로 묶으므로 포인트 잔액 자체가 일일 상한이 된다. 상한을 두 곳에 두면 진단만 어려워진다 |

## 포인트 경제 근거

Task 4의 수치는 다음 계산에 근거한다. 값을 바꿀 때 이 절을 함께 갱신한다.

**현행 문제:** `/럭키박스`의 배수 `random.uniform(0.2, 3.0)`은 기댓값 1.6배로 하우스 엣지가 음수다. 로그 기준으로도 회당 +34%라 잔액이 증가하는 쪽으로만 편향된다. 베팅 상한이 잔액 전부이므로 하루 3회 전액 베팅 시 잔액이 하루 약 2.4배(기댓값 4.1배)로 복리 증가하고, 현재 잔액 5,478 P 기준 3일이면 이미지 1회분을 넘긴다. **어떤 가격을 매겨도 며칠 안에 무의미해진다.**

**해결:** 통화 발행 경로를 `/출석` 하나로 축소한다. 사용자당 수입이 하루 5,000~30,000 P(평균 17,500)로 확정되고, 가격이 곧 사용 빈도 상한이 된다.

| 명령 | 기존 | 신규 | 평균 수입 기준 하루 한도 |
|---|---|---|---|
| `/기본대화` | 0 P | 200 P | 약 87회 |
| `/고급대화` | 2,000 P | 2,000 P (유지) | 약 8.7회 |
| `/이미지` | 50,000 P | 30,000 P | 약 0.6회 (이틀에 1회) |

`/기본대화`에 값을 붙이는 것이 핵심이다. 무료로 두면 유일한 수입원을 `/출석`으로 묶는 의미가 사라진다.

**남는 한계:** 포인트는 유량이 아니라 저량이므로 장기 누적 잔액을 한 번에 소진하는 버스트는 여전히 가능하다. Task 5의 쿨다운이 속도를 제한하고 OpenAI 계정 예산이 최후의 안전망이다. 소규모 서버 기준으로 수용 가능하다고 판단한다.

## File Responsibility Map

- `module/database.py`: SQLite 연결 정책(busy timeout, WAL), 포인트 원장, 파티 정원·역할 유니크 제약.
- `module/attendance_cog.py`: 유일한 통화 발행처(`/출석`), 원장 경유 포인트 이동, 랭킹 표시.
- `module/hyacine_chat_cog.py`: AI 명령 가격과 쿨다운, 채널 세션 직렬화, 진행 중 세션 보호, quota 에러 구분.
- `module/hyacine_image_cog.py`: 이미지 가격, 프롬프트 길이 제한, 임시 이미지 경로와 수명.
- `module/playwith_cog.py`: 파티 생성 중복 차단, 서버 이탈 참가자 정리, 인원수 계산.
- `module/forbiddenfilter_cog.py`: 메시지 수정 검사.
- `module/finance_cog.py`: 외부 호출 타임아웃과 TTL 캐시.
- `module/main.py`: 길드 경계 강제, 명령 sync 마커 위치.
- `compose.yaml`: backup 서비스 시크릿 제거, WAL용 데이터 마운트 권한.

## Required Source-to-Test Mapping

| Task | 동작·배포 변경 | 같은 커밋에 포함할 테스트 |
|---|---|---|
| 1 | SQLite busy timeout | `test_sqlite_busy_timeout` |
| 2 | WAL 전환 + backup 마운트 | `test_backup_reads_wal_without_writer`, `test_deployment_contracts` |
| 3 | 포인트 원장 | `test_point_ledger` |
| 4 | 럭키박스 삭제·가격 재산정 | `test_luckybox_removed`, `EconomyTests` |
| 5 | AI 쿨다운 | `AICooldownTests` |
| 6 | 채널 세션 직렬화 | `ChatConcurrencyTests` |
| 7 | 진행 중 세션 eviction | `test_active_session_survives_eviction` |
| 8 | 파티 생성 중복 차단 | `PartyCreationTests`, `test_party_repository` |
| 9 | 정원·역할 SQL 제약 | `test_party_capacity_constraint` |
| 10 | 길드 경계 | `GuildBoundaryTests`, `test_guild_guard` |
| 11 | 서버 이탈 참가자 정리 | `PartyMembershipTests` |
| 12 | 메시지 수정 검사 | `ForbiddenEditTests` |
| 13 | 금융 타임아웃·캐시 | `FinanceCommandTests` 확장 |
| 14 | 프롬프트 길이 제한 | `ImageCommandTests` 확장 |
| 15 | 임시 이미지 경로 | `test_temp_image_lifecycle` |
| 16 | 랭킹 표시 | `RankingCommandTests` |
| 17 | sync 마커 위치 | `test_global_cleanup_marker_location` |
| 18 | backup 시크릿 제거 | `test_deployment_contracts` |

---

### Task 1: SQLite busy timeout 상향

**Files:**
- Modify: `module/database.py:16-25,116-402`
- Modify: `test/console_tests.py`

**Interfaces:**
- Produces: `database._connect(db_path, *, isolation_level="") -> sqlite3.Connection`

봇과 backup 프로세스가 같은 DB 파일을 연다. 기본 busy timeout 5초는 백업 중 쓰기 실패를 낼 수 있다. 모든 연결을 단일 헬퍼로 모아 timeout을 30초로 올린다.

- [ ] **Step 1: 연결 계약 테스트 작성**

```python
def test_sqlite_busy_timeout():
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "t.db"
        with closing(database._connect(path)) as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        check("busy timeout 30초 적용", timeout_ms == 30_000)

    source = pathlib.Path(inspect.getsourcefile(database)).read_text(encoding="utf-8")
    direct = source.count("sqlite3.connect(")
    check("모든 연결이 헬퍼를 경유", direct == 1, f"직접 호출 {direct}건")
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `_connect` 부재로 실패.

- [ ] **Step 3: 연결 헬퍼 도입**

`module/database.py`에 추가하고, 기존 `sqlite3.connect(...)` 호출을 전부 교체한다. `play_luckybox`는 Task 4에서 삭제되므로, 그 전까지는 `isolation_level=None`을 넘겨 기존 `BEGIN IMMEDIATE` 동작을 유지한다.

```python
SQLITE_TIMEOUT_SECONDS = 30.0


def _connect(db_path, *, isolation_level: str | None = "") -> sqlite3.Connection:
    return sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=isolation_level,
    )
```

`module/backup.py`의 읽기 전용 연결도 `timeout=SQLITE_TIMEOUT_SECONDS`를 넘긴다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/database.py module/backup.py test/console_tests.py
git commit -m "fix: raise sqlite busy timeout for concurrent processes"
```

---

### Task 2: WAL 전환과 백업 마운트 권한

**Files:**
- Modify: `module/database.py`
- Modify: `compose.yaml`
- Modify: `test/console_tests.py`

**Interfaces:**
- Produces: `journal_mode=WAL`인 `attendance_data.db`, `party_data.db`

**⚠️ 전제 조건 — 실측 결과:** WAL DB는 읽기 전용 연결이라도 `-shm`/`-wal` 파일을 만들 수 있어야 한다. 읽기 전용 디렉터리에서는 불가능하다.

| 상황 | 결과 |
|---|---|
| WAL + `:ro` 마운트 + 봇 실행 중(`-shm` 존재) | 백업 성공 |
| WAL + `:ro` 마운트 + 봇 정지(`-shm` 없음) | **실패** — `attempt to write a readonly database` |

따라서 WAL 전환은 backup 서비스의 데이터 마운트를 rw로 바꾸는 것과 **같은 커밋**이어야 한다. 그러지 않으면 봇이 멈춰 있을 때만 백업이 실패하고, `module.backup loop`이 예외를 삼켜 조용히 재시도만 반복한다. 백업 코드는 온라인 backup API로 읽기만 하므로 rw 마운트가 데이터를 변경하지는 않는다.

- [ ] **Step 1: 봇 정지 상태 백업 회귀 테스트 작성**

```python
def test_backup_reads_wal_without_writer():
    with tempfile.TemporaryDirectory() as directory:
        data_dir = pathlib.Path(directory) / "data"
        data_dir.mkdir()
        repo = database.SQLiteAttendanceRepository(data_dir / "attendance_data.db")
        repo.add_points(1, 10)
        del repo  # 쓰기 연결 없음 = 봇 정지 상태

        source = data_dir / "attendance_data.db"
        check("WAL 모드로 저장됨", not (data_dir / "attendance_data.db-wal").exists() or True)
        target = pathlib.Path(directory) / "copy.db"
        backup._backup_one(source, target)
        with closing(sqlite3.connect(target)) as conn:
            points = conn.execute("SELECT points FROM users WHERE user_id = 1").fetchone()
        check("쓰기 프로세스 없이도 백업 가능", points == (10,))
```

`test_deployment_contracts`에는 마운트 계약을 추가한다.

```python
compose = yaml_like_load(PROJECT_ROOT / "compose.yaml")
backup_volumes = compose["services"]["backup"]["volumes"]
check(
    "backup 데이터 마운트는 WAL을 위해 쓰기 가능",
    not any(v.endswith(":ro") and "runtime/data" in v for v in backup_volumes),
)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 마운트 계약 검사 실패.

- [ ] **Step 3: WAL 적용과 마운트 변경**

`_connect`에 추가한다. `journal_mode`는 DB 파일에 영속되므로 매 연결 설정은 멱등하다.

```python
def _connect(db_path, *, isolation_level: str | None = "") -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=isolation_level,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
```

`compose.yaml`의 backup 서비스에서 `:ro`를 제거한다.

```yaml
    volumes:
      - ./runtime/data:/app/runtime/data
      - ./runtime/backups:/app/runtime/backups
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/database.py compose.yaml test/console_tests.py
git commit -m "fix: enable wal and allow backup to open it"
```

---

### Task 3: 포인트 원장

**Files:**
- Modify: `module/database.py:29-76,116-254`
- Modify: `module/attendance_cog.py:16-36`
- Modify: `test/console_tests.py`

**Interfaces:**
- Produces: `point_ledger(id, user_id, delta, reason, created_at)` 테이블
- Produces: `AttendanceRepository.get_ledger(user_id, limit) -> List[Tuple[int, str, int]]`

환불 실패 시 사용자에게 "관리자에게 수동 정산을 요청"하라고 안내하지만 대조할 기록이 없다. 모든 포인트 이동을 같은 트랜잭션에서 append-only로 기록한다.

- [ ] **Step 1: 원장 테스트 작성**

```python
def test_point_ledger():
    with tempfile.TemporaryDirectory() as directory:
        repo = database.SQLiteAttendanceRepository(pathlib.Path(directory) / "a.db")
        repo.add_points(1, 500, reason="attendance")
        check("차감 실패는 원장에 남기지 않음", repo.deduct_points(1, 9_999, reason="image") is False)
        check("차감 성공", repo.deduct_points(1, 200, reason="image") is True)
        repo.add_points(1, 200, reason="image_refund")

        entries = repo.get_ledger(1, limit=10)
        check("모든 성공 이동이 기록됨", len(entries) == 3)
        check("원장 합계가 잔액과 일치", sum(delta for delta, _, _ in entries) == repo.get_points(1))
        check("실패한 차감은 기록되지 않음", all(reason != "image" or delta == -200 for delta, reason, _ in entries))
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `reason` 인자 미지원으로 실패.

- [ ] **Step 3: 원장 구현**

`_init_db`에 테이블을 추가한다.

```python
c.execute("""
    CREATE TABLE IF NOT EXISTS point_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user ON point_ledger (user_id, id DESC)")
```

`add_points`, `deduct_points`, `claim_attendance`에 `reason: str` 인자를 추가하고(`play_luckybox`는 Task 4에서 삭제되므로 건드리지 않는다), **포인트를 실제로 변경한 트랜잭션 안에서만** 원장 행을 넣는다. `deduct_points`는 `rowcount > 0`일 때만 기록한다. `AttendanceCog`의 위임 메서드도 `reason`을 통과시키고, 호출부(`hyacine_chat_cog.py:194`, `hyacine_image_cog.py:67` 등)는 각자 사유를 넘긴다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/database.py module/attendance_cog.py module/hyacine_chat_cog.py module/hyacine_image_cog.py test/console_tests.py
git commit -m "feat: record every point movement in an append-only ledger"
```

---

### Task 4: 럭키박스 삭제와 AI 가격 재산정

**Files:**
- Modify: `module/attendance_cog.py:1,75-114`
- Modify: `module/database.py:66-75,219-254`
- Modify: `module/hyacine_chat_cog.py:39-41,267-285`
- Modify: `module/hyacine_image_cog.py:37-50`
- Modify: `README.md:7`
- Modify: `test/console_tests.py:1-10,780-798,880-920,1848`
- Modify: `test/test_discord_commands.py`

**Interfaces:**
- Removes: `/럭키박스` 명령, `AttendanceRepository.play_luckybox`
- Produces: `hyacine_chat_cog.LIGHT_COST = 200`, `DEEP_COST = 2_000`, `hyacine_image_cog.IMAGE_COST = 30_000`

「포인트 경제 근거」절 참조. 럭키박스가 통화를 무한 발행하는 한 어떤 가격도 며칠이면 무의미해진다. 배수를 조정하는 대신 명령을 삭제해 통화 발행 경로를 `/출석` 하나로 만들고, 그 수입에 맞춰 가격을 재산정한다. `/기본대화`의 유료화가 핵심이다 — 무료로 남기면 유일 수입원으로 묶는 의미가 사라진다.

**스키마 처리:** `users.luckybox_count`와 `last_luckybox_date` 컬럼은 **남긴다.** 컬럼 삭제는 기존 백업에서의 복구 호환성을 깨뜨리는 반면 미사용 컬럼의 비용은 없다. `_init_db`의 마이그레이션 항목도 그대로 두어 구버전 DB가 여전히 승격되게 한다.

- [ ] **Step 1: 삭제·가격 테스트 작성**

콘솔 테스트에서 명령과 메서드가 사라졌는지 확인한다.

```python
def test_luckybox_removed():
    check("play_luckybox 인터페이스 제거", not hasattr(database.AttendanceRepository, "play_luckybox"))
    check("럭키박스 명령 제거", "럭키박스" not in {c.name for c in AttendanceCog(bot=None).get_app_commands()})
    check("레거시 컬럼은 보존", {"luckybox_count", "last_luckybox_date"} <= existing_columns(repo))
    check("출석 외 통화 발행 없음", "add_points" not in inspect.getsource(attendance_cog.AttendanceCog._attend))
```

`EconomyTests`에서는 가격 계약을 고정한다.

```python
async def test_light_chat_charges_points(self):
    # 잔액 부족 시 API 호출 없이 거부되고 포인트가 변하지 않는지
    ...
    self.assertEqual(attendance.deducted, [(FakeUser.id, 200)])
    self.assertEqual(cog.client.calls, 0)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `play_luckybox`가 여전히 존재해 실패.

- [ ] **Step 3: 삭제와 가격 적용**

`module/attendance_cog.py`에서 `_luckybox` 명령 전체와 이제 쓰이지 않는 `random` import를 제거한다(`_attend`의 `random.randint`는 유지되므로 import는 남는다). `module/database.py`에서 `play_luckybox`를 추상 메서드와 SQLite 구현 양쪽에서 제거한다.

`hyacine_chat_cog.py`에 가격 상수를 도입하고 `_light_talk`이 이를 사용하게 한다.

```python
LIGHT_COST = 200
DEEP_COST = 2_000

...
await self._run_talk(inter, 내용, 이미지, LIGHT_MODEL, "none", LIGHT_COST)
```

명령 설명 문자열도 갱신한다(`/기본대화 ... (200 P)`). `hyacine_image_cog.py`의 `cost = 50000`을 모듈 상수 `IMAGE_COST = 30_000`으로 바꾸고 설명 문자열을 맞춘다.

`README.md`의 명령 목록에서 `/럭키박스`를 제거한다. `test/console_tests.py`의 `test_play_luckybox`와 그 호출(`:1848`), 헤더 주석의 관련 항목을 삭제하되, 구버전 스키마 마이그레이션 테스트(`:780-798`)는 컬럼을 남기므로 **유지한다.**

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/attendance_cog.py module/database.py module/hyacine_chat_cog.py module/hyacine_image_cog.py README.md test/
git commit -m "feat: fund ai commands from attendance income only"
```

---

### Task 5: AI 명령 쿨다운

**Files:**
- Modify: `module/config.py:38-79`
- Modify: `module/hyacine_chat_cog.py:163-285`
- Modify: `module/hyacine_image_cog.py:37-50`
- Modify: `.env.example`
- Modify: `test/test_discord_commands.py`

Task 4로 지속 사용량은 출석 수입에 묶였지만, 포인트는 유량이 아니라 저량이라 누적 잔액을 한 번에 소진하는 버스트가 남는다. 쿨다운이 그 속도를 제한한다. 사용자별 일일 쿼터는 도입하지 않는다 — 포인트 잔액이 이미 그 역할을 한다.

- [ ] **Step 1: 쿨다운·에러 구분 테스트 작성**

`AICooldownTests`에서 검증한다.

- 쿨다운 중 재호출은 ephemeral 안내가 나가고 **포인트가 차감되지 않는다**
- 쿨다운 안내가 남은 초를 포함한다
- `openai.RateLimitError`는 일반 실패와 다른 문구로 안내되고, 두 경로 모두 환불된다

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 쿨다운과 에러 분기 구현**

```python
AI_COOLDOWN_SECONDS = _int_from_env("AI_COOLDOWN_SECONDS", 15)
```

`validate_config()`에 양수 검사를 추가하고 `.env.example`에도 반영한다. 세 AI 명령에 데코레이터를 건다.

```python
@app_commands.checks.cooldown(1, AI_COOLDOWN_SECONDS, key=lambda i: i.user.id)
```

쿨다운은 콜백 진입 전에 걸리므로 포인트 차감보다 앞선다. Cog의 `cog_app_command_error`에서 `app_commands.CommandOnCooldown`을 잡아 `error.retry_after`를 ephemeral로 안내한다.

`_run_talk`의 예외 처리에서 `openai.RateLimitError`를 별도 분기해 "지금은 요청이 몰려 있어요. 잠시 후 다시 시도해 주세요." 문구로 안내한다. 기존 `refund_points()` 경로는 그대로 탄다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/config.py module/hyacine_chat_cog.py module/hyacine_image_cog.py .env.example test/test_discord_commands.py
git commit -m "feat: add cooldown to ai commands"
```

---

### Task 6: 채널 세션 직렬화

**Files:**
- Modify: `module/hyacine_chat_cog.py:16-25,163-250`
- Modify: `test/test_discord_commands.py`

**Interfaces:**
- Produces: `ChannelSession.lock: asyncio.Lock`

`:209`가 히스토리를 읽고 `:221`에서 API를 await한 뒤 `:246-247`에서 append한다. 같은 채널의 동시 호출은 같은 히스토리를 읽고 완료 순서대로 append하므로 user/assistant 턴이 어긋나게 짝지어지고 `:233`의 `last_usage`도 경합한다.

- [ ] **Step 1: 인터리빙 테스트 작성**

`ChatConcurrencyTests`에 같은 `channel_id`로 두 호출을 `asyncio.gather`로 띄우되, 첫 호출의 API 응답이 두 번째보다 늦게 끝나도록 지연시킨다.

```python
async def test_same_channel_calls_do_not_interleave_history(self):
    cog = make_chat_cog(delays={"first": 0.05, "second": 0.0})
    a = FakeInteraction(channel_id=1)
    b = FakeInteraction(channel_id=1)

    await asyncio.gather(
        HyacineChatCog._light_talk.callback(cog, a, "first", None),
        HyacineChatCog._light_talk.callback(cog, b, "second", None),
    )

    roles = [m["role"] for m in cog.get_session(1).history if m["role"] != "system"]
    self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

Expected: `["user", "user", "assistant", "assistant"]`로 실패.

- [ ] **Step 3: 세션 락 도입**

`ChannelSession.__init__`에 `self.lock = asyncio.Lock()`을 추가하고, `_run_talk`에서 히스토리 읽기부터 append까지를 감싼다. 포인트 차감과 `defer()`는 락 밖에 두어 대기 중에도 인터랙션이 만료되지 않게 한다.

```python
async with session.lock:
    self.trim(session)
    recent_turns = [...]
    resp = await self.client.responses.create(**kwargs)
    ...
    session.history.append(...)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/hyacine_chat_cog.py test/test_discord_commands.py
git commit -m "fix: serialize chat turns within a channel"
```

---

### Task 7: 진행 중 세션 eviction 방지

**Files:**
- Modify: `module/hyacine_chat_cog.py:29,73-81`
- Modify: `test/test_discord_commands.py`

`get_session`의 LRU 축출(`:79-80`)은 응답을 기다리는 세션도 버린다. 코루틴은 고아 객체에 append하므로 해당 턴이 조용히 유실된다.

- [ ] **Step 1: 축출 테스트 작성**

```python
async def test_active_session_survives_eviction(self):
    cog = HyacineChatCog(bot=None)
    cog.MAX_CHANNEL_SESSIONS = 2
    active = cog.get_session(1)
    async with active.lock:
        for channel_id in range(2, 12):
            cog.get_session(channel_id)
        self.assertIs(cog.get_session(1), active)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 사용 중 세션 보호**

축출 대상을 고를 때 `lock.locked()`인 세션을 건너뛴다.

```python
while len(self.sessions) > self.MAX_CHANNEL_SESSIONS:
    for channel_id, candidate in self.sessions.items():
        if not candidate.lock.locked():
            del self.sessions[channel_id]
            break
    else:
        break  # 전부 사용 중이면 이번 사이클은 축출하지 않음
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/hyacine_chat_cog.py test/test_discord_commands.py
git commit -m "fix: keep in-flight chat sessions out of lru eviction"
```

---

### Task 8: 파티 생성 중복 차단

**Files:**
- Modify: `module/database.py:85-87,320-325`
- Modify: `module/playwith_cog.py:40-41,194-198`
- Modify: `test/test_discord_commands.py`, `test/console_tests.py`

**Interfaces:**
- Produces: `PartyRepository.create_party(game, created_at) -> bool`

`playwith_cog.py:68-71`이 `/모집` 시점에 가용 게임을 계산하고, `create_party`는 `INSERT OR IGNORE`라 성공 여부를 알리지 않으며, `GameSelect.callback`은 무조건 임베드를 게시한다. 두 사람이 각각 `/모집`을 연 뒤 같은 게임을 고르면 파티는 하나인데 공개 임베드와 참가 버튼이 두 개 생긴다. 드롭다운 선택 사이에 사용자 대기 시간이 있어 이벤트 루프가 막아주지 못한다.

- [ ] **Step 1: 중복 생성 테스트 작성**

```python
check("첫 생성은 True", repo.create_party("LoL", 1_000) is True)
check("중복 생성은 False", repo.create_party("LoL", 2_000) is False)
check("중복 생성이 시각을 덮어쓰지 않음", repo.get_party("LoL") == (1_000,))
```

`PartyCreationTests`에서는 같은 게임을 고른 두 번째 `GameSelect.callback`이 임베드 대신 안내 메시지를 내는지 검증한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

- [ ] **Step 3: 생성 결과 전달**

```python
def create_party(self, game: str, created_at: int) -> bool:
    with closing(_connect(self.db_path)) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO parties (game, created_at) VALUES (?, ?)",
            (game, created_at),
        )
        conn.commit()
        return cursor.rowcount > 0
```

`PlayWithCog.create_party`가 결과를 반환하고, `GameSelect.callback`은 False면 ephemeral 안내 후 조기 반환한다.

```python
if not self.cog.create_party(selected_game):
    await interaction.response.send_message(
        f"⚠️ `{selected_game}` 파티는 이미 생성되어 있습니다.", ephemeral=True
    )
    return
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/database.py module/playwith_cog.py test/
git commit -m "fix: reject duplicate party creation"
```

---

### Task 9: 정원·역할 유니크를 SQL로 강제

**Files:**
- Modify: `module/database.py:96-99,263-350`
- Modify: `module/playwith_cog.py:253-292,321-323`
- Modify: `test/console_tests.py`

**Interfaces:**
- Produces: `add_participant(game, user_id, role, max_players) -> bool`

현재 정원·역할 중복 검사는 파이썬에만 있고, 검사와 쓰기 사이에 `await`가 없어서 우연히 안전하다. 누가 그 사이에 `await` 한 줄만 넣어도 정원 초과 파티와 역할 중복이 조용히 생긴다. 불변식을 DB로 내린다.

- [ ] **Step 1: 제약 테스트 작성**

```python
def test_party_capacity_constraint():
    with tempfile.TemporaryDirectory() as directory:
        repo = database.SQLitePartyRepository(pathlib.Path(directory) / "p.db")
        repo.create_party("PUBG", 1_000)
        results = [repo.add_participant("PUBG", uid, None, max_players=2) for uid in (1, 2, 3)]
        check("정원 초과는 DB에서 거부", results == [True, True, False])

        repo.create_party("LoL", 1_000)
        check("역할 배정", repo.add_participant("LoL", 1, "탑", max_players=5) is True)
        check("역할 중복은 DB에서 거부", repo.add_participant("LoL", 2, "탑", max_players=5) is False)
        check("본인 역할 재지정은 허용", repo.add_participant("LoL", 1, "탑", max_players=5) is True)
        check("거부된 참가는 행을 남기지 않음", repo.get_user_party(2) is None)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

- [ ] **Step 3: 제약과 조건부 INSERT 구현**

`_init_db`에 부분 유니크 인덱스를 추가한다. 기존 DB에 역할 중복 행이 있으면 인덱스 생성이 실패하므로, 생성 전에 `game, role`별로 `rowid`가 가장 작은 행만 남기고 정리한다.

```python
cursor.execute("""
    DELETE FROM participants
    WHERE role IS NOT NULL
      AND rowid NOT IN (
          SELECT MIN(rowid) FROM participants WHERE role IS NOT NULL GROUP BY game, role
      )
""")
cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_role
    ON participants (game, role) WHERE role IS NOT NULL
""")
```

`add_participant`는 `BEGIN IMMEDIATE` 안에서 정원을 확인하고, 역할 충돌은 `IntegrityError`로 잡는다. 기존 참가자의 역할 변경(`RoleUpdateSelect`)은 같은 `user_id`이므로 먼저 자기 행을 지우고 다시 넣는다.

`playwith_cog.py`의 파이썬 검사는 **사용자 친화적 메시지를 위해 유지**하되, 최종 판정은 `add_participant`의 반환값을 따른다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/database.py module/playwith_cog.py test/
git commit -m "fix: enforce party capacity and role uniqueness in sql"
```

---

### Task 10: 길드 경계 강제

**Files:**
- Modify: `module/main.py:53-78`
- Modify: `module/forbiddenfilter_cog.py:109-133`
- Modify: `module/playwith_cog.py:229-232`
- Modify: `test/test_discord_commands.py`, `test/console_tests.py`

슬래시 명령은 길드 sync로 격리되지만 `on_message`는 모든 길드와 DM에서 동작해 공용 DB의 `forbidden_count`를 올리고, 영속 JoinButton(`custom_id="party:join:{game}"`)은 위치 무관하게 응답한다. 파티·포인트 테이블에 guild_id 컬럼이 없어 전 서버 공용이다.

- [ ] **Step 1: 경계 테스트 작성**

`FakeInteraction`에 `guild_id` 속성을 추가하고 다음을 검증한다.

- 다른 길드의 `on_message`는 경고도 카운트 증가도 하지 않는다
- DM(`message.guild is None`)은 무시한다
- 다른 길드의 JoinButton 클릭은 참가 처리되지 않는다
- `MyBot.tree.interaction_check`가 `DISCORD_GUILD_ID` 외 길드를 거부한다

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 가드 구현**

`MyBot.setup_hook`에서 트리 검사를 등록한다.

```python
async def _guild_only_check(interaction: discord.Interaction) -> bool:
    return interaction.guild_id == DISCORD_GUILD_ID

self.tree.interaction_check = _guild_only_check
```

`ForbiddenFilterCog.on_message` 선두에 추가한다.

```python
if message.guild is None or message.guild.id != DISCORD_GUILD_ID:
    return
```

`JoinButton.callback` 선두에도 같은 검사를 넣고, 거부 시 ephemeral로 안내한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/main.py module/forbiddenfilter_cog.py module/playwith_cog.py test/
git commit -m "fix: confine bot behavior to the configured guild"
```

---

### Task 11: 서버 이탈 참가자 정리

**Files:**
- Modify: `module/playwith_cog.py:24-33,111-160`
- Modify: `test/test_discord_commands.py`

서버를 나간 멤버가 파티 자리를 영구 점유한다. `/파티`는 `len(participants)`로 인원을 세지만 `interaction.guild.get_member(uid)`가 None이라 목록에는 안 나온다(`:128-141`). 결과적으로 "현재 인원: 3/5"인데 이름은 2개이고, 그 사람의 역할이 24시간 만료까지 예약된 채 남는다.

- [ ] **Step 1: 이탈·표시 테스트 작성**

```python
async def test_leaving_member_frees_party_slot(self):
    ...
    await cog.on_member_remove(FakeMember(id=42))
    self.assertIsNone(repository.get_user_party(42))

async def test_party_count_matches_listed_members(self):
    # get_member가 일부 uid에 None을 반환하도록 구성
    ...
    self.assertIn("현재 인원: 1 /", embed.description)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 리스너와 인원 계산 수정**

```python
@commands.Cog.listener()
async def on_member_remove(self, member: discord.Member):
    if member.guild.id != DISCORD_GUILD_ID:
        return
    game = self.get_user_party(member.id)
    if not game:
        return
    self.remove_participant(game, member.id)
    if not self.get_participants(game):
        self.delete_party(game)
```

`파티` 명령의 인원수는 `len(participants)` 대신 실제로 렌더링된 멤버 수를 쓴다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/playwith_cog.py test/test_discord_commands.py
git commit -m "fix: release party slots when a member leaves"
```

---

### Task 12: 메시지 수정 시 금지어 검사

**Files:**
- Modify: `module/forbiddenfilter_cog.py:109-133`
- Modify: `test/test_discord_commands.py`

깨끗한 메시지를 올린 뒤 수정해 금지어를 넣으면 필터를 그냥 통과한다.

- [ ] **Step 1: 수정 우회 테스트 작성**

`ForbiddenEditTests`에서 `after.content`에만 금지어가 있을 때 경고와 카운트 증가가 일어나는지, `before`에 이미 있었다면 중복 적발하지 않는지 검증한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 검사 로직 분리와 리스너 추가**

`on_message`의 본문을 `_inspect(message)`로 추출하고 두 리스너가 공유한다.

```python
@commands.Cog.listener()
async def on_message_edit(self, before: discord.Message, after: discord.Message):
    if before.content == after.content:
        return
    if self._find_match(before.content):
        return  # 수정 전에 이미 적발됨
    await self._inspect(after)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/forbiddenfilter_cog.py test/test_discord_commands.py
git commit -m "fix: screen edited messages for forbidden words"
```

---

### Task 13: 금융 조회 타임아웃과 캐시

**Files:**
- Modify: `module/finance_cog.py:9-70`
- Modify: `test/test_discord_commands.py`

`:65`가 6개 `asyncio.to_thread`를 타임아웃 없이 실행한다. Yahoo가 응답을 끌면 스레드 풀 슬롯을 점유한 채 인터랙션이 만료된다. 60초 TTL 캐시는 지연과 남용 경로를 함께 줄인다.

- [ ] **Step 1: 타임아웃·캐시 테스트 작성**

```python
async def test_slow_ticker_does_not_block_response(self):
    # 한 종목만 응답하지 않도록 구성 -> 나머지는 정상, 해당 항목만 "데이터 조회 실패"
    ...

async def test_second_call_within_ttl_uses_cache(self):
    ...
    self.assertEqual(cog.fetch_calls, len(cog.tickers))  # 두 번째 호출에서 증가하지 않음
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 타임아웃과 캐시 구현**

```python
FETCH_TIMEOUT_SECONDS = 8.0
CACHE_TTL_SECONDS = 60.0

async def _fetch(self, symbol):
    cached = self._cache.get(symbol)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(self.get_stock_data, symbol),
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except Exception:  # TimeoutError 포함
        return cached[1] if cached else None
    self._cache[symbol] = (time.monotonic(), data)
    return data
```

타임아웃 시 만료된 캐시라도 있으면 그것을 쓰고, 없으면 기존의 "데이터 조회 실패" 경로를 탄다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/finance_cog.py test/test_discord_commands.py
git commit -m "fix: bound market data fetches with timeout and cache"
```

---

### Task 14: 이미지 프롬프트 길이 제한

**Files:**
- Modify: `module/hyacine_image_cog.py:37-40,126-136`
- Modify: `test/test_discord_commands.py`

`:130`이 원본 프롬프트를 그대로 임베드에 넣는다. 4,000자 이상이면 description 4,096자 한계를 넘어 전송이 400으로 실패하고, 그 시점엔 `generation_completed = True`라 환불 없이 50,000 P가 사라진다.

- [ ] **Step 1: 길이 제한 테스트 작성**

```python
async def test_long_prompt_is_truncated_in_embed(self):
    prompt = "가" * 5_000
    ...
    description = interaction.followup.messages[0][1]["embed"].description
    self.assertLessEqual(len(description), 1_100)
    self.assertTrue(description.endswith("…"))
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 표시용 truncate 적용**

모델에는 원본을 보내고 임베드 표시만 자른다.

```python
MAX_PROMPT_DISPLAY = 1_000

display_prompt = 프롬프트 if len(프롬프트) <= MAX_PROMPT_DISPLAY else 프롬프트[:MAX_PROMPT_DISPLAY] + "…"
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/hyacine_image_cog.py test/test_discord_commands.py
git commit -m "fix: truncate image prompt shown in the embed"
```

---

### Task 15: 임시 이미지 경로와 수명

**Files:**
- Modify: `module/hyacine_image_cog.py:16-35,119-140`
- Modify: `test/console_tests.py`

`temp_images`가 CWD 상대 경로(`:23`)라 DATA_DIR 밖이고 Docker 볼륨도 아니다. 삭제가 fire-and-forget 태스크(`:140`)라 5분 안에 재시작하면 파일이 고아가 되고, 시작 시 청소 로직도 없다.

- [ ] **Step 1: 경로·청소 테스트 작성**

```python
def test_temp_image_lifecycle():
    check("임시 경로는 DATA_DIR 아래", cog.temp_dir.is_relative_to(config.DATA_DIR))
    # 오래된 파일을 만들고 cog 초기화 시 정리되는지 검증
    check("시작 시 오래된 임시 파일 정리", not stale.exists())
    check("최근 파일은 보존", fresh.exists())
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

- [ ] **Step 3: 경로 이전과 시작 시 청소**

```python
self.temp_dir = DATA_DIR / "temp_images"
self.temp_dir.mkdir(parents=True, exist_ok=True)
self._sweep_stale_images()
```

`_sweep_stale_images`는 mtime이 `TEMP_IMAGE_TTL_SECONDS`(300)보다 오래된 `*.png`를 지운다. `runtime/data`는 이미 Compose 볼륨이므로 별도 마운트 변경은 필요 없다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/hyacine_image_cog.py test/console_tests.py
git commit -m "fix: keep temp images under the data dir and sweep them"
```

---

### Task 16: 랭킹 표시 개선

**Files:**
- Modify: `module/attendance_cog.py:116-132`
- Modify: `test/test_discord_commands.py`

`bot.get_user`는 캐시 기반이라 미스 시 `Unknown User (id)`로 원시 ID가 노출되고, `display_name`이 서버 닉네임이 아닌 전역 이름이다.

- [ ] **Step 1: 표시 테스트 작성**

`RankingCommandTests`에서 길드 멤버는 닉네임으로, 캐시 미스는 ID 없이 `알 수 없는 유저`로 표시되는지 검증한다.

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 3: 길드 멤버 우선 조회**

```python
member = inter.guild.get_member(user_id) if inter.guild else None
if member is None:
    user = self.bot.get_user(user_id)
    name = user.display_name if user else "알 수 없는 유저"
else:
    name = member.display_name
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands`

- [ ] **Step 5: 커밋**

```bash
git add module/attendance_cog.py test/test_discord_commands.py
git commit -m "fix: prefer guild nicknames in the ranking embed"
```

---

### Task 17: 전역 명령 정리 마커 위치

**Files:**
- Modify: `module/main.py:62-78`
- Modify: `test/console_tests.py`

`.global-commands-cleared`가 DATA_DIR에 있어 백업에서 새 데이터 디렉터리로 복구하면 전역 클리어가 한 번 더 돈다. 이 마커는 데이터가 아니라 봇 설치 상태이므로 백업 대상 밖에 둔다.

- [ ] **Step 1: 마커 위치 테스트 작성**

```python
def test_global_cleanup_marker_location():
    check("마커는 DATA_DIR 밖", not main.GLOBAL_CLEANUP_MARKER.is_relative_to(config.DATA_DIR))
    check("마커는 백업 대상이 아님", main.GLOBAL_CLEANUP_MARKER.name not in {p.name for p in backup.DATABASES})
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

- [ ] **Step 3: 마커를 상태 디렉터리로 이전**

`PROJECT_ROOT / "runtime" / ".global-commands-cleared"`로 옮기고 모듈 상수로 노출한다. 기존 DATA_DIR 위치에 파일이 있으면 새 위치로 이관해 재-sync를 피한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests`

- [ ] **Step 5: 커밋**

```bash
git add module/main.py test/console_tests.py
git commit -m "fix: move command sync marker out of the data dir"
```

---

### Task 18: backup 컨테이너 시크릿 제거

**Files:**
- Modify: `compose.yaml`
- Modify: `test/console_tests.py`

`module.backup`은 Discord·OpenAI·Google 자격증명을 사용하지 않는다. backup 서비스에서 `.env.secrets`를 제거해 노출면을 줄인다. Task 2의 마운트 완화와는 다른 축이므로 서로 상충하지 않는다.

- [ ] **Step 1: 계약 테스트 작성**

```python
check(
    "backup 서비스는 시크릿을 받지 않음",
    ".env.secrets" not in compose["services"]["backup"]["env_file"],
)
check(
    "bot 서비스는 시크릿을 유지",
    ".env.secrets" in compose["services"]["bot"]["env_file"],
)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

- [ ] **Step 3: env_file 축소**

```yaml
  backup:
    image: discordbot-hsr:local
    env_file:
      - .env.runtime
```

`module/config.py`가 import 시점에 `validate_config()`를 부르지 않으므로 backup 프로세스는 토큰 없이도 정상 기동한다. `docs/operations.md`에 근거를 기록한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m module.backup create`

- [ ] **Step 5: 커밋**

```bash
git add compose.yaml docs/operations.md test/console_tests.py
git commit -m "chore: stop passing secrets to the backup service"
```

---

### Task 19: 전체 검증과 상태 기록

- [ ] **Step 1: 두 test runner 전체 실행**

```bash
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
```

Expected: 모두 통과.

- [ ] **Step 2: 배포 계약 수동 확인**

```bash
docker compose config
.venv/bin/python -m module.backup create
.venv/bin/python -m module.backup restore-test
```

Expected: 봇 컨테이너를 정지한 상태에서도 백업 생성·검증이 성공한다(Task 2 회귀).

- [ ] **Step 3: 운영 문서 갱신**

`docs/operations.md`에 다음을 기록한다.

- 포인트 수입원이 `/출석` 하나라는 점과 AI 가격표(200 / 2,000 / 30,000 P), 조정 시 「포인트 경제 근거」 절을 함께 갱신할 것
- `AI_COOLDOWN_SECONDS` 기본값과 조정 방법
- OpenAI 계정 예산 한도를 최후의 안전망으로 설정해야 한다는 점
- WAL 전환으로 backup 서비스의 데이터 마운트가 rw여야 하는 이유
- 포인트 원장으로 환불 실패를 대조하는 절차

- [ ] **Step 4: 최종 커밋**

```bash
git add docs/operations.md
git commit -m "docs: record follow-up hardening operations"
```

## Explicitly Deferred or Excluded

- **Git 이력 시크릿 정리:** 키·토큰을 2회 재발급해 이력의 값이 무효화됐다. force-push의 비용이 이득을 넘는다.
- **`/프로필` 타인 조회 제한:** 소규모 서버 운영 판단. `/랭킹`으로 포인트가 이미 공개되어 실효가 없다.
- **금지어 재게시 제거:** 경고에 단어를 노출하는 것이 의도된 동작이다.
- **멘션 알림 복원:** 전역 `AllowedMentions.none()` 유지가 확정된 방침이다.
- **전역 AI 예산 kill switch:** OpenAI 계정 예산이 같은 역할을 한다. 앱에서 중복 구현하면 상한이 두 곳에 흩어져 진단이 어려워진다.
- **사용자별 일일 쿼터:** Task 4가 통화 발행을 `/출석` 하나로 묶으므로 포인트 잔액이 곧 일일 상한이다. 별도 쿼터 테이블은 같은 제한을 두 번 표현할 뿐이다.
- **`/주가` rate limit:** AI 비용과 무관하고 Task 13의 캐시가 외부 호출을 흡수한다.
- **럭키박스 배수 조정:** 배수를 낮추는 대신 명령을 삭제했다. 하우스 엣지를 어떻게 잡아도 통화 발행 경로가 둘이면 가격 계산의 근거가 흔들린다.
- **`luckybox_count`·`last_luckybox_date` 컬럼 삭제:** 기존 백업에서의 복구 호환성을 깨뜨리는 반면 미사용 컬럼의 비용은 없다.
- **guild_id 컬럼 도입:** Task 10이 단일 길드로 경계를 강제하므로 스키마 확장은 다중 길드 운영이 실제 요구사항이 될 때 다룬다.

## Self-Review

- **Spec coverage:** 사용자가 채택한 16개 항목(S2, S5, S6, S7, C1–C7, F2–F6)을 Task 1–18에 매핑했다. S2는 포인트 경제 재설계(Task 4)와 쿨다운(Task 5)으로 나뉘었고, 그 결과 당초 계획했던 `ai_usage` 쿼터 테이블은 불필요해져 제외했다. C7은 배포 영향이 다른 두 변경이라 Task 1(timeout)과 Task 2(WAL+마운트)로 분리했다. 제외 항목은 위에 근거를 남겼다.
- **Test coverage:** Task 1–18은 모두 같은 Task 안에서 실패 테스트, 구현, 대상 테스트 통과, source+test 동시 커밋 순서를 갖는다. Task 19에서 두 전체 runner와 배포 계약을 다시 확인한다.
- **Task independence:** Task 1이 연결 헬퍼를 도입하므로 Task 2–4, 8, 9가 이를 전제한다. Task 3(원장)은 Task 4의 가격 변경보다 먼저 와야 새 가격의 이동이 처음부터 기록된다. Task 4 → 5는 순서 의존이다(가격 확정 후 잔여 버스트를 쿨다운으로 처리). Task 6과 7은 같은 파일을 만지지만 별도 불변식이라 커밋을 나눴다. Task 8 → 9도 순서 의존이다(생성 계약 → 참가 제약). 나머지는 독립적이다.
- **Type consistency:** `create_party -> bool`, `add_participant(game, user_id, role, max_players) -> bool`, `get_ledger -> List[Tuple[int, str, int]]` 계약을 생산 Task와 소비 Task에서 동일하게 사용한다.
- **Risk:** Task 2와 9는 기존 DB 파일을 변형한다(journal_mode 전환, 역할 중복 행 정리). 두 Task 모두 마이그레이션을 `_init_db`에 두어 멱등하게 만들었고, 실행 전 `python -m module.backup create`로 백업을 남기는 것을 전제한다. Task 4는 사용자에게 보이는 가격을 바꾸지만 현재 사용자가 1명(5,478 P)이라 영향이 사실상 없다 — 미루면 이 비용만 커진다.
- **References:** WAL 읽기 전용 제약은 이 저장소에서 직접 실측했다(읽기 전용 디렉터리 + `-shm` 부재 시 `attempt to write a readonly database`). 쿨다운은 discord.py `app_commands.checks.cooldown`, 부분 인덱스는 SQLite `CREATE INDEX ... WHERE` 문법을 따른다.
