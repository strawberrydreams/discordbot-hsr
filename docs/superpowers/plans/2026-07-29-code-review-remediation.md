# Code Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docs/code-review-2026-07-29.md`에서 확인된 실제 코드·배포 결함을 수정하고 private 금지어 파일을 Git 작업 트리와 분리해 보안, 데이터 일관성, 이벤트 루프 응답성, 신규 설치 안정성을 확보한다.

**Architecture:** 기존 Cog/Repository 구조와 표준 라이브러리를 유지하며 결함이 발생하는 가장 낮은 공통 계층에서 수정한다. Discord 상호작용은 Cog 테스트로, 데이터 일관성·시작 순서·배포 계약은 임시 SQLite DB를 사용하는 콘솔 테스트로 검증한다.

**Tech Stack:** Python 3.12, discord.py 2.7.1, SQLite, OpenAI Responses API, Google Gen AI SDK, Docker Compose, launchd

## Global Constraints

- 새 Python 의존성을 추가하지 않는다.
- 기존 SQLite 파일과 백업 형식을 유지하며 운영 DB를 테스트에서 열지 않는다.
- 모든 Discord 사용자 입력 에코는 역할·사용자·전체 멘션을 발생시키지 않아야 한다.
- 포인트 차감·환불과 출석 지급은 중복 요청 및 전송 실패에서도 정확히 한 번만 반영되어야 한다.
- `python -m unittest test.test_discord_commands`와 `python -m test.console_tests`를 모두 통과시킨다.
- 각 작업은 해당 테스트를 먼저 실패시킨 뒤 최소 구현으로 통과시키고 별도 커밋한다.
- 동작 코드나 배포 계약을 수정하는 커밋은 해당 회귀 테스트 변경을 같은 커밋에 포함한다. 테스트 없는 동작 변경은 완료로 처리하지 않는다.
- 실제 `forbidden_words.json`은 Git에 커밋하지 않고 `runtime/data/forbidden_words.json`에서 유지한다.

## File Responsibility Map

- `module/main.py`: 봇 전역 멘션 정책, DB 시작 검증, 단일 인스턴스 잠금, 길드 명령 동기화.
- `module/config.py`, `.env.example`: 단일 운영 길드 ID와 runtime 금지어 경로 설정 및 검증.
- `module/playwith_cog.py`, `module/database.py`: 파티 생명주기, 참가 무결성, UTC epoch 저장, 출석 원자성.
- `module/hyacine_image_cog.py`: 이미지 생성 비용의 정확히 한 번 환불과 생성/전송 단계 구분.
- `module/hyacine_chat_cog.py`: 제한된 채널 세션 캐시와 만료 가능한 CDN URL 제거.
- `module/eventnotice_cog.py`: 결정적 이벤트 정렬, 목록 및 상세 조회.
- `module/finance_cog.py`: 동기 금융 HTTP 호출의 스레드 오프로딩과 병렬 실행.
- `module/forbiddenfilter_cog.py`: 실제 운영 로그에 보이는 금지어 로드 메시지.
- `compose.yaml`, `deploy/macos/*.plist.example`: 단일 이미지 빌드, runtime 데이터 마운트, 로그 제한, 단일 백업 주기 설정.
- `deploy/macos/com.discordbot.hsr.newsyslog.conf.example`: launchd 로그 로테이션 예시.
- `docs/operations.md`: 변경된 Docker/launchd 운영 계약.
- `test/test_discord_commands.py`, `test/console_tests.py`: 회귀 및 동시성 검증.
- `settings/forbidden_words.example.json`: Git이 추적하는 신규 설치용 예시 목록.
- `runtime/data/forbidden_words.json`: Git이 추적하지 않는 실제 운영 목록.
- `single/command_prefix.py`, `test/hello_prefix.py`, `test/makejson.py`: 연결되지 않은 실험 코드로 삭제.

## Required Source-to-Test Mapping

| Task | 동작·배포 변경 | 같은 커밋에 포함할 테스트 |
|---|---|---|
| 1 | `module/main.py` 멘션 정책 | `test/console_tests.py::test_bot_disables_all_mentions` |
| 2 | 파티 조회·참가 무결성 | `PartyInteractionTests`, `test_party_repository` |
| 3 | 이미지 환불·전송 단계 | `ImageCommandTests` |
| 4 | 이벤트 목록·정렬 | `CommandPrivacyTests` 이벤트 케이스 |
| 5 | 금융 병렬 호출 | `FinanceCommandTests` |
| 6 | 출석 원자성 | `test_attendance_atomicity` |
| 7 | 대화 세션·이미지 history | `test_channel_sessions`, `ChatCommandTests` |
| 8 | 파티 epoch 마이그레이션 | `test_party_repository` legacy 시간 케이스 |
| 9 | 신규 DB·인스턴스 잠금 | startup 및 lock 콘솔 테스트 |
| 10 | 길드 command sync | config/env 및 FakeTree startup 테스트 |
| 11 | 금지어 로그 | `test_forbidden_words_fail_fast` 인접 stdout 테스트 |
| 12 | runtime 금지어 경로 | config/env/Git ignore/Compose 계약 테스트 |
| 13 | Docker·launchd 계약 | `test_deployment_contracts` |

---

### Task 1: 봇 전역 멘션 차단

**Files:**
- Modify: `module/main.py:24-30`
- Modify: `test/console_tests.py:291-300`

**Interfaces:**
- Consumes: `discord.AllowedMentions.none()`
- Produces: `MyBot`이 보내는 모든 메시지에 적용되는 `allowed_mentions` 기본값

- [ ] **Step 1: 봇 생성자 계약 테스트 작성**

`test/console_tests.py`에 다음 검사를 추가하고 실행 목록에 등록한다.

```python
def test_bot_disables_all_mentions():
    import module.main as main
    from discord.ext import commands

    with patch.object(commands.Bot, "__init__", return_value=None) as init:
        main.MyBot()

    allowed = init.call_args.kwargs["allowed_mentions"]
    check("봇 전역 멘션 차단", allowed.everyone is False and allowed.roles is False and allowed.users is False)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `봇 전역 멘션 차단` 실패 또는 `allowed_mentions` 키 누락.

- [ ] **Step 3: 봇 생성자에서 멘션을 전역 차단**

`MyBot.__init__`의 `super().__init__`에 다음 인자를 추가한다.

```python
allowed_mentions=discord.AllowedMentions.none(),
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

- [ ] **Step 5: 커밋**

```bash
git add module/main.py test/console_tests.py
git commit -m "fix: disable bot mentions globally"
```

---

### Task 2: 유령 파티와 고아 참가자 차단

**Files:**
- Modify: `module/playwith_cog.py:111-164,223-285`
- Modify: `module/database.py:95-97,311-316`
- Modify: `test/test_discord_commands.py:283-319`
- Modify: `test/console_tests.py:414-454`

**Interfaces:**
- Consumes: `PartyRepository.get_party(game)`
- Produces: `PartyRepository.add_participant(game, user_id, role) -> bool`

- [ ] **Step 1: 종료된 파티 버튼과 빈 파티 조회 테스트 작성**

`PartyInteractionTests`에 다음 두 테스트를 추가한다.

```python
async def test_party_status_keeps_empty_active_party(self):
    with tempfile.TemporaryDirectory() as directory:
        repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
        game = next(iter(playwith_cog.GAMES))
        repository.create_party(game, 1_000)
        with patch("discord.ext.tasks.Loop.start"):
            cog = PlayWithCog(bot=None, repository=repository)
        interaction = FakeInteraction(channel_id=1, guild=FakeGuild())

        with patch.object(playwith_cog, "RECRUIT_CHANNEL_ID", 1):
            await PlayWithCog.파티.callback(cog, interaction)

        self.assertIsNotNone(repository.get_party(game))
        self.assertEqual(interaction.response.messages[0][1]["embeds"][0].description,
                         f"현재 인원: 0 / {playwith_cog.GAMES[game]['max_players']}")

async def test_stale_join_button_rejects_deleted_party(self):
    with tempfile.TemporaryDirectory() as directory:
        repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
        game = next(iter(playwith_cog.GAMES))
        with patch("discord.ext.tasks.Loop.start"):
            cog = PlayWithCog(bot=None, repository=repository)
        interaction = FakeInteraction(channel_id=1)

        await cog.shared_views[game].children[0].callback(interaction)

        self.assertIn("모집이 종료된 파티", interaction.response.messages[0][0][0])
        self.assertIsNone(repository.get_user_party(interaction.user.id))
```

`test_party_repository()`에는 DB 계층의 고아 참가자 차단 검사를 추가한다.

```python
check("없는 파티 참가 거부", repo.add_participant("missing", 99) is False)
check("거부된 참가자는 고아 행을 남기지 않음", repo.get_user_party(99) is None)
```

- [ ] **Step 2: 대상 테스트가 실패하는지 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.PartyInteractionTests && .venv/bin/python -m test.console_tests`

Expected: 빈 파티가 삭제되고, 종료된 파티 버튼 및 Repository가 참가를 허용해 실패.

- [ ] **Step 3: 조회 중 삭제를 제거하고 DB 참가를 조건부 처리**

`/파티`의 `if not participants:` 삭제 분기를 제거해 참가자 0명인 활성 파티도 embed에 포함한다.

`AttendanceRepository`가 아닌 `PartyRepository`와 SQLite 구현의 반환 계약을 다음처럼 바꾼다.

```python
def add_participant(self, game: str, user_id: int, role: Optional[str] = None) -> bool:
    """존재하는 파티에만 참가자를 추가하거나 역할을 갱신한다."""
```

```python
def add_participant(self, game: str, user_id: int, role: Optional[str] = None) -> bool:
    with sqlite3.connect(self.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO participants (game, user_id, role)
            SELECT ?, ?, ?
            WHERE EXISTS (SELECT 1 FROM parties WHERE game = ?)
            """,
            (game, user_id, role, game),
        )
        conn.commit()
        return cursor.rowcount > 0
```

Cog 위임 메서드도 `bool`을 반환하게 한다.

```python
def add_participant(self, game, user_id, role=None):
    return self.db.add_participant(game, user_id, role)
```

- [ ] **Step 4: 모든 참가 UI에서 종료된 파티를 먼저 거부**

`JoinButton.callback`의 첫 분기에 다음 검사를 넣고, `RoleSelect.callback`에서도 동일 검사를 수행한다.

```python
if not self.cog.get_party(game):
    await interaction.response.send_message("❌ 모집이 종료된 파티입니다.", ephemeral=True)
    return
```

실제 추가 시 조건부 INSERT가 실패한 경우도 같은 메시지를 보낸다.

```python
if not self.cog.add_participant(game, user_id, role):
    await interaction.response.send_message("❌ 모집이 종료된 파티입니다.", ephemeral=True)
    return
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.PartyInteractionTests && .venv/bin/python -m test.console_tests`

Expected: 전체 통과.

- [ ] **Step 6: 커밋**

```bash
git add module/playwith_cog.py module/database.py test/test_discord_commands.py test/console_tests.py
git commit -m "fix: prevent ghost party participants"
```

---

### Task 3: 이미지 비용을 정확히 한 번 정산

**Files:**
- Modify: `module/hyacine_image_cog.py:42-114`
- Modify: `test/test_discord_commands.py`

**Interfaces:**
- Consumes: `AttendanceCog.deduct_points`, `AttendanceCog.add_points`
- Produces: 생성 실패 시 최대 한 번 환불, 생성 성공 뒤 Discord 전송 실패 시 미환불

- [ ] **Step 1: 이중 환불과 전송 실패 정산 테스트 작성**

`RecordingFollowup`에 선택적 실패 횟수를 넣는다.

```python
class RecordingFollowup:
    def __init__(self, fail_on_call=None):
        self.messages = []
        self.fail_on_call = fail_on_call

    async def send(self, *args, **kwargs):
        if self.fail_on_call == len(self.messages) + 1:
            raise RuntimeError("followup transport failed")
        self.messages.append((args, kwargs))
```

새 `ImageCommandTests`에서 다음을 검증한다.

```python
async def test_empty_image_response_refunds_only_once_when_error_message_fails(self):
    attendance = RecordingAttendance()
    interaction = FakeInteraction(channel_id=1)
    interaction.followup = RecordingFollowup(fail_on_call=1)
    cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
    temp_dir = tempfile.TemporaryDirectory()
    self.addCleanup(temp_dir.cleanup)
    cog.temp_dir = temp_dir.name
    cog.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_: SimpleNamespace(parts=[])
    ))

    with patch("module.hyacine_image_cog.print"), patch("module.hyacine_image_cog.traceback.print_exc"):
        await HyacineImageCog._image.callback(cog, interaction, "test")

    self.assertEqual(attendance.refunds, [(123, 50_000)])

async def test_generated_image_is_not_refunded_when_discord_upload_fails(self):
    attendance = RecordingAttendance()
    interaction = FakeInteraction(channel_id=1)
    interaction.followup = RecordingFollowup(fail_on_call=1)
    cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
    temp_dir = tempfile.TemporaryDirectory()
    self.addCleanup(temp_dir.cleanup)
    cog.temp_dir = temp_dir.name
    part = SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))
    cog.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_: SimpleNamespace(parts=[part])
    ))

    with patch("module.hyacine_image_cog.print"), patch("module.hyacine_image_cog.traceback.print_exc"):
        await HyacineImageCog._image.callback(cog, interaction, "test")

    self.assertEqual(attendance.refunds, [])
```

테스트 import에 `HyacineImageCog`를 추가한다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.ImageCommandTests`

Expected: 빈 응답 오류 메시지 실패에서 환불 2회, 업로드 실패에서 환불 1회.

- [ ] **Step 3: 환불 함수와 생성 완료 플래그 구현**

`_image`에서 차감 직후 아래 상태를 유지한다.

```python
charged = True
refunded = False
generation_completed = False
filepath = None

def refund_points() -> bool:
    nonlocal refunded
    if not charged or refunded or generation_completed:
        return False
    attendance_cog.add_points(inter.user.id, cost)
    refunded = True
    return True
```

이미지 바이트를 확보한 직후 `generation_completed = True`로 바꾼다. 빈 응답과 생성 API 예외에서만 `refund_points()`를 호출하며, 바깥 예외 처리도 같은 함수를 사용한다. Discord 전송 실패 메시지는 “이미지는 생성되었지만 Discord 전송에 실패했습니다.”로 구분하고 환불 문구를 붙이지 않는다.

- [ ] **Step 4: 실패 시 임시 파일 즉시 정리**

업로드 성공 여부를 기록하고, 업로드 전에 실패한 파일은 `finally`에서 삭제한다.

```python
uploaded = False
try:
    # save and send
    await inter.followup.send(embed=embed, file=file)
    uploaded = True
finally:
    if filepath and not uploaded:
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass
```

성공한 파일만 기존 5분 후 삭제 task를 등록한다.

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.ImageCommandTests`

Expected: 전체 통과.

- [ ] **Step 6: 커밋**

```bash
git add module/hyacine_image_cog.py test/test_discord_commands.py
git commit -m "fix: settle image generation charges once"
```

---

### Task 4: 이벤트 목록과 결정적 번호 제공

**Files:**
- Modify: `module/eventnotice_cog.py:11-71`
- Modify: `test/test_discord_commands.py:52-61,271-280`

**Interfaces:**
- Consumes: `Guild.fetch_scheduled_events()`
- Produces: `/이벤트 [index]`; index 생략 시 정렬된 목록, 지정 시 같은 정렬 기준의 상세 정보

- [ ] **Step 1: 정렬 및 목록 테스트 작성**

테스트 이벤트를 생성하는 helper를 추가한다.

```python
def fake_event(event_id, name, start_time, end_time=None):
    return SimpleNamespace(
        id=event_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        description=None,
        creator=None,
        location=None,
        cover_image=None,
    )
```

`FakeGuild`가 생성자에서 events를 받아 반환하게 한 뒤 다음 테스트를 추가한다.

```python
async def test_event_list_and_detail_use_start_time_then_id_order(self):
    later = datetime(2026, 8, 2, tzinfo=timezone.utc)
    earlier = datetime(2026, 8, 1, tzinfo=timezone.utc)
    guild = FakeGuild([
        fake_event(2, "두 번째", later),
        fake_event(3, "같은 시각 뒤", earlier),
        fake_event(1, "같은 시각 앞", earlier),
    ])
    cog = EventNoticeCog(bot=None)

    listing = FakeInteraction(channel_id=1, guild=guild)
    detail = FakeInteraction(channel_id=1, guild=guild)
    with patch("module.eventnotice_cog.EVENT_CHANNEL_ID", 1):
        await EventNoticeCog.show_specific_event.callback(cog, listing, None)
        await EventNoticeCog.show_specific_event.callback(cog, detail, 1)

    list_text = listing.followup.messages[0][1]["embed"].description
    self.assertLess(list_text.index("같은 시각 앞"), list_text.index("같은 시각 뒤"))
    self.assertLess(list_text.index("같은 시각 뒤"), list_text.index("두 번째"))
    self.assertIn("같은 시각 앞", detail.followup.messages[0][1]["embed"].title)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.CommandPrivacyTests`

Expected: `index=None` 비교에서 실패하거나 목록 embed가 없어 실패.

- [ ] **Step 3: 유효 이벤트를 한 곳에서 정렬**

Cog에 다음 helper를 추가한다.

```python
@staticmethod
def _valid_events(events, now):
    return sorted(
        (event for event in events if not event.end_time or event.end_time > now),
        key=lambda event: (event.start_time, event.id),
    )
```

명령 인자를 선택값으로 바꾼다.

```python
async def show_specific_event(
    self,
    interaction: discord.Interaction,
    index: Optional[int] = None,
):
```

`typing.Optional`을 import하고 `index is None`이면 최대 25개를 번호, 이름, 시작 시각으로 표시한다.

```python
lines = [
    f"`{number}.` **{event.name}** — <t:{int(event.start_time.timestamp())}:F>"
    for number, event in enumerate(valid_events[:25], start=1)
]
if len(valid_events) > 25:
    lines.append(f"외 {len(valid_events) - 25}개")
embed = discord.Embed(
    title="📅 서버 이벤트 목록",
    description="\n".join(lines),
    color=discord.Color.blue(),
)
await interaction.followup.send(embed=embed)
return
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.CommandPrivacyTests`

Expected: 전체 통과.

- [ ] **Step 5: 커밋**

```bash
git add module/eventnotice_cog.py test/test_discord_commands.py
git commit -m "feat: add deterministic event listing"
```

---

### Task 5: 금융 HTTP 호출을 이벤트 루프 밖에서 병렬 실행

**Files:**
- Modify: `module/finance_cog.py:1-104`
- Modify: `test/test_discord_commands.py`

**Interfaces:**
- Consumes: 동기 `FinanceCog.get_stock_data(symbol)`
- Produces: `asyncio.to_thread` 작업 6개를 `asyncio.gather`로 동시에 대기

- [ ] **Step 1: 여섯 호출이 동시에 시작되는 테스트 작성**

```python
class FinanceCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tickers_start_before_any_result_is_released(self):
        cog = FinanceCog(bot=None)
        interaction = FakeInteraction(channel_id=1)
        started = 0
        all_started = asyncio.Event()

        async def controlled_to_thread(function, symbol):
            nonlocal started
            started += 1
            if started == len(cog.tickers):
                all_started.set()
            await all_started.wait()
            return {"price": 1.0, "change": 0.0, "change_percent": 0.0}

        with patch("module.finance_cog.asyncio.to_thread", side_effect=controlled_to_thread):
            await asyncio.wait_for(
                FinanceCog.stock_price.callback(cog, interaction),
                timeout=1,
            )

        self.assertEqual(started, len(cog.tickers))
```

`asyncio`와 `FinanceCog` import를 추가한다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.FinanceCommandTests`

Expected: 기존 코드는 `asyncio.to_thread`를 호출하지 않아 `started == 0`.

- [ ] **Step 3: `to_thread`와 `gather`로 호출**

`module/finance_cog.py`에 `asyncio`를 import하고 순차 loop 앞에서 결과를 만든다.

```python
items = list(self.tickers.items())
results = await asyncio.gather(
    *(asyncio.to_thread(self.get_stock_data, symbol) for _, symbol in items)
)

for (name, _), data in zip(items, results):
    if data:
        price = data["price"]
        change = data["change"]
        pct = data["change_percent"]
        emoji = "🔺" if change > 0 else "🔹" if change < 0 else "➖"
        if "국채" in name:
            value_str = f"{price:.3f}%"
        elif "비트코인" in name:
            value_str = f"${price:,.2f}"
        else:
            value_str = f"{price:,.2f}"
        embed.add_field(
            name=f"{name} {emoji}",
            value=f"**{value_str}**\n`{change:+.2f} ({pct:+.2f}%)`",
            inline=True,
        )
    else:
        embed.add_field(name=name, value="데이터 조회 실패", inline=True)
```

데이터 조회 예외는 기존 `get_stock_data`가 `None`으로 변환하므로 `gather`에 별도 예외 분기를 추가하지 않는다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m unittest test.test_discord_commands.FinanceCommandTests`

Expected: 전체 통과.

- [ ] **Step 5: 커밋**

```bash
git add module/finance_cog.py test/test_discord_commands.py
git commit -m "fix: fetch market data without blocking"
```

---

### Task 6: 출석 지급을 원자적 조건부 UPSERT로 변경

**Files:**
- Modify: `module/database.py:44-50,173-196`
- Modify: `module/attendance_cog.py:40-71`
- Modify: `test/console_tests.py:303-413`

**Interfaces:**
- Removes: `get_attendance`, `update_attendance`
- Produces: `claim_attendance(user_id: int, reward: int, attendance_date: str) -> Optional[int]`

- [ ] **Step 1: 동시 출석 테스트 작성**

```python
def test_attendance_atomicity(repo: SQLiteAttendanceRepository):
    user_id = 150
    results = []
    lock = threading.Lock()

    def worker():
        result = repo.claim_attendance(user_id, 10_000, "2026-07-29")
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check("동시 출석은 정확히 한 번 성공", sum(result is not None for result in results) == 1)
    check("동시 출석 포인트는 한 번만 지급", repo.get_points(user_id) == 10_000)
```

`test_migration()` 반환 Repository로 이 검사를 실행 목록에 추가한다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `claim_attendance`가 없어 실패.

- [ ] **Step 3: Repository 계약과 SQLite UPSERT 구현**

기존 두 출석 메서드를 다음 하나로 교체한다.

```python
@abstractmethod
def claim_attendance(
    self,
    user_id: int,
    reward: int,
    attendance_date: str,
) -> Optional[int]:
    """당일 첫 출석이면 포인트를 지급하고 새 잔액을, 중복이면 None을 반환한다."""
```

```python
def claim_attendance(
    self,
    user_id: int,
    reward: int,
    attendance_date: str,
) -> Optional[int]:
    with sqlite3.connect(self.db_path) as conn:
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
```

- [ ] **Step 4: Cog의 read-modify-write 제거**

보상값을 만든 뒤 Repository 반환값으로 중복과 성공을 구분한다.

```python
reward = random.randint(5_000, 30_000)
new_points = self.db.claim_attendance(user_id, reward, today_str)
if new_points is None:
    await inter.response.send_message(
        f"🛑 {inter.user.mention}, 오늘은 이미 출석하셨어요! 내일 또 오세요~",
        ephemeral=True,
    )
    return
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands.CommandPrivacyTests`

Expected: 전체 통과.

- [ ] **Step 6: 커밋**

```bash
git add module/database.py module/attendance_cog.py test/console_tests.py
git commit -m "fix: make attendance claims atomic"
```

---

### Task 7: 대화 세션 수 제한과 CDN URL 제거

**Files:**
- Modify: `module/hyacine_chat_cog.py:3-4,57-67,174-176`
- Modify: `test/console_tests.py:493-513`
- Modify: `test/test_discord_commands.py:99-231`

**Interfaces:**
- Consumes: 채널 ID와 현재 턴의 OpenAI input parts
- Produces: 최근 사용한 최대 100개 채널 세션, 텍스트만 남긴 과거 user history

- [ ] **Step 1: LRU 제한 및 이미지 URL 비보존 테스트 작성**

`test_channel_sessions()`에 추가한다.

```python
for channel_id in range(cog.MAX_CHANNEL_SESSIONS + 1):
    cog.get_session(channel_id)
check("채널 세션 수 제한", len(cog.sessions) == cog.MAX_CHANNEL_SESSIONS)
check("가장 오래된 세션 제거", 0 not in cog.sessions)
```

`ChatCommandTests`에 이미지 턴 저장 검사를 추가한다.

```python
async def test_image_url_is_sent_once_but_not_saved_in_history(self):
    interaction = FakeInteraction(channel_id=1)
    attachment = SimpleNamespace(content_type="image/png", url="https://cdn.example/signed.png")
    captured = {}

    async def response(**kwargs):
        captured["input"] = kwargs["input"]
        return SimpleNamespace(
            output_text="확인했습니다.",
            model="gpt-5.6-terra",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    self.cog.client = SimpleNamespace(responses=SimpleNamespace(create=response))
    await self.cog._run_talk(interaction, "", attachment, "gpt-5.6-terra", "none", 0)

    self.assertIn("image_url", repr(captured["input"]))
    self.assertNotIn("image_url", repr(list(self.cog.get_session(1).history)))
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands.ChatCommandTests`

Expected: 세션이 101개가 되고 저장 history에 `image_url`이 남아 실패.

- [ ] **Step 3: 세션 저장소를 제한된 LRU로 변경**

```python
from collections import OrderedDict, deque

class HyacineChatCog(commands.Cog):
    MAX_CHANNEL_SESSIONS = 100

    def __init__(...):
        self.sessions: OrderedDict[int, ChannelSession] = OrderedDict()

    def get_session(self, channel_id: int) -> ChannelSession:
        session = self.sessions.pop(channel_id, None)
        if session is None:
            session = ChannelSession(self.system_prompt)
        self.sessions[channel_id] = session
        if len(self.sessions) > self.MAX_CHANNEL_SESSIONS:
            self.sessions.popitem(last=False)
        return session
```

- [ ] **Step 4: 현재 요청에는 이미지를 보내되 history에는 텍스트만 저장**

OpenAI 응답을 받은 뒤 history에 넣기 직전에 저장용 parts를 만든다.

```python
history_parts = [
    part for part in parts
    if part.get("type") == "input_text"
]
if not history_parts:
    history_parts = [{"type": "input_text", "text": "(이전 턴에 이미지 첨부됨)"}]

session.history.append({"role": "user", "content": history_parts})
session.history.append({"role": "assistant", "content": reply})
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands.ChatCommandTests`

Expected: 전체 통과.

- [ ] **Step 6: 커밋**

```bash
git add module/hyacine_chat_cog.py test/console_tests.py test/test_discord_commands.py
git commit -m "fix: bound chat sessions and drop stale image urls"
```

---

### Task 8: 파티 시각을 UTC epoch로 저장

**Files:**
- Modify: `module/playwith_cog.py:1-41`
- Modify: `module/database.py:16-21,76-109,256-295,331-342`
- Modify: `test/console_tests.py:414-454`

**Interfaces:**
- Consumes: Unix epoch seconds
- Produces: `create_party(game: str, created_at: int)`, `delete_expired_parties(cutoff: int)`

- [ ] **Step 1: epoch 저장과 legacy KST 마이그레이션 테스트 작성**

`test_party_repository()`에서 datetime 인자를 epoch 정수로 바꾸고 다음 검사를 추가한다.

```python
legacy_db = _TMP_DIR / "party_legacy_time.db"
with sqlite3.connect(legacy_db) as conn:
    conn.execute("CREATE TABLE parties (game TEXT PRIMARY KEY, created_at TIMESTAMP)")
    conn.execute(
        "CREATE TABLE participants (game TEXT, user_id INTEGER, role TEXT, PRIMARY KEY (game, user_id))"
    )
    conn.execute("INSERT INTO parties VALUES (?, ?)", ("Legacy", "2026-07-29 12:00:00"))
repo_with_legacy = SQLitePartyRepository(legacy_db)
legacy_value = repo_with_legacy.get_party("Legacy")[0]
check("legacy 파티 시각을 epoch로 변환", isinstance(legacy_value, int))
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 기존 문자열 값이 그대로 남아 실패.

- [ ] **Step 3: Repository 초기화에서 기존 naive KST 문자열 변환**

`datetime`과 `zoneinfo.ZoneInfo`를 import하고 `_init_db`에서 다음 변환을 한 번 수행한다.

```python
cursor.execute("SELECT game, created_at FROM parties")
for game, value in cursor.fetchall():
    if isinstance(value, int):
        continue
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    cursor.execute(
        "UPDATE parties SET created_at = ? WHERE game = ?",
        (int(parsed.timestamp()), game),
    )
```

기존 운영 코드가 KST 호스트의 `datetime.now()`를 저장했으므로 naive legacy 값은 `Asia/Seoul`로 해석한다.

- [ ] **Step 4: Cog에서 epoch만 전달**

```python
import time

expiration_time = int(time.time()) - 24 * 60 * 60
self.db.delete_expired_parties(expiration_time)

def create_party(self, game):
    self.db.create_party(game, int(time.time()))
```

Repository의 `Any` 시각 타입을 `int`로 좁힌다.

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

- [ ] **Step 6: 커밋**

```bash
git add module/playwith_cog.py module/database.py test/console_tests.py
git commit -m "fix: store party timestamps as utc epoch"
```

---

### Task 9: 신규 DB 시작 허용과 이중 인스턴스 차단

**Files:**
- Modify: `module/main.py:1-65`
- Modify: `test/console_tests.py:193-300`

**Interfaces:**
- Consumes: `DATA_DIR`, `fcntl.flock`
- Produces: `_verify_databases(existing_only=False)`, `acquire_instance_lock(path) -> BinaryIO`

- [ ] **Step 1: 빈 DATA_DIR 시작 순서 테스트 수정**

기존 시작 검증 테스트를 두 경우로 나눈다.

```python
def verify_existing(path, tables):
    events.append(f"verify:{path.name}")
    return {}

with patch.object(main.Path, "exists", return_value=False), \
     patch.object(main, "verify_database", side_effect=verify_existing):
    asyncio.run(main.MyBot.setup_hook(FakeBot()))

check("신규 설치는 사전 검증 생략", not any(event.startswith("verify:") for event in events[:len(main.EXTENSIONS)]))
check("Cog 로드 후 DB 검증", events[-3:-1] == ["verify:attendance_data.db", "verify:party_data.db"])
```

기존 손상 DB 실패 테스트는 `Path.exists()`가 `True`를 반환하도록 패치해 “존재하는 DB 검증 실패” 계약을 유지한다.

- [ ] **Step 2: 잠금 경합 테스트 작성**

```python
def test_instance_lock_rejects_second_holder():
    import module.main as main

    lock_path = _TMP_DIR / ".bot.lock"
    first = main.acquire_instance_lock(lock_path)
    try:
        try:
            main.acquire_instance_lock(lock_path)
            rejected = False
        except RuntimeError:
            rejected = True
        check("두 번째 봇 인스턴스 거부", rejected)
    finally:
        first.close()
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 신규 설치에서 기존 사전 검증 호출, `acquire_instance_lock` 미정의.

- [ ] **Step 4: 존재하는 DB만 사전 검증**

```python
def _verify_databases(existing_only: bool = False) -> None:
    for filename, required_tables in DATABASES.items():
        path = DATA_DIR / filename
        if existing_only and not path.exists():
            continue
        verify_database(path, required_tables)
```

`setup_hook`은 Cog 전 `_verify_databases(existing_only=True)`, Cog 로드 후 `_verify_databases()`를 호출한다.

- [ ] **Step 5: 비차단 단일 인스턴스 잠금 구현**

```python
import fcntl
from pathlib import Path
from typing import BinaryIO

def acquire_instance_lock(path: Path) -> BinaryIO:
    lock = path.open("a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError(f"봇이 이미 실행 중입니다: {path}") from exc
    return lock
```

`main()`에서 DATA_DIR 생성 후 프로세스 수명 동안 파일 객체를 유지한다.

```python
with acquire_instance_lock(DATA_DIR / ".bot.lock"):
    MyBot().run(DISCORD_TOKEN)
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

- [ ] **Step 7: 커밋**

```bash
git add module/main.py test/console_tests.py
git commit -m "fix: guard startup databases and bot instance"
```

---

### Task 10: 명령어를 운영 길드에만 동기화

**Files:**
- Modify: `module/config.py:45-76`
- Modify: `module/main.py:3-43`
- Modify: `.env.example:1-14`
- Modify: `test/console_tests.py:120-165,193-288`
- Modify: `docs/operations.md`

**Interfaces:**
- Consumes: `DISCORD_GUILD_ID: int`
- Produces: `tree.copy_global_to(guild=discord.Object(...))`, `tree.sync(guild=...)`

- [ ] **Step 1: 설정 및 길드 sync 테스트 작성**

공개 env 계약에 `DISCORD_GUILD_ID`를 추가한다. 시작 테스트의 FakeTree를 다음처럼 바꾼다.

```python
class FakeTree:
    def copy_global_to(self, *, guild):
        events.append(f"copy:{guild.id}")

    async def sync(self, *, guild):
        events.append(f"sync:{guild.id}")
```

`main.DISCORD_GUILD_ID`를 `123`으로 패치하고 마지막 이벤트가 다음과 같은지 검사한다.

```python
["copy:123", "sync:123"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `DISCORD_GUILD_ID` 미정의 또는 `sync()`가 guild 없이 호출돼 실패.

- [ ] **Step 3: 필수 운영 길드 설정 추가**

`module/config.py`에 다음 값을 추가하고 양의 정수로 검증한다.

```python
DISCORD_GUILD_ID = _int_from_env("DISCORD_GUILD_ID", 0)
```

`.env.example`의 runtime 영역에 추가한다.

```dotenv
DISCORD_GUILD_ID=
```

- [ ] **Step 4: 길드 명령 동기화로 교체**

```python
guild = discord.Object(id=DISCORD_GUILD_ID)
self.tree.copy_global_to(guild=guild)
await self.tree.sync(guild=guild)
print(f"🔄 Command tree synced to guild {DISCORD_GUILD_ID}")
```

이 패턴은 discord.py 공식 FAQ의 guild sync 계약을 따른다. 기존 글로벌 명령과 길드 명령 이름이 같으면 해당 길드에서는 길드 명령이 사용된다.

- [ ] **Step 5: 운영 문서 갱신**

`docs/operations.md` 환경 설정 설명에 Discord 개발자 모드에서 서버 ID를 복사해 `DISCORD_GUILD_ID`에 넣는 절차를 추가한다. 명령 변경은 다음 시작 시 해당 길드에 즉시 동기화된다고 명시한다.

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

- [ ] **Step 7: 커밋**

```bash
git add module/config.py module/main.py .env.example test/console_tests.py docs/operations.md
git commit -m "fix: sync commands to the configured guild"
```

---

### Task 11: 운영 로그 유실과 미사용 코드 제거

**Files:**
- Modify: `module/forbiddenfilter_cog.py:1-8,103-108`
- Delete: `single/command_prefix.py`
- Delete: `test/hello_prefix.py`
- Delete: `test/makejson.py`
- Modify: `test/console_tests.py:174-190`

**Interfaces:**
- Produces: stdout에 `📥 금지어 N개 로드` 한 줄

- [ ] **Step 1: 금지어 로드 출력 테스트 작성**

```python
import module.forbiddenfilter_cog as forbiddenfilter_cog

words = _TMP_DIR / "forbidden-log.json"
words.write_text('["금지어"]', encoding="utf-8")
with patch.object(forbiddenfilter_cog, "DATA_FILE", words), \
     patch("module.forbiddenfilter_cog.print") as output:
    cog = forbiddenfilter_cog.ForbiddenFilterCog(bot=None)

output.assert_called_once_with("📥 금지어 1개 로드")
check("금지어 로드 로그가 stdout에 기록", output.call_count == 1)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: `print`가 호출되지 않아 실패.

- [ ] **Step 3: root logger 호출을 기존 stdout 방식으로 통일**

`logging` import를 제거하고 다음 한 줄로 바꾼다.

```python
print(f"📥 금지어 {len(self._banned)}개 로드")
```

- [ ] **Step 4: 참조가 없는 실험 파일 확인 후 삭제**

Run: `rg -n "command_prefix|hello_prefix|makejson" --glob '!docs/code-review-2026-07-29.md' .`

Expected: 세 파일을 import하거나 실행하는 운영·테스트 참조가 없음.

Delete: `single/command_prefix.py`, `test/hello_prefix.py`, `test/makejson.py`.

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests && .venv/bin/python -m unittest test.test_discord_commands`

Expected: 전체 통과.

- [ ] **Step 6: 커밋**

```bash
git add module/forbiddenfilter_cog.py test/console_tests.py
git rm single/command_prefix.py test/hello_prefix.py test/makejson.py
git commit -m "chore: remove dead scripts and restore startup log"
```

---

### Task 12: 실제 금지어 파일을 Git 작업 트리에서 분리

**Files:**
- Modify: `module/config.py:38-43`
- Modify: `.env.example:6-14`
- Modify: `README.md:15-34`
- Modify: `docs/operations.md`
- Modify: `compose.yaml:10-13`
- Modify: `test/console_tests.py:84-172`
- Preserve locally: `runtime/data/forbidden_words.json` (Git에 추가하지 않음)

**Interfaces:**
- Consumes: `DATA_DIR`
- Produces: `FORBIDDEN_WORDS_FILE = DATA_DIR / "forbidden_words.json"`

- [ ] **Step 1: runtime 경로 및 Git 비추적 계약 테스트 작성**

`module.*` import 전 테스트 환경 설정에 다음 행을 추가한다.

```python
os.environ["FORBIDDEN_WORDS_FILE"] = str(_TMP_DIR / "forbidden_words.json")
```

`test_config_paths()`와 `test_public_env_contract()`에 다음 검사를 추가한다.

```python
check(
    "금지어 파일은 DATA_DIR에 저장",
    config.FORBIDDEN_WORDS_FILE == config.DATA_DIR / "forbidden_words.json",
)
check(
    "runtime 금지어 파일은 Git ignore",
    subprocess.run(
        ["git", "check-ignore", "-q", "runtime/data/forbidden_words.json"],
        cwd=PROJECT_ROOT,
        check=False,
    ).returncode == 0,
)
check(
    "실제 금지어 파일은 Git 비추적",
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", "runtime/data/forbidden_words.json"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0,
)
check(
    "금지어 예시 파일은 Git 추적",
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", "settings/forbidden_words.example.json"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0,
)
```

`.env.example` 계약에는 정확한 공개 값을 검사한다.

```python
check(
    "공개 env 금지어 경로",
    example["FORBIDDEN_WORDS_FILE"] == "runtime/data/forbidden_words.json",
)
compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
check(
    "Compose는 settings 금지어 파일을 bind하지 않음",
    "settings/forbidden_words.json" not in compose,
)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 현재 기본 경로와 공개 env 값이 `settings/forbidden_words.json`이라 실패.

- [ ] **Step 3: 기본 경로를 DATA_DIR 아래로 이동**

`module/config.py`에서 `DATA_DIR`을 만든 뒤 금지어 경로를 계산한다.

```python
DATA_DIR = _path_from_env("DATA_DIR", "runtime/data")
BACKUP_DIR = _path_from_env("BACKUP_DIR", "runtime/backups")
FORBIDDEN_WORDS_FILE = _path_from_env(
    "FORBIDDEN_WORDS_FILE",
    str(DATA_DIR / "forbidden_words.json"),
)
```

`.env.example`은 다음 값을 사용한다.

```dotenv
FORBIDDEN_WORDS_FILE=runtime/data/forbidden_words.json
```

- [ ] **Step 4: 현재 실제 목록을 runtime 경로에 손실 없이 복사**

구현을 적용하는 기본 worktree에서 다음 순서로 기존 파일을 보존한다.

```bash
mkdir -p runtime/data
test -f settings/forbidden_words.json
test ! -e runtime/data/forbidden_words.json
cp settings/forbidden_words.json runtime/data/forbidden_words.json
cmp settings/forbidden_words.json runtime/data/forbidden_words.json
chmod 600 runtime/data/forbidden_words.json
```

`runtime/data/forbidden_words.json`이 이미 있으면 덮어쓰지 않고 두 파일을 `cmp`로 비교한 뒤 사용자가 선택할 때까지 둘 다 보존한다. `.env.runtime`의 다음 행도 로컬에서 교체하되 Git에는 추가하지 않는다.

```dotenv
FORBIDDEN_WORDS_FILE=runtime/data/forbidden_words.json
```

- [ ] **Step 5: 신규 설치 및 기존 설치 문서 갱신**

`README.md`의 초기화 명령을 다음처럼 바꾼다.

```bash
mkdir -p runtime/data runtime/backups runtime/logs
cp settings/forbidden_words.example.json runtime/data/forbidden_words.json
chmod 600 .env.secrets .env.runtime runtime/data/forbidden_words.json
```

`docs/operations.md`에는 기존 설치 마이그레이션을 정확히 기록한다.

```bash
mkdir -p runtime/data
cp settings/forbidden_words.json runtime/data/forbidden_words.json
chmod 600 runtime/data/forbidden_words.json
```

마이그레이션 후 `.env.runtime`의 `FORBIDDEN_WORDS_FILE`을 `runtime/data/forbidden_words.json`으로 변경하고 봇 재시작 전 `cmp`로 내용이 같은지 확인하도록 안내한다.

- [ ] **Step 6: Compose의 개별 settings 파일 bind 제거**

`runtime/data` 디렉터리가 이미 `/app/runtime/data`에 마운트되므로 다음 개별 bind 항목을 완전히 제거한다.

```yaml
- ./settings/forbidden_words.json:/app/settings/forbidden_words.json:ro
```

`FORBIDDEN_WORDS_FILE=runtime/data/forbidden_words.json`은 컨테이너 안에서 `/app/runtime/data/forbidden_words.json`으로 해석된다. 이로써 누락 파일을 Docker가 동명 디렉터리로 만드는 문제도 함께 제거된다.

- [ ] **Step 7: Compose와 Python 계약 테스트 통과 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

Run: `docker compose config --quiet`

Expected: exit 0.

Run: `git status --short --ignored runtime/data/forbidden_words.json`

Expected: `!! runtime/` 또는 `!! runtime/data/forbidden_words.json`; 파일 내용은 출력하지 않는다.

- [ ] **Step 8: source와 테스트만 커밋**

```bash
git add module/config.py .env.example README.md docs/operations.md compose.yaml test/console_tests.py
git commit -m "fix: keep moderation data outside git worktrees"
```

`runtime/data/forbidden_words.json`, `.env.runtime`, `settings/forbidden_words.json`은 커밋하지 않는다.

---

### Task 13: Docker와 launchd 배포 계약 강화

**Files:**
- Modify: `compose.yaml`
- Modify: `deploy/macos/com.discordbot.hsr-backup.plist.example`
- Create: `deploy/macos/com.discordbot.hsr.newsyslog.conf.example`
- Modify: `docs/operations.md`
- Modify: `test/console_tests.py:168-172`

**Interfaces:**
- Consumes: 하나의 `discordbot-hsr:local` 이미지와 `.env.runtime`의 `BACKUP_INTERVAL_SECONDS`
- Produces: 단일 애플리케이션 이미지, Docker/launchd 로그 상한, 단일 백업 주기 설정

- [ ] **Step 1: Compose 및 plist 계약 테스트 작성**

`test_compose_env_file_order()`를 `test_deployment_contracts()`로 확장한다.

```python
def test_deployment_contracts():
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    backup_plist = (
        PROJECT_ROOT / "deploy/macos/com.discordbot.hsr-backup.plist.example"
    ).read_text(encoding="utf-8")

    check("Compose 두 서비스 secrets 파일 우선",
          compose.count("    env_file:\n      - .env.runtime\n      - .env.secrets\n") == 2)
    check("Compose 이미지는 한 번만 빌드", compose.count("build: .") == 1)
    check("두 서비스가 같은 이미지 사용", compose.count("image: discordbot-hsr:local") == 2)
    check("settings 금지어 bind 제거", "settings/forbidden_words.json" not in compose)
    check("Compose 로그 크기 제한", compose.count('max-size: "10m"') == 2)
    check("Compose 로그 파일 수 제한", compose.count('max-file: "5"') == 2)
    check("launchd 백업은 env 주기 loop 사용",
          "<string>loop</string>" in backup_plist and "<key>StartInterval</key>" not in backup_plist)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m test.console_tests`

Expected: 이미지, settings 파일 bind 제거, 로그, launchd loop 계약이 실패.

- [ ] **Step 3: Compose에서 단일 이미지를 사용**

두 서비스에 같은 이미지 이름을 지정하고 `build`는 bot에만 둔다. `compose.yaml`은 다음 형태가 된다.

```yaml
services:
  bot:
    image: discordbot-hsr:local
    build: .
    env_file:
      - .env.runtime
      - .env.secrets
    command: ["python", "-m", "module.main"]
    restart: unless-stopped
    stop_grace_period: 30s
    volumes:
      - ./runtime/data:/app/runtime/data
      - ./runtime/backups:/app/runtime/backups
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

  backup:
    image: discordbot-hsr:local
    env_file:
      - .env.runtime
      - .env.secrets
    command: ["python", "-m", "module.backup", "loop"]
    restart: unless-stopped
    volumes:
      - ./runtime/data:/app/runtime/data:ro
      - ./runtime/backups:/app/runtime/backups
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

운영 절차는 항상 `docker compose build bot` 후 `docker compose up -d` 순서로 실행한다.

- [ ] **Step 4: 개별 금지어 bind가 다시 추가되지 않았는지 확인**

```yaml
volumes:
  - ./runtime/data:/app/runtime/data
  - ./runtime/backups:/app/runtime/backups
```

Task 12에서 실제 목록이 `runtime/data/forbidden_words.json`으로 이동했으므로 `settings/forbidden_words.json`을 참조하는 Compose 항목은 두지 않는다.

- [ ] **Step 5: 두 컨테이너 로그를 제한**

각 서비스에 다음 설정을 넣는다.

```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

- [ ] **Step 6: launchd 백업 주기를 `.env.runtime` 하나로 통일**

백업 plist의 command를 `create`에서 `loop`로 바꾸고 `StartInterval`을 제거한다.

```xml
<string>module.backup</string>
<string>loop</string>
<key>RunAtLoad</key>
<true/>
<key>KeepAlive</key>
<true/>
<key>ThrottleInterval</key>
<integer>10</integer>
```

이후 Docker와 launchd 모두 `BACKUP_INTERVAL_SECONDS`와 `BACKUP_RETENTION_DAYS`를 사용한다.

- [ ] **Step 7: macOS native 로그 로테이션 예시 추가**

`deploy/macos/com.discordbot.hsr.newsyslog.conf.example`을 다음 네 줄로 만든다. 경로는 기존 plist 예시와 동일하게 실제 저장소 절대 경로를 사용한다.

```text
/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/bot.log          640  7  10240  *  JC
/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/bot-error.log    640  7  10240  *  JC
/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/backup.log       640  7  10240  *  JC
/Users/strawberrydreams/coding/discordbot-hsr/runtime/logs/backup-error.log 640  7  10240  *  JC
```

`docs/operations.md`에 이 파일을 `/etc/newsyslog.d/com.discordbot.hsr.conf`로 복사하고 `sudo newsyslog -nvv`로 구문을 확인하는 설치 절차를 추가한다.

- [ ] **Step 8: 운영 명령 갱신**

`docs/operations.md`의 `docker compose build bot backup`을 모두 `docker compose build bot`으로 바꾼다. launchd 백업 설명에서 plist와 env를 동시에 수정하라는 문구를 제거하고 `.env.runtime` 변경 후 백업 LaunchAgent 재시작만 안내한다.

- [ ] **Step 9: 정적 및 런타임 검증**

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

Run: `docker compose config --quiet`

Expected: exit 0. Docker가 없는 개발 환경에서는 이 명령만 건너뛰고 CI 또는 배포 호스트에서 반드시 수행한다.

- [ ] **Step 10: 커밋**

```bash
git add compose.yaml deploy/macos/com.discordbot.hsr-backup.plist.example deploy/macos/com.discordbot.hsr.newsyslog.conf.example docs/operations.md test/console_tests.py
git commit -m "fix: harden local deployment contracts"
```

---

### Task 14: 전체 회귀 검증과 리뷰 추적표

**Files:**
- Modify: `docs/code-review-2026-07-29.md`

**Interfaces:**
- Consumes: Tasks 1-13의 구현과 테스트 결과
- Produces: 리뷰 항목별 해결/유지/제외 상태

- [ ] **Step 1: Python 문법과 전체 테스트 실행**

Run: `.venv/bin/python -m compileall -q module test`

Expected: exit 0.

Run: `.venv/bin/python -m unittest test.test_discord_commands`

Expected: 전체 통과.

Run: `.venv/bin/python -m test.console_tests`

Expected: 전체 통과.

- [ ] **Step 2: 배포 설정 검증**

Run: `docker compose config --quiet`

Expected: exit 0.

Run: `plutil -lint deploy/macos/com.discordbot.hsr.plist.example deploy/macos/com.discordbot.hsr-backup.plist.example`

Expected: 두 파일 모두 `OK`.

- [ ] **Step 3: 민감정보 및 diff 검사**

Run: `git diff --check`

Expected: 출력 없음.

Run: `git status --short`

Expected: 이 계획에 포함된 파일만 표시.

- [ ] **Step 4: 코드 리뷰 문서에 해결 상태 기록**

`docs/code-review-2026-07-29.md` 맨 아래에 다음 상태표를 추가한다.

```markdown
## 구현 상태

- 해결: 전역 멘션, 유령 파티, 이미지 이중 환불, 금지어 로드 로그, 이벤트 목록/정렬
- 해결: 금융 호출 블로킹, 출석 원자성, 대화 세션/만료 이미지 URL, 파티 UTC 시각
- 해결: 이중 인스턴스, 길드 sync, 신규 설치 DB, runtime 금지어 보존, 로그 로테이션
- 해결: 백업 주기 단일화, Compose 단일 이미지 빌드, 미사용 코드
- 검증: 모든 동작·배포 변경 커밋에 대응 회귀 테스트 포함
- 유지: 포인트 원자성, 시크릿 위생, 백업 구현
- 보류: SQLite WAL/timeout — 실제 `database is locked` 관측 시 적용
- 보류: 외부 장애 알림 — 알림 수신 서비스와 가용성 목표가 정해질 때 적용
- 제외: Mac 절전 가용성 — 코드가 아닌 호스트 선택 문제
- 조건부 제외: 과거 금지어 이력 정리 — 목록이 민감정보로 분류될 때만 수행
```

- [ ] **Step 5: 최종 커밋**

```bash
git add docs/code-review-2026-07-29.md
git commit -m "docs: record code review remediation status"
```

## Explicitly Deferred or Excluded

- **SQLite WAL/timeout:** 리뷰 자체가 현재 규모에서는 문제가 없고 lock 오류가 보일 때 조절하라고 명시한다. 관측 없는 연결 계층 재작성은 하지 않는다.
- **외부 장애 알림:** healthcheck만 추가해도 Compose가 자동으로 알리지 않으며, 알림 목적지와 가용성 목표가 없으면 실효성이 없다. 운영 모니터링 서비스를 정할 때 별도 계획으로 다룬다.
- **Mac 절전:** launchd와 Docker 모두 호스트 절전 중 멈추므로 애플리케이션 변경으로 해결할 수 없다.
- **금지어 Git 이력 정리:** 현재 목록의 민감도 판단이 선행되어야 하고 force-push가 필요한 파괴적 작업이므로 포함하지 않는다.
- **이미 안전한 포인트·백업 경로:** `deduct_points`, `play_luckybox`, 원자적 백업/검증/보존 로직은 변경하지 않는다.

## Self-Review

- **Spec coverage:** 코드 리뷰의 🔴/🟠 실제 결함과 즉시 수정 가능한 🟡 항목은 Tasks 1-13에 연결했다. Task 12는 추가 요구사항인 PR/브랜치 변경 중 실제 금지어 파일 보존을 처리한다. 녹색 항목은 변경하지 않으며 조건부·호스트·외부 서비스 항목은 위 제외 사유를 기록했다.
- **Test coverage:** Tasks 1-13은 모두 같은 Task 안에서 실패 테스트, 구현, 대상 테스트 통과, source+test 동시 커밋 순서를 갖는다. Task 14에서 두 전체 test runner와 배포 설정 검증을 다시 수행한다.
- **Task independence:** 각 Task는 자체 실패 테스트와 커밋 경계를 가지며, Task 14만 전체 통합 상태를 기록한다.
- **Type consistency:** `add_participant -> bool`, `claim_attendance -> Optional[int]`, party timestamp `int`, `/이벤트 index -> Optional[int]` 계약을 생산 Task와 소비 Task에서 동일하게 사용한다.
- **References:** guild sync는 discord.py 공식 FAQ의 `tree.copy_global_to(guild=...)`/`tree.sync(guild=...)`, image와 logging은 Docker Compose 공식 서비스 명세를 따른다.
