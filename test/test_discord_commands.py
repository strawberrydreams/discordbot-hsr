import asyncio
import gc
import json
import pathlib
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import discord
import httpx
import openai

import module.config as config
from module.attendance_cog import AttendanceCog
from module.database import SQLiteAttendanceRepository, SQLitePartyRepository
from module.eventnotice_cog import EventNoticeCog
from module.finance_cog import FinanceCog
from module.hyacine_chat_cog import HyacineChatCog
from module.hyacine_image_cog import HyacineImageCog
from module.playwith_cog import PlayWithCog
import module.playwith_cog as playwith_cog
import module.forbiddenfilter_cog as forbiddenfilter_cog
import module.backup as backup


class RecordingResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, *args, **kwargs):
        if self.messages:
            raise RuntimeError("interaction already has an initial response")
        self.messages.append((args, kwargs))

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def defer(self):
        self.deferred = True


class RecordingFollowup:
    def __init__(self, fail_on_call=None):
        self.messages = []
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def send(self, *args, **kwargs):
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("followup transport failed")
        self.messages.append((args, kwargs))


class FakeUser:
    id = 123
    mention = "<@123>"
    display_name = "테스트 유저"
    joined_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    color = discord.Color.default()
    avatar = None


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


class FakeGuild:
    def __init__(self, events=None):
        self.fetch_scheduled_events_calls = 0
        self.events = events or []

    async def fetch_scheduled_events(self):
        self.fetch_scheduled_events_calls += 1
        return self.events

    def get_member(self, user_id):
        return FakeUser()


class FakeInteraction:
    def __init__(self, channel_id, guild=None, guild_id=None):
        self.channel_id = channel_id
        self.user = FakeUser()
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.guild = guild
        self.created_at = datetime.now(timezone.utc)
        # 기본값은 설정된 길드 — 경계 테스트만 다른 값을 넘긴다.
        self.guild_id = config.DISCORD_GUILD_ID if guild_id is None else guild_id


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
            return function(symbol)

        with patch.object(
            FinanceCog,
            "get_stock_data",
            return_value={"price": 1.0, "change": 0.0, "change_percent": 0.0},
        ) as get_stock_data, patch(
            "module.finance_cog.asyncio.to_thread", side_effect=controlled_to_thread
        ):
            await asyncio.wait_for(
                FinanceCog.stock_price.callback(cog, interaction), timeout=1
            )

        self.assertEqual(started, len(cog.tickers))
        self.assertEqual(get_stock_data.call_count, len(cog.tickers))
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.followup.messages), 1)
        self.assertEqual(len(interaction.followup.messages[0][1]["embed"].fields), len(cog.tickers))


class FinanceTimeoutAndCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_ticker_does_not_block_the_other_results(self):
        cog = FinanceCog(bot=None)
        slow_symbol = list(cog.tickers.values())[0]

        def fetch(symbol):
            return {"price": 1.0, "change": 0.0, "change_percent": 0.0}

        async def maybe_hang(function, symbol):
            if symbol == slow_symbol:
                await asyncio.sleep(10)
            return function(symbol)

        interaction = FakeInteraction(channel_id=1)
        with patch.object(FinanceCog, "get_stock_data", side_effect=fetch), patch(
            "module.finance_cog.FETCH_TIMEOUT_SECONDS", 0.05
        ), patch("module.finance_cog.asyncio.to_thread", side_effect=maybe_hang):
            await asyncio.wait_for(
                FinanceCog.stock_price.callback(cog, interaction), timeout=2
            )

        fields = interaction.followup.messages[0][1]["embed"].fields
        failed = [field for field in fields if field.value == "데이터 조회 실패"]
        self.assertEqual(len(fields), len(cog.tickers))
        self.assertEqual(len(failed), 1)

    async def test_second_call_within_ttl_uses_cache(self):
        cog = FinanceCog(bot=None)
        calls = []

        def fetch(symbol):
            calls.append(symbol)
            return {"price": 1.0, "change": 0.0, "change_percent": 0.0}

        with patch.object(FinanceCog, "get_stock_data", side_effect=fetch):
            await FinanceCog.stock_price.callback(cog, FakeInteraction(channel_id=1))
            self.assertEqual(len(calls), len(cog.tickers))
            await FinanceCog.stock_price.callback(cog, FakeInteraction(channel_id=1))
            self.assertEqual(len(calls), len(cog.tickers))

    async def test_expired_cache_is_served_when_the_fetch_times_out(self):
        cog = FinanceCog(bot=None)
        symbol = list(cog.tickers.values())[0]
        cog._cache[symbol] = (0.0, {"price": 9.0, "change": 0.0, "change_percent": 0.0})

        async def always_hang(function, argument):
            await asyncio.sleep(10)

        with patch("module.finance_cog.CACHE_TTL_SECONDS", 0.0), patch(
            "module.finance_cog.FETCH_TIMEOUT_SECONDS", 0.05
        ), patch("module.finance_cog.asyncio.to_thread", side_effect=always_hang):
            data = await cog._fetch(symbol)

        self.assertEqual(data["price"], 9.0)


class RecordingAttendance:
    def __init__(self, refund_error=None):
        self.deductions = []
        self.refunds = []
        self.refund_attempts = []
        self.reasons = []
        self.refund_error = refund_error

    def deduct_points(self, user_id, amount, reason="unspecified"):
        self.deductions.append((user_id, amount))
        self.reasons.append(reason)
        return True

    def add_points(self, user_id, amount, reason="unspecified"):
        self.reasons.append(reason)
        self.refund_attempts.append((user_id, amount))
        if self.refund_error:
            raise self.refund_error
        self.refunds.append((user_id, amount))


class ImageCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_defer_and_refund_failures_request_manual_reconciliation_once(self):
        attendance = RecordingAttendance(
            refund_error=RuntimeError("database unavailable")
        )
        interaction = FakeInteraction(channel_id=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))

        async def fail_defer():
            raise RuntimeError("defer transport failed")

        interaction.response.defer = fail_defer
        escaped = None
        with patch("module.hyacine_image_cog.print"), patch(
            "module.hyacine_image_cog.traceback.print_exc"
        ):
            try:
                await HyacineImageCog._image.callback(cog, interaction, "test")
            except Exception as exc:
                escaped = exc

        self.assertIsNone(escaped)
        self.assertEqual(attendance.refund_attempts, [(123, 30_000)])
        messages = interaction.response.messages + interaction.followup.messages
        self.assertTrue(messages)
        message = messages[-1][0][0]
        self.assertIn("자동 환불에 실패", message)
        self.assertIn("관리자", message)
        self.assertIn("수동 정산", message)

    async def test_generation_exception_refunds_once_when_error_message_fails(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        interaction.followup = RecordingFollowup(fail_on_call=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))

        def fail_generation(**_):
            raise RuntimeError("provider failed")

        cog.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=fail_generation)
        )

        with patch("module.hyacine_image_cog.print"), patch(
            "module.hyacine_image_cog.traceback.print_exc"
        ):
            await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(attendance.refunds, [(123, 30_000)])

    async def test_empty_image_response_refunds_only_once_when_error_message_fails(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        interaction.followup = RecordingFollowup(fail_on_call=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cog.temp_dir = temp_dir.name
        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_: SimpleNamespace(parts=[])
            )
        )

        with patch("module.hyacine_image_cog.print"), patch(
            "module.hyacine_image_cog.traceback.print_exc"
        ):
            await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(attendance.refunds, [(123, 30_000)])

    async def test_generated_image_is_not_refunded_when_discord_upload_fails(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        interaction.followup = RecordingFollowup(fail_on_call=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cog.temp_dir = temp_dir.name
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))
        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_: SimpleNamespace(parts=[part])
            )
        )

        with patch("module.hyacine_image_cog.print"), patch(
            "module.hyacine_image_cog.traceback.print_exc"
        ):
            await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(attendance.refunds, [])
        self.assertIn(
            "이미지는 생성되었지만 Discord 전송에 실패했습니다.",
            interaction.followup.messages[-1][0][0],
        )
        self.assertNotIn("환불", interaction.followup.messages[-1][0][0])

    async def test_long_prompt_is_truncated_in_embed(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cog.temp_dir = temp_dir.name
        cog.bot = SimpleNamespace(
            get_cog=lambda _: attendance,
            loop=SimpleNamespace(create_task=lambda coro: coro.close()),
        )
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))
        captured = {}

        def generate(**kwargs):
            captured["contents"] = kwargs["contents"]
            return SimpleNamespace(parts=[part])

        cog.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        prompt = "가" * 5_000

        await HyacineImageCog._image.callback(cog, interaction, prompt)

        description = interaction.followup.messages[0][1]["embed"].description
        self.assertLessEqual(len(description), 1_100)
        self.assertTrue(description.endswith("…"))
        # 모델에는 원본이 그대로 간다.
        self.assertEqual(captured["contents"], [prompt])

    async def test_failed_discord_upload_deletes_temporary_image_immediately(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        interaction.followup = RecordingFollowup(fail_on_call=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cog.temp_dir = temp_dir.name
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))
        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_: SimpleNamespace(parts=[part])
            )
        )

        with patch("module.hyacine_image_cog.print"), patch(
            "module.hyacine_image_cog.traceback.print_exc"
        ):
            await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(list(pathlib.Path(temp_dir.name).iterdir()), [])


class DisappearingAttendanceBot:
    def __init__(self, attendance):
        self.attendance = attendance
        self.calls = 0

    def get_cog(self, name):
        self.calls += 1
        return self.attendance if self.calls == 1 else None


class ChatCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.hyacine_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)
        self.cog = HyacineChatCog(bot=None)

    def test_chat_command_set_replaces_switching_commands(self):
        names = {command.name for command in self.cog.get_app_commands()}

        self.assertTrue({"기본대화", "고급대화"}.issubset(names))
        self.assertFalse({"대화", "기본", "고급"} & names)

    async def test_basic_and_advanced_commands_route_exact_model_settings(self):
        calls = []

        async def record_run_talk(*args):
            calls.append(args)

        self.cog._run_talk = record_run_talk
        basic = FakeInteraction(channel_id=1)
        advanced = FakeInteraction(channel_id=1)

        await HyacineChatCog._light_talk.callback(self.cog, basic, "기본 대화")
        await HyacineChatCog._deep_talk.callback(self.cog, advanced, "고급 대화")

        self.assertEqual(
            calls,
            [
                (basic, "기본 대화", None, "gpt-5.6-terra", "none", 200),
                (advanced, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000),
            ],
        )

    async def test_status_lists_both_models_and_last_usage(self):
        interaction = FakeInteraction(channel_id=1)
        self.cog.get_session(1).last_usage = {
            "model": "gpt-5.6-sol",
            "input_tokens": 12,
            "output_tokens": 34,
            "total_tokens": 46,
        }

        await HyacineChatCog._status.callback(self.cog, interaction)

        message = interaction.response.messages[-1][0][0]
        for text in ("/기본대화", "gpt-5.6-terra", "/고급대화", "gpt-5.6-sol", "직전 사용 모델", "46"):
            self.assertIn(text, message)

    async def test_image_url_is_sent_once_but_not_saved_in_history(self):
        interaction = FakeInteraction(channel_id=1)
        attachment = SimpleNamespace(
            content_type="image/png", url="https://cdn.example/signed.png"
        )
        captured = {}

        async def response(**kwargs):
            captured["input"] = kwargs["input"]
            return SimpleNamespace(
                output_text="확인했습니다.",
                model="gpt-5.6-terra",
                usage=SimpleNamespace(
                    input_tokens=1, output_tokens=1, total_tokens=2
                ),
            )

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=response)
        )
        await self.cog._run_talk(
            interaction, "", attachment, "gpt-5.6-terra", "none", 0
        )

        self.assertIn(attachment.url, repr(captured["input"]))
        self.assertNotIn(
            attachment.url, repr(list(self.cog.get_session(1).history))
        )

    async def test_pre_api_exception_refunds_with_the_original_attendance_cog(self):
        attendance = RecordingAttendance()
        bot = DisappearingAttendanceBot(attendance)
        interaction = FakeInteraction(channel_id=1)
        self.cog.bot = bot

        def fail_before_api(*args):
            raise RuntimeError("attachment processing failed")

        self.cog.build_user_parts = fail_before_api

        with patch("module.hyacine_chat_cog.print"), patch(
            "module.hyacine_chat_cog.traceback.print_exc"
        ):
            await self.cog._run_talk(
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000
            )

        self.assertEqual(attendance.deductions, [(123, 2_000)])
        self.assertEqual(attendance.refunds, [(123, 2_000)])
        self.assertEqual(bot.calls, 1)
        self.assertIn("포인트 환불됨", interaction.followup.messages[-1][0][0])

    async def test_empty_response_refunds_charged_points(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        self.cog.bot = DisappearingAttendanceBot(attendance)

        async def empty_response(**kwargs):
            return SimpleNamespace(output_text="")

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=empty_response)
        )

        with patch("module.hyacine_chat_cog.traceback.print_exc"):
            await self.cog._run_talk(
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000
            )

        self.assertEqual(attendance.refunds, [(123, 2_000)])
        self.assertIn("포인트 환불됨", interaction.followup.messages[-1][0][0])

    async def test_refund_note_is_omitted_when_refund_fails(self):
        attendance = RecordingAttendance(refund_error=RuntimeError("database unavailable"))
        interaction = FakeInteraction(channel_id=1)
        self.cog.bot = DisappearingAttendanceBot(attendance)

        async def empty_response(**kwargs):
            return SimpleNamespace(output_text="")

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=empty_response)
        )

        with patch("module.hyacine_chat_cog.print"), patch(
            "module.hyacine_chat_cog.traceback.print_exc"
        ):
            await self.cog._run_talk(
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000
            )

        self.assertNotIn("포인트 환불됨", interaction.followup.messages[-1][0][0])

    async def test_failed_defer_refunds_and_uses_initial_response(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        self.cog.bot = DisappearingAttendanceBot(attendance)

        async def fail_defer():
            raise RuntimeError("defer transport failed")

        interaction.response.defer = fail_defer

        with patch("module.hyacine_chat_cog.print"), patch(
            "module.hyacine_chat_cog.traceback.print_exc"
        ):
            await self.cog._run_talk(
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000
            )

        self.assertEqual(attendance.refunds, [(123, 2_000)])
        self.assertEqual(interaction.followup.messages, [])
        self.assertIn("포인트 환불됨", interaction.response.messages[-1][0][0])


class ChatConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.hyacine_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)

    async def test_same_channel_calls_do_not_interleave_history(self):
        cog = HyacineChatCog(bot=None)
        # 첫 호출의 응답이 두 번째보다 늦게 끝나도록 지연시킨다.
        delays = {"first": 0.05, "second": 0.0}

        async def delayed(**kwargs):
            text = kwargs["input"][-1]["content"][0]["text"]
            await asyncio.sleep(delays[text])
            return SimpleNamespace(
                output_text=f"{text} 응답",
                model="gpt-5.6-terra",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
            )

        cog.client = SimpleNamespace(responses=SimpleNamespace(create=delayed))

        await asyncio.gather(
            cog._run_talk(FakeInteraction(channel_id=1), "first", None, "gpt-5.6-terra", "none", 0),
            cog._run_talk(FakeInteraction(channel_id=1), "second", None, "gpt-5.6-terra", "none", 0),
        )

        roles = [m["role"] for m in cog.get_session(1).history if m["role"] != "system"]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    async def test_active_session_survives_eviction(self):
        cog = HyacineChatCog(bot=None)
        cog.MAX_CHANNEL_SESSIONS = 2
        active = cog.get_session(1)
        async with active.lock:
            for channel_id in range(2, 12):
                cog.get_session(channel_id)
            self.assertIs(cog.get_session(1), active)


class AICooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.hyacine_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)

    async def test_every_ai_command_carries_a_cooldown(self):
        chat = HyacineChatCog(bot=None)
        image = HyacineImageCog(bot=None)
        commands = {
            "기본대화": HyacineChatCog._light_talk,
            "고급대화": HyacineChatCog._deep_talk,
            "이미지": HyacineImageCog._image,
        }
        for name, command in commands.items():
            with self.subTest(command=name):
                self.assertTrue(command.checks, "쿨다운 검사가 등록되어 있지 않다")
                predicate = command.checks[0]
                interaction = FakeInteraction(channel_id=1)
                self.assertTrue(await predicate(interaction))
                with self.assertRaises(
                    discord.app_commands.CommandOnCooldown
                ) as raised:
                    await predicate(FakeInteraction(channel_id=1))
                self.assertLessEqual(
                    raised.exception.retry_after, config.AI_COOLDOWN_SECONDS
                )
        self.assertTrue(hasattr(chat, "cog_app_command_error"))
        self.assertTrue(hasattr(image, "cog_app_command_error"))

    async def test_cooldown_notice_is_ephemeral_and_charges_nothing(self):
        attendance = RecordingAttendance()
        cog = HyacineChatCog(bot=DisappearingAttendanceBot(attendance))
        interaction = FakeInteraction(channel_id=1)
        error = discord.app_commands.CommandOnCooldown(
            discord.app_commands.Cooldown(1, 15), 7.4
        )

        await cog.cog_app_command_error(interaction, error)

        args, kwargs = interaction.response.messages[-1]
        self.assertIs(kwargs.get("ephemeral"), True)
        self.assertIn("7초", args[0])
        self.assertEqual(attendance.deductions, [])

    async def test_rate_limit_error_gets_its_own_notice_and_refund(self):
        attendance = RecordingAttendance()
        cog = HyacineChatCog(bot=DisappearingAttendanceBot(attendance))
        interaction = FakeInteraction(channel_id=1)

        async def rate_limited(**kwargs):
            raise openai.RateLimitError(
                "rate limited",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://api.openai.com")
                ),
                body=None,
            )

        cog.client = SimpleNamespace(responses=SimpleNamespace(create=rate_limited))

        with patch("module.hyacine_chat_cog.print"), patch(
            "module.hyacine_chat_cog.traceback.print_exc"
        ):
            await cog._run_talk(
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000
            )

        message = interaction.followup.messages[-1][0][0]
        self.assertIn("요청이 몰려", message)
        self.assertIn("포인트 환불됨", message)
        self.assertEqual(attendance.refunds, [(123, 2_000)])


class CommandPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = pathlib.Path(self.temp_dir.name)
        self.attendance = AttendanceCog(
            bot=None,
            repository=SQLiteAttendanceRepository(root / "attendance.db"),
        )
        self.party_repository = SQLitePartyRepository(root / "party.db")
        with patch("discord.ext.tasks.Loop.start"):
            self.play = PlayWithCog(bot=None, repository=self.party_repository)

    async def test_attendance_wallet_and_profile_successes_are_ephemeral(self):
        for command in (
            AttendanceCog._attend,
            AttendanceCog._wallet,
            AttendanceCog._profile,
        ):
            interaction = FakeInteraction(channel_id=1)
            await command.callback(self.attendance, interaction)
            self.assertIs(interaction.response.messages[-1][1].get("ephemeral"), True)

    async def test_attendance_balance_matches_db_and_duplicate_only_responds_duplicate(self):
        self.attendance.db.add_points(FakeUser.id, 2_000)

        success = FakeInteraction(channel_id=1)
        with patch("module.attendance_cog.random.randint", return_value=7_000):
            await AttendanceCog._attend.callback(self.attendance, success)

        success_args, success_kwargs = success.response.messages[0]
        self.assertEqual(success_args, ())
        self.assertEqual(self.attendance.db.get_points(FakeUser.id), 9_000)
        self.assertEqual(success_kwargs["embed"].fields[0].value, "9,000 P")

        duplicate = FakeInteraction(channel_id=1)
        with patch("module.attendance_cog.random.randint", return_value=30_000):
            await AttendanceCog._attend.callback(self.attendance, duplicate)

        duplicate_args, duplicate_kwargs = duplicate.response.messages[0]
        self.assertEqual(len(duplicate.response.messages), 1)
        self.assertEqual(len(duplicate_args), 1)
        self.assertIn("이미 출석", duplicate_args[0])
        self.assertNotIn("embed", duplicate_kwargs)
        self.assertIs(duplicate_kwargs.get("ephemeral"), True)
        self.assertEqual(self.attendance.db.get_points(FakeUser.id), 9_000)

    async def test_recruit_selector_and_no_available_games_are_ephemeral(self):
        with patch.object(playwith_cog, "RECRUIT_CHANNEL_ID", 1):
            selector_interaction = FakeInteraction(channel_id=1)
            await PlayWithCog.모집.callback(self.play, selector_interaction)
            self.assertIs(
                selector_interaction.response.messages[-1][1].get("ephemeral"), True
            )

            for game in playwith_cog.GAMES:
                self.party_repository.create_party(game, datetime.now().isoformat())
            full_interaction = FakeInteraction(channel_id=1)
            await PlayWithCog.모집.callback(self.play, full_interaction)
            self.assertIs(full_interaction.response.messages[-1][1].get("ephemeral"), True)

    async def test_event_command_rejects_other_channels_before_fetching(self):
        guild = FakeGuild()
        interaction = FakeInteraction(channel_id=-1, guild=guild)
        with patch("module.eventnotice_cog.EVENT_CHANNEL_ID", 1):
            await EventNoticeCog.show_specific_event.callback(
                EventNoticeCog(bot=None), interaction, 1
            )

        self.assertIs(interaction.response.messages[-1][1].get("ephemeral"), True)
        self.assertEqual(guild.fetch_scheduled_events_calls, 0)

    async def test_event_list_and_detail_use_start_time_then_id_order(self):
        later = datetime(2026, 8, 2, tzinfo=timezone.utc)
        earlier = datetime(2026, 8, 1, tzinfo=timezone.utc)
        guild = FakeGuild(
            [
                fake_event(2, "두 번째", later),
                fake_event(3, "같은 시각 뒤", earlier),
                fake_event(1, "같은 시각 앞", earlier),
            ]
        )
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


class PartyInteractionTests(unittest.IsolatedAsyncioTestCase):
    def test_party_repository_closes_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
                repository.create_party("PUBG", 1_000)
                repository.add_participant("PUBG", 123)
                repository.delete_party("PUBG")
                del repository
                gc.collect()

        self.assertFalse(
            [warning for warning in caught if issubclass(warning.category, ResourceWarning)]
        )

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
            self.assertEqual(
                interaction.response.messages[0][1]["embeds"][0].description,
                f"현재 인원: 0 / {playwith_cog.GAMES[game]['max_players']}",
            )

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

    async def test_stale_role_update_rejects_deleted_party(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(game, 1_000)
            repository.add_participant(game, FakeUser.id, "탑")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)
            select = playwith_cog.RoleUpdateSelect(cog, game, FakeUser.id)
            select._values = ["정글"]
            repository.delete_party(game)
            interaction = FakeInteraction(channel_id=1)

            await select.callback(interaction)

            self.assertIn("모집이 종료된 파티", interaction.response.messages[0][0][0])
            self.assertIsNone(repository.get_user_party(interaction.user.id))

    async def test_party_status_sends_multiple_embeds_in_one_initial_response(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            games = list(playwith_cog.GAMES)[:2]
            for user_id, game in enumerate(games, start=1):
                repository.create_party(game, datetime.now().isoformat())
                repository.add_participant(game, user_id)

            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)
            interaction = FakeInteraction(channel_id=1, guild=FakeGuild())

            with patch.object(playwith_cog, "RECRUIT_CHANNEL_ID", 1):
                await PlayWithCog.파티.callback(cog, interaction)

        self.assertEqual(len(interaction.response.messages), 1)
        self.assertEqual(len(interaction.response.messages[0][1]["embeds"]), 2)

    def test_join_views_are_persistent_and_registered_at_cog_load(self):
        class Bot:
            def __init__(self):
                self.views = []

            def add_view(self, view):
                self.views.append(view)

        bot = Bot()
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=bot, repository=repository)

        self.assertEqual(len(bot.views), len(playwith_cog.GAMES))
        for game, view in cog.shared_views.items():
            self.assertTrue(view.is_persistent())
            self.assertEqual(view.children[0].custom_id, f"party:join:{game}")


class FakeMessage:
    def __init__(self, content, guild_id=None):
        self.content = content
        self.author = SimpleNamespace(bot=False, id=FakeUser.id, mention=FakeUser.mention)
        self.guild = (
            None
            if guild_id is None
            else SimpleNamespace(id=guild_id)
        )
        self.channel = SimpleNamespace(send=self._send)
        self.sent = []

    async def _send(self, text):
        self.sent.append(text)


class RecordingForbiddenCounts:
    def __init__(self):
        self.counts = []

    def increment_forbidden_count(self, user_id):
        self.counts.append(user_id)


def make_forbidden_cog(counter, words=("나쁜말",)):
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(list(words), handle)
        path = pathlib.Path(handle.name)
    with patch.object(forbiddenfilter_cog, "DATA_FILE", path), patch(
        "module.forbiddenfilter_cog.print"
    ):
        cog = forbiddenfilter_cog.ForbiddenFilterCog(
            SimpleNamespace(get_cog=lambda _: counter)
        )
    path.unlink()
    return cog


class GuildBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_guild_and_dm_messages_are_ignored(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)

        other_guild = FakeMessage("나쁜말", guild_id=config.DISCORD_GUILD_ID + 1)
        direct_message = FakeMessage("나쁜말", guild_id=None)
        for message in (other_guild, direct_message):
            await cog.on_message(message)

        self.assertEqual(counter.counts, [])
        self.assertEqual(other_guild.sent + direct_message.sent, [])

    async def test_configured_guild_message_is_still_screened(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)
        message = FakeMessage("나쁜말", guild_id=config.DISCORD_GUILD_ID)

        await cog.on_message(message)

        self.assertEqual(counter.counts, [FakeUser.id])
        self.assertIn("나쁜말", message.sent[0])

    async def test_join_button_from_another_guild_does_not_join(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(game, 1_000)
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)
            interaction = FakeInteraction(
                channel_id=1, guild_id=config.DISCORD_GUILD_ID + 1
            )

            await cog.shared_views[game].children[0].callback(interaction)

            self.assertIsNone(repository.get_user_party(FakeUser.id))
            self.assertIs(interaction.response.messages[0][1].get("ephemeral"), True)
            self.assertIn("이 서버에서 사용할 수 없습니다", interaction.response.messages[0][0][0])

    async def test_command_tree_rejects_other_guilds(self):
        import module.main as main

        class FakeTree:
            interaction_check = None

            def copy_global_to(self, *, guild):
                pass

            def clear_commands(self, *, guild):
                pass

            async def sync(self, *, guild=None):
                pass

        class FakeBot:
            tree = FakeTree()

            async def load_extension(self, extension):
                pass

        with patch.object(main, "_verify_databases"), patch.object(
            main, "DISCORD_GUILD_ID", 4_242
        ), patch.object(main, "DATA_DIR", pathlib.Path(tempfile.mkdtemp())), patch.object(
            main, "GLOBAL_CLEANUP_MARKER", pathlib.Path(tempfile.mkdtemp()) / "marker"
        ):
            bot = FakeBot()
            await main.MyBot.setup_hook(bot)
            check = bot.tree.interaction_check

            self.assertTrue(await check(FakeInteraction(channel_id=1, guild_id=4_242)))
            self.assertFalse(await check(FakeInteraction(channel_id=1, guild_id=1)))
            # DM 상호작용은 guild_id가 없다.
            self.assertFalse(await check(SimpleNamespace(guild_id=None)))


class RankingCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        repository = SQLiteAttendanceRepository(
            pathlib.Path(self.temp_dir.name) / "attendance.db"
        )
        repository.add_points(1, 500)
        repository.add_points(2, 400)
        self.cog = AttendanceCog(bot=None, repository=repository)

    async def test_guild_nickname_wins_and_cache_miss_hides_the_raw_id(self):
        class PartialGuild:
            def get_member(self, user_id):
                return (
                    SimpleNamespace(display_name="서버 닉네임")
                    if user_id == 1
                    else None
                )

        interaction = FakeInteraction(channel_id=1, guild=PartialGuild())

        await AttendanceCog._ranking.callback(self.cog, interaction)

        names = [
            field.name for field in interaction.response.messages[0][1]["embed"].fields
        ]
        self.assertIn("서버 닉네임", names[0])
        self.assertIn("알 수 없는 유저", names[1])
        self.assertNotIn("2", names[1].replace("2️⃣", ""))


class ForbiddenEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counter = RecordingForbiddenCounts()
        self.cog = make_forbidden_cog(self.counter)

    async def test_edit_that_introduces_a_forbidden_word_is_caught(self):
        before = FakeMessage("안녕하세요", guild_id=config.DISCORD_GUILD_ID)
        after = FakeMessage("나쁜말", guild_id=config.DISCORD_GUILD_ID)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(self.counter.counts, [FakeUser.id])
        self.assertIn("나쁜말", after.sent[0])

    async def test_edit_of_an_already_caught_message_is_not_double_counted(self):
        before = FakeMessage("나쁜말 하나", guild_id=config.DISCORD_GUILD_ID)
        after = FakeMessage("나쁜말 둘", guild_id=config.DISCORD_GUILD_ID)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(self.counter.counts, [])
        self.assertEqual(after.sent, [])

    async def test_unchanged_content_is_not_rescreened(self):
        message = FakeMessage("나쁜말", guild_id=config.DISCORD_GUILD_ID)

        await self.cog.on_message_edit(message, message)

        self.assertEqual(self.counter.counts, [])

    async def test_clean_edit_stays_clean(self):
        before = FakeMessage("안녕", guild_id=config.DISCORD_GUILD_ID)
        after = FakeMessage("반가워요", guild_id=config.DISCORD_GUILD_ID)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(self.counter.counts, [])
        self.assertEqual(after.sent, [])


class PartyCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_selection_of_same_game_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            first = FakeInteraction(channel_id=1)
            second = FakeInteraction(channel_id=1)
            select = playwith_cog.GameSelect(cog, [game])
            select._values = [game]

            await select.callback(first)
            await select.callback(second)

            self.assertIn("embed", first.response.messages[0][1])
            args, kwargs = second.response.messages[0]
            self.assertNotIn("embed", kwargs)
            self.assertIs(kwargs.get("ephemeral"), True)
            self.assertIn("이미 생성", args[0])

    async def test_full_party_rejects_further_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = "PUBG"  # 역할 없는 게임
            repository.create_party(game, 1_000)
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            for user_id in range(playwith_cog.GAMES[game]["max_players"]):
                self.assertTrue(cog.add_participant(game, user_id))

            self.assertFalse(cog.add_participant(game, 999))
            self.assertIsNone(repository.get_user_party(999))


class PartyMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_leaving_member_frees_party_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(game, 1_000)
            repository.add_participant(game, 42, "탑")
            repository.add_participant(game, 43, "미드")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            member = SimpleNamespace(
                id=42, guild=SimpleNamespace(id=config.DISCORD_GUILD_ID)
            )
            await cog.on_member_remove(member)

            self.assertIsNone(repository.get_user_party(42))
            self.assertEqual(repository.get_participants(game), {43: "미드"})
            self.assertIsNotNone(repository.get_party(game))

    async def test_last_leaving_member_disbands_the_party(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(game, 1_000)
            repository.add_participant(game, 42, "탑")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            await cog.on_member_remove(
                SimpleNamespace(id=42, guild=SimpleNamespace(id=config.DISCORD_GUILD_ID))
            )

            self.assertIsNone(repository.get_party(game))

    async def test_party_count_matches_listed_members(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(game, 1_000)
            repository.add_participant(game, 1, "탑")
            repository.add_participant(game, 2, "미드")  # 서버를 이미 떠난 유저
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            class PartialGuild(FakeGuild):
                def get_member(self, user_id):
                    return FakeUser() if user_id == 1 else None

            interaction = FakeInteraction(channel_id=1, guild=PartialGuild())
            with patch.object(playwith_cog, "RECRUIT_CHANNEL_ID", 1):
                await PlayWithCog.파티.callback(cog, interaction)

            description = interaction.response.messages[0][1]["embeds"][0].description
            self.assertIn("현재 인원: 1 /", description)


class BackupConnectionTests(unittest.TestCase):
    def test_backup_database_connections_are_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "party.db"
            temporary = pathlib.Path(directory) / "backup.db"
            SQLitePartyRepository(source)

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                backup._backup_one(source, temporary)
                backup.verify_database(temporary, {"parties", "participants"})
                gc.collect()

        self.assertFalse(
            [warning for warning in caught if issubclass(warning.category, ResourceWarning)]
        )
