# Discord Command Privacy and Models Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discord 명령의 개인정보 보호와 파티 상호작용 안정성을 보완하고, 채널 모델 전환 방식을 GPT-5.6 기반의 독립 대화 명령 두 개로 교체한다.

**Architecture:** 기존 Cog 경계를 유지하고 `discord.Interaction` 응답 옵션만 최소 수정한다. 파티 View는 게임별 고정 `custom_id`를 가진 persistent View로 등록하고, 대화 Cog는 채널별 히스토리와 요청별 모델 선택을 분리한다.

**Tech Stack:** Python 3.12+, discord.py 2.7.1, openai 2.41.0 Responses API, google-genai 2.8.0, stdlib `unittest`

## Global Constraints

- `/출석`, `/지갑`, `/프로필` 성공 응답은 `ephemeral=True`여야 한다.
- `/모집`의 게임 선택 또는 생성 불가 메시지만 ephemeral이고, 게임 선택 후 실제 파티 모집 메시지는 공개여야 한다.
- `/이벤트`는 기존 `EVENT_CHANNEL_ID` 가드를 유지한다.
- `/파티`는 활성 파티가 여러 개여도 최초 응답을 정확히 한 번만 보낸다.
- 참가 버튼은 게임별 고정 `custom_id`와 `timeout=None` View를 사용하고 Cog 로드 시 등록한다.
- `/대화`, `/기본`, `/고급`은 제거한다.
- `/기본대화`는 `gpt-5.6-terra`, reasoning effort `none`, 비용 0P를 사용한다.
- `/고급대화`는 `gpt-5.6-sol`, reasoning effort `medium`, 비용 2,000P를 사용한다.
- 두 대화 명령은 같은 채널의 히스토리를 공유하며 대화 출력은 공개로 유지한다.
- `gemini-3.1-flash-image`는 이미 최신 Nano Banana 2 안정 모델이므로 관련 생산 코드와 변경 감지용 상수 테스트를 추가하지 않는다.
- 새 의존성과 요청 범위 밖 리팩터링을 추가하지 않는다.

---

### Task 1: 개인정보 응답 및 이벤트 채널 가드

**Files:**
- Create: `test/test_discord_commands.py`
- Modify: `module/attendance_cog.py:40-169`
- Modify: `module/playwith_cog.py:55-80`

**Interfaces:**
- Consumes: 기존 `AttendanceCog` 명령 callback과 `PlayWithCog.모집` callback
- Produces: 재사용 가능한 `RecordingResponse`, `FakeInteraction` 테스트 더블과 ephemeral 명령 동작

- [ ] **Step 1: 개인정보 응답 실패 테스트 작성**

`test/test_discord_commands.py`에 stdlib `unittest.IsolatedAsyncioTestCase`와 실제 callback 호출 테스트를 작성한다. `RecordingResponse.send_message`는 전달받은 `args`, `kwargs`를 저장한다. 테스트는 SQLite 임시 Repository와 최소 사용자 객체를 사용하고 다음을 검증한다.

```python
class RecordingResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    def is_done(self):
        return bool(self.messages)


class CommandPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_attendance_wallet_and_profile_successes_are_ephemeral(self):
        # AttendanceCog의 _attend, _wallet, _profile callback을 각각 실행한다.
        # 각 interaction.response.messages[-1][1]["ephemeral"] is True를 검증한다.

    async def test_recruit_selector_and_no_available_games_are_ephemeral(self):
        # RECRUIT_CHANNEL_ID를 patch하고 빈/가득 찬 PartyRepository로 모집 callback을 실행한다.
        # 두 응답 모두 kwargs["ephemeral"] is True를 검증한다.

    async def test_event_command_rejects_other_channels_before_fetching(self):
        # EVENT_CHANNEL_ID 밖 interaction으로 show_specific_event callback을 실행한다.
        # 응답이 ephemeral이고 guild.fetch_scheduled_events가 호출되지 않았음을 검증한다.
```

Test doubles는 프레임워크 메서드 호출 횟수 자체가 아니라 Cog가 외부에 내보내는 응답 flags와 채널 경계 동작을 기록해야 한다.

- [ ] **Step 2: 실패 확인**

Run:

```bash
.venv/bin/python -m unittest \
  test.test_discord_commands.CommandPrivacyTests.test_attendance_wallet_and_profile_successes_are_ephemeral \
  test.test_discord_commands.CommandPrivacyTests.test_recruit_selector_and_no_available_games_are_ephemeral
```

Expected: `/출석`, `/지갑`, `/프로필`, `/모집` 성공 응답에서 `ephemeral`이 없거나 `False`여서 FAIL. 이벤트 가드 테스트는 기존 코드에서 PASS해도 된다.

- [ ] **Step 3: 최소 구현**

`module/attendance_cog.py`의 세 성공 응답과 `module/playwith_cog.py`의 두 최초 모집 응답에만 `ephemeral=True`를 추가한다.

```python
await inter.response.send_message(embed=embed, ephemeral=True)
await interaction.response.send_message(embed=embed, ephemeral=True)
await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
```

`send_party_embed`와 참가 완료 메시지는 변경하지 않는다.

- [ ] **Step 4: 집중 및 전체 테스트**

Run:

```bash
.venv/bin/python -m unittest test.test_discord_commands.CommandPrivacyTests
.venv/bin/python -m test.console_tests
```

Expected: 두 명령 모두 exit 0, 새 테스트 전체 PASS, 기존 콘솔 테스트 실패 0.

- [ ] **Step 5: 커밋**

```bash
git add test/test_discord_commands.py module/attendance_cog.py module/playwith_cog.py
git commit -m "fix: make personal command prompts ephemeral"
```

---

### Task 2: 파티 응답 및 persistent View 안정화

**Files:**
- Modify: `test/test_discord_commands.py`
- Modify: `module/playwith_cog.py:10-16,105-161,185-228`

**Interfaces:**
- Consumes: Task 1의 `RecordingResponse`, `FakeInteraction`
- Produces: `PlayWithCog.shared_views: dict[str, View]`, 게임별 `party:join:<game>` 버튼 ID, 단일 `/파티` 응답

- [ ] **Step 1: 파티 안정성 실패 테스트 작성**

다음 테스트를 `test/test_discord_commands.py`에 추가한다.

```python
class PartyInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_party_status_sends_multiple_embeds_in_one_initial_response(self):
        # Repository에 참가자가 있는 활성 파티 두 개를 생성한다.
        # /파티 callback 실행 후 response.messages 길이가 1이고
        # kwargs["embeds"] 길이가 2인지 검증한다.

    def test_join_views_are_persistent_and_registered_at_cog_load(self):
        # cleanup_parties.start를 patch하고 기록용 bot.add_view를 사용해 Cog를 생성한다.
        # 모든 GAMES에 View가 하나씩 등록되었는지 검증한다.
        # 각 view.is_persistent()가 True인지 검증한다.
        # 각 JoinButton.custom_id가 f"party:join:{game}"인지 검증한다.
```

`RecordingResponse.send_message`는 두 번째 최초 응답 시 `RuntimeError`를 내도록 하여 Discord의 “한 interaction당 최초 응답 한 번” 계약도 재현한다.

- [ ] **Step 2: 실패 확인**

Run:

```bash
.venv/bin/python -m unittest test.test_discord_commands.PartyInteractionTests
```

Expected: 현재 `/파티`가 두 번째 `send_message`를 호출하거나 `embeds=` 단일 응답을 만들지 않아 FAIL. View가 등록되지 않고 버튼 `custom_id`가 고정되지 않아 FAIL.

- [ ] **Step 3: 단일 `/파티` 응답 구현**

활성 파티 embed를 모두 만든 뒤 한 번만 응답한다.

```python
if embeds:
    await interaction.response.send_message(embeds=embeds)
else:
    await interaction.response.send_message("📭 현재 모집 중인 파티가 없습니다.")
```

기존 `has_party` 변수와 embed별 응답 루프는 제거한다.

- [ ] **Step 4: persistent View 최소 구현**

Cog 생성 시 모든 게임의 persistent View를 한 번씩 만들고 등록한다.

```python
self.shared_views = {}
for game in GAMES:
    view = View(timeout=None)
    view.add_item(JoinButton(self, game))
    self.shared_views[game] = view
    bot.add_view(view)
```

버튼 생성자는 고정 ID를 사용한다.

```python
super().__init__(
    label="참가하기",
    style=discord.ButtonStyle.primary,
    custom_id=f"party:join:{game}",
)
```

`send_party_embed`는 이미 `shared_views[game]`을 재사용하므로 별도 메시지 ID 저장이나 DB 스키마 변경은 하지 않는다.

- [ ] **Step 5: 집중 및 전체 테스트**

Run:

```bash
.venv/bin/python -m unittest test.test_discord_commands.PartyInteractionTests
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
```

Expected: 모두 exit 0, 새 테스트 전체 PASS, 기존 콘솔 테스트 실패 0.

- [ ] **Step 6: 커밋**

```bash
git add test/test_discord_commands.py module/playwith_cog.py
git commit -m "fix: persist party interactions across restarts"
```

---

### Task 3: GPT-5.6 독립 대화 명령

**Files:**
- Modify: `test/test_discord_commands.py`
- Modify: `test/console_tests.py:1-12,495-523`
- Modify: `module/hyacine_chat_cog.py:12-29,154-270`

**Interfaces:**
- Consumes: 기존 `HyacineChatCog.get_session`, `trim`, Responses API 호출과 AttendanceCog 포인트 facade
- Produces: `_run_talk(inter, 내용, 이미지, model, reasoning_effort, cost)`, `/기본대화`, `/고급대화`

- [ ] **Step 1: 명령 라우팅 및 세션 실패 테스트 작성**

`test/test_discord_commands.py`에 실제 slash command callback을 실행하는 테스트를 추가한다.

```python
class ChatCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_chat_command_set_replaces_switching_commands(self):
        # cog.get_app_commands() 이름에 기본대화, 고급대화가 있고
        # 대화, 기본, 고급이 없는지 검증한다.

    async def test_basic_and_advanced_commands_route_exact_model_settings(self):
        # _run_talk을 기록 coroutine으로 교체한 뒤 두 callback을 실행한다.
        # 기본: ("gpt-5.6-terra", "none", 0)
        # 고급: ("gpt-5.6-sol", "medium", 2_000)

    async def test_status_lists_both_models_and_last_usage(self):
        # session.last_usage를 채우고 /상태 callback을 실행한다.
        # 두 명령/모델 매핑과 직전 사용 모델이 응답에 포함되는지 검증한다.
```

`test/console_tests.py::test_channel_sessions`에서는 모델 전환 상태 검사를 제거하고, 서로 다른 채널의 히스토리 격리 및 같은 채널 세션 재사용만 유지한다.

- [ ] **Step 2: 실패 확인**

Run:

```bash
.venv/bin/python -m unittest test.test_discord_commands.ChatCommandTests
```

Expected: 새 명령이 없고 기존 명령이 남아 있으며 모델 ID가 GPT-5.6이 아니므로 FAIL.

- [ ] **Step 3: 세션과 모델 상수 최소 변경**

```python
LIGHT_MODEL = "gpt-5.6-terra"
DEEP_MODEL = "gpt-5.6-sol"
```

`ChannelSession`에서 `model`, `reasoning_effort`를 제거하고 히스토리와 `last_usage`만 유지한다.

- [ ] **Step 4: 공통 대화 실행 함수로 기존 로직 이동**

기존 `_talk` 본문을 다음 서명의 비장식 메서드로 옮긴다.

```python
async def _run_talk(
    self,
    inter: discord.Interaction,
    내용: str,
    이미지: Optional[discord.Attachment],
    model: str,
    reasoning_effort: str,
    cost: int,
):
```

함수 내부의 모델·토큰 제한·API kwargs는 전달된 값만 사용한다.

```python
max_tokens = self.MAX_ASSISTANT_DEEP if model == DEEP_MODEL else self.MAX_ASSISTANT_LIGHT
kwargs = {
    "model": model,
    "instructions": self.system_prompt,
    "input": recent_turns + [{"role": "user", "content": parts}],
    "max_output_tokens": max_tokens,
    "reasoning": {"effort": reasoning_effort},
}
```

포인트 확인은 `cost > 0`일 때만 수행하고, 오류 로그에도 `model`을 사용한다. 공개 defer/followup과 실패 시 환불 동작은 유지한다.

- [ ] **Step 5: 두 독립 slash command와 상태 구현**

```python
@app_commands.command(name="기본대화", description="GPT-5.6 Terra와 빠르게 대화합니다.")
@app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
async def _light_talk(self, inter, 내용, 이미지=None):
    await self._run_talk(inter, 내용, 이미지, LIGHT_MODEL, "none", 0)

@app_commands.command(name="고급대화", description="GPT-5.6 Sol과 깊이 대화합니다. (2,000 P)")
@app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
async def _deep_talk(self, inter, 내용, 이미지=None):
    await self._run_talk(inter, 내용, 이미지, DEEP_MODEL, "medium", 2_000)
```

`/대화`, `/기본`, `/고급` decorator와 구현을 삭제한다. `/상태`는 선택 상태 대신 명령 매핑과 `session.last_usage`를 표시한다.

- [ ] **Step 6: 집중 및 전체 테스트**

Run:

```bash
.venv/bin/python -m unittest test.test_discord_commands.ChatCommandTests
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
```

Expected: 모두 exit 0, 새 테스트 전체 PASS, 기존 콘솔 테스트 실패 0.

- [ ] **Step 7: 커밋**

```bash
git add test/test_discord_commands.py test/console_tests.py module/hyacine_chat_cog.py
git commit -m "feat: split GPT-5.6 chat commands"
```

---

## Final Verification

모든 작업 리뷰가 끝난 뒤 다음을 새로 실행한다.

```bash
.venv/bin/python -m unittest test.test_discord_commands
.venv/bin/python -m test.console_tests
.venv/bin/python -m compileall -q module test
git diff --check
```

모든 명령이 exit 0이어야 완료로 판정한다.
