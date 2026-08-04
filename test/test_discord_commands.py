import asyncio
import gc
import hashlib
import json
import pathlib
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import call, patch

import discord
import httpx
import openai

import module.config as config
import module.main as bot_main
from module.attendance_cog import AttendanceCog, KST
from module.database import (
    SQLiteAttendanceRepository,
    SQLiteGuildSettingsRepository,
    SQLitePartyRepository,
)
from module.eventnotice_cog import EventNoticeCog
from module.finance_cog import FinanceCog
from module.guildsettings_cog import GuildSettingsCog, SetupView
from module.hyacine_chat_cog import HyacineChatCog
from module.hyacine_image_cog import HyacineImageCog
from module.playwith_cog import PlayWithCog
from module.panel import drop_panel_locks, panel_lock, upsert_panel
import module.playwith_cog as playwith_cog
import module.forbiddenfilter_cog as forbiddenfilter_cog
import module.backup as backup



# 단일 운영 길드를 전제하던 상수가 사라졌다. 테스트는 임의의 길드 하나를 쓴다.
TEST_GUILD_ID = 4_242

_PARTY_TEST_GAMES = {
    "League of Legends": {"max_players": 5, "roles": ["탑", "정글", "미드", "원딜", "서포터"]},
    "PUBG": {"max_players": 4, "roles": []},
    "Overwatch": {"max_players": 5, "roles": ["딜러1", "딜러2", "탱커", "힐러1", "힐러2"]},
}


def setUpModule():
    games_patch = patch.object(playwith_cog, "GAMES", _PARTY_TEST_GAMES)
    games_patch.start()
    unittest.addModuleCleanup(games_patch.stop)


class MinimalConfigTest(unittest.TestCase):
    def test_only_discord_token_is_required(self):
        with patch.object(config, "DISCORD_TOKEN", "t"), \
             patch.object(config, "OPENAI_API_KEY", None), \
             patch.object(config, "GOOGLE_API_KEY", None):
            config.validate_config()

    def test_missing_discord_token_still_raises(self):
        with patch.object(config, "DISCORD_TOKEN", None):
            with self.assertRaises(RuntimeError):
                config.validate_config()

    def test_model_and_limit_defaults_exist(self):
        self.assertTrue(config.CHAT_MODEL_LIGHT)
        self.assertTrue(config.IMAGE_MODEL)
        self.assertGreater(config.LIMIT_LIGHT, 0)
        self.assertGreater(config.LIMIT_DEEP, 0)
        self.assertGreater(config.LIMIT_IMAGE, 0)


class ConditionalExtensionTest(unittest.TestCase):
    def test_all_extensions_load_when_every_key_present(self):
        with patch.object(bot_main, "ENV_VALUES", {"OPENAI_API_KEY": "a", "GOOGLE_API_KEY": "b"}):
            names = bot_main.available_extensions()
        self.assertIn("module.hyacine_chat_cog", names)
        self.assertIn("module.hyacine_image_cog", names)

    def test_ai_extensions_skipped_without_keys(self):
        with patch.object(bot_main, "ENV_VALUES", {"OPENAI_API_KEY": None, "GOOGLE_API_KEY": None}):
            names = bot_main.available_extensions()
        self.assertNotIn("module.hyacine_chat_cog", names)
        self.assertNotIn("module.hyacine_image_cog", names)

    def test_core_extensions_survive_with_no_optional_keys(self):
        with patch.object(bot_main, "ENV_VALUES", {"OPENAI_API_KEY": None, "GOOGLE_API_KEY": None}):
            names = bot_main.available_extensions()
        for required in (
            "module.guildsettings_cog",
            "module.playwith_cog",
            "module.forbiddenfilter_cog",
            "module.attendance_cog",
        ):
            self.assertIn(required, names)


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

    async def defer(self, **kwargs):
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
        self.id = TEST_GUILD_ID
        self.fetch_scheduled_events_calls = 0
        self.events = events or []

    async def fetch_scheduled_events(self):
        self.fetch_scheduled_events_calls += 1
        return self.events

    def get_member(self, user_id):
        return FakeUser()


_UNSET = object()  # guild_id=None(=DM)과 "기본값 사용"을 구분한다


class FakeInteraction:
    def __init__(self, channel_id, guild=None, guild_id=_UNSET, message_id=10, user=None):
        self.channel_id = channel_id
        self.user = user or FakeUser()
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.guild = guild
        self.created_at = datetime.now(timezone.utc)
        # 기본값은 임의의 길드 — 경계 테스트만 다른 값(또는 DM을 뜻하는 None)을 넘긴다.
        self.guild_id = TEST_GUILD_ID if guild_id is _UNSET else guild_id
        self.message = SimpleNamespace(id=message_id) if message_id is not None else None


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
    def __init__(self, refund_error=None, reserve_result=("2026-08-04", 1), deduct_result=True, usage=None):
        self.deductions = []
        self.refunds = []
        self.refund_attempts = []
        self.reasons = []
        self.reservations = []
        self.releases = []
        self.refund_error = refund_error
        self.reserve_result = reserve_result
        self.deduct_result = deduct_result
        self.usage = usage or {}

    async def reserve_ai_usage(self, user_id, command, limit):
        self.reservations.append((user_id, command, limit))
        return self.reserve_result

    async def release_ai_usage(self, user_id, usage_date, command):
        self.releases.append((user_id, usage_date, command))
        return True

    async def get_ai_usage(self, user_id, command):
        return self.usage.get(command, 0)

    async def deduct_points(self, guild_id, user_id, amount, reason="unspecified"):
        self.deductions.append((user_id, amount))
        self.reasons.append(reason)
        return self.deduct_result

    async def get_points(self, guild_id, user_id):
        return 0

    async def add_points(self, guild_id, user_id, amount, reason="unspecified"):
        self.reasons.append(reason)
        self.refund_attempts.append((user_id, amount))
        if self.refund_error:
            raise self.refund_error
        self.refunds.append((user_id, amount))


class ImageCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.google_key = patch("module.hyacine_image_cog.GOOGLE_API_KEY", "test-dummy")
        self.google_key.start()
        self.addCleanup(self.google_key.stop)

    async def test_daily_limit_stops_before_points_and_provider(self):
        attendance = RecordingAttendance(reserve_result=None)
        interaction = FakeInteraction(channel_id=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))
        provider_calls = []
        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kwargs: provider_calls.append(kwargs)
            )
        )

        await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(
            attendance.reservations,
            [(123, "image", config.LIMIT_IMAGE)],
        )
        self.assertEqual(attendance.deductions, [])
        self.assertEqual(provider_calls, [])
        self.assertIn("오늘 사용 횟수", interaction.response.messages[-1][0][0])

    async def test_insufficient_points_releases_reserved_slot(self):
        attendance = RecordingAttendance(deduct_result=False)
        interaction = FakeInteraction(channel_id=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))

        await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(attendance.releases, [(123, "2026-08-04", "image")])
        self.assertIn("포인트가 부족", interaction.response.messages[-1][0][0])

    async def test_defer_failure_refunds_points_and_releases_slot(self):
        attendance = RecordingAttendance()
        interaction = FakeInteraction(channel_id=1)
        cog = HyacineImageCog(SimpleNamespace(get_cog=lambda _: attendance))

        async def fail_defer():
            raise RuntimeError("defer transport failed")

        interaction.response.defer = fail_defer
        with patch("module.hyacine_image_cog.print"), patch(
            "module.hyacine_image_cog.traceback.print_exc"
        ):
            await HyacineImageCog._image.callback(cog, interaction, "test")

        self.assertEqual(attendance.refunds, [(123, 30_000)])
        self.assertEqual(attendance.releases, [(123, "2026-08-04", "image")])

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
        self.assertEqual(attendance.releases, [(123, "2026-08-04", "image")])
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
        self.assertEqual(attendance.releases, [])

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
        self.attendance = RecordingAttendance()
        self.cog = HyacineChatCog(
            bot=SimpleNamespace(get_cog=lambda _: self.attendance)
        )

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
                (basic, "기본 대화", None, "gpt-5.6-terra", "none", 200, "light", config.LIMIT_LIGHT),
                (advanced, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000, "deep", config.LIMIT_DEEP),
            ],
        )

    async def test_status_lists_both_models_and_last_usage(self):
        class RecordingUsageRepository:
            def __init__(self):
                self.calls = []
                self.counts = {
                    "light": 3,
                    "deep": config.LIMIT_DEEP + 5,
                    "image": 1,
                }

            def get_ai_usage(self, user_id, usage_date, command):
                self.calls.append((user_id, usage_date, command))
                return self.counts[command]

        repository = RecordingUsageRepository()
        attendance = AttendanceCog(bot=None, repository=repository)
        self.cog.bot = SimpleNamespace(get_cog=lambda _: attendance)
        interaction = FakeInteraction(channel_id=1)
        self.cog.get_session(1).last_usage = {
            "model": "gpt-5.6-sol",
            "input_tokens": 12,
            "output_tokens": 34,
            "total_tokens": 46,
        }

        with patch("module.attendance_cog.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 5, 0, 1, tzinfo=KST)
            await HyacineChatCog._status.callback(self.cog, interaction)

        message = interaction.response.messages[-1][0][0]
        for text in ("/기본대화", "gpt-5.6-terra", "/고급대화", "gpt-5.6-sol", "직전 사용 모델", "46"):
            self.assertIn(text, message)
        lines = message.splitlines()
        self.assertIn(
            f"- `/기본대화`: {max(0, config.LIMIT_LIGHT - 3)}/{config.LIMIT_LIGHT}회",
            lines,
        )
        self.assertIn(f"- `/고급대화`: 0/{config.LIMIT_DEEP}회", lines)
        self.assertIn(
            f"- `/이미지`: {config.LIMIT_IMAGE - 1}/{config.LIMIT_IMAGE}회",
            lines,
        )
        self.assertEqual(
            repository.calls,
            [
                (123, "2026-08-05", "light"),
                (123, "2026-08-05", "deep"),
                (123, "2026-08-05", "image"),
            ],
        )
        self.assertEqual(mocked_datetime.now.call_args_list, [call(KST)] * 3)

    async def test_status_preserves_models_when_attendance_is_unavailable(self):
        self.cog.bot = None
        interaction = FakeInteraction(channel_id=1)
        self.cog.get_session(1).last_usage = {
            "model": "gpt-5.6-sol",
            "total_tokens": 46,
        }

        await HyacineChatCog._status.callback(self.cog, interaction)

        message = interaction.response.messages[-1][0][0]
        self.assertIn("gpt-5.6-terra", message)
        self.assertIn("gpt-5.6-sol", message)
        self.assertIn("46", message)
        self.assertIn("확인할 수 없어요", message)

    async def test_daily_limit_stops_before_points_and_provider(self):
        attendance = RecordingAttendance(reserve_result=None)
        self.cog.bot = SimpleNamespace(get_cog=lambda _: attendance)
        interaction = FakeInteraction(channel_id=1)
        provider_calls = []

        async def provider(**kwargs):
            provider_calls.append(kwargs)

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=provider)
        )

        await self.cog._run_talk(
            interaction, "test", None, "gpt-5.6-terra", "none", 200,
            "light", config.LIMIT_LIGHT,
        )

        self.assertEqual(attendance.deductions, [])
        self.assertEqual(provider_calls, [])
        self.assertIn("오늘 사용 횟수", interaction.response.messages[-1][0][0])

    async def test_insufficient_points_releases_reserved_slot(self):
        attendance = RecordingAttendance(deduct_result=False)
        self.cog.bot = SimpleNamespace(get_cog=lambda _: attendance)
        interaction = FakeInteraction(channel_id=1)

        await self.cog._run_talk(
            interaction, "test", None, "gpt-5.6-terra", "none", 200,
            "light", config.LIMIT_LIGHT,
        )

        self.assertEqual(attendance.releases, [(123, "2026-08-04", "light")])

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
            interaction, "", attachment, "gpt-5.6-terra", "none", 0,
            "light", config.LIMIT_LIGHT,
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
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000,
                "deep", config.LIMIT_DEEP,
            )

        self.assertEqual(attendance.deductions, [(123, 2_000)])
        self.assertEqual(attendance.refunds, [(123, 2_000)])
        self.assertEqual(bot.calls, 1)
        self.assertEqual(attendance.releases, [(123, "2026-08-04", "deep")])
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
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000,
                "deep", config.LIMIT_DEEP,
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
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000,
                "deep", config.LIMIT_DEEP,
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
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000,
                "deep", config.LIMIT_DEEP,
            )

        self.assertEqual(attendance.refunds, [(123, 2_000)])
        self.assertEqual(attendance.releases, [(123, "2026-08-04", "deep")])
        self.assertEqual(interaction.followup.messages, [])
        self.assertIn("포인트 환불됨", interaction.response.messages[-1][0][0])


class ChatConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.hyacine_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)

    async def test_same_channel_calls_do_not_interleave_history(self):
        attendance = RecordingAttendance()
        cog = HyacineChatCog(bot=SimpleNamespace(get_cog=lambda _: attendance))
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
            cog._run_talk(FakeInteraction(channel_id=1), "first", None, "gpt-5.6-terra", "none", 0, "light", config.LIMIT_LIGHT),
            cog._run_talk(FakeInteraction(channel_id=1), "second", None, "gpt-5.6-terra", "none", 0, "light", config.LIMIT_LIGHT),
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
        self.google_key = patch("module.hyacine_image_cog.GOOGLE_API_KEY", "test-dummy")
        self.google_key.start()
        self.addCleanup(self.google_key.stop)

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

    def test_ai_command_descriptions_are_model_neutral(self):
        descriptions = " ".join(
            command.description
            for command in (
                HyacineChatCog._light_talk,
                HyacineChatCog._deep_talk,
                HyacineImageCog._image,
            )
        )
        for model_label in ("GPT-5.6", "Terra", "Sol", "Nano Banana"):
            self.assertNotIn(model_label, descriptions)

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
                interaction, "고급 대화", None, "gpt-5.6-sol", "medium", 2_000,
                "deep", config.LIMIT_DEEP,
            )

        message = interaction.followup.messages[-1][0][0]
        self.assertIn("요청이 몰려", message)
        self.assertIn("포인트 환불됨", message)
        self.assertEqual(attendance.refunds, [(123, 2_000)])
        self.assertEqual(attendance.releases, [])


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
        self.attendance.db.add_points(TEST_GUILD_ID, FakeUser.id, 2_000)

        success = FakeInteraction(channel_id=1)
        with patch("module.attendance_cog.random.randint", return_value=7_000):
            await AttendanceCog._attend.callback(self.attendance, success)

        success_args, success_kwargs = success.response.messages[0]
        self.assertEqual(success_args, ())
        self.assertEqual(self.attendance.db.get_points(TEST_GUILD_ID, FakeUser.id), 9_000)
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
        self.assertEqual(self.attendance.db.get_points(TEST_GUILD_ID, FakeUser.id), 9_000)

    async def test_legacy_party_slash_commands_are_removed(self):
        for command in ("모집", "파티", "나가기", "변경"):
            self.assertNotIn(command, PlayWithCog.__dict__)

    async def test_event_commands_reach_their_backend_from_any_channel(self):
        self.assertNotIn("settings", EventNoticeCog.__init__.__code__.co_varnames)

        guild = FakeGuild()
        interaction = FakeInteraction(channel_id=-1, guild=guild)
        await EventNoticeCog.show_specific_event.callback(EventNoticeCog(bot=None), interaction, 1)

        self.assertEqual(guild.fetch_scheduled_events_calls, 1)

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
        await EventNoticeCog.show_specific_event.callback(cog, listing, None)
        await EventNoticeCog.show_specific_event.callback(cog, detail, 1)

        list_text = listing.followup.messages[0][1]["embed"].description
        self.assertLess(list_text.index("같은 시각 앞"), list_text.index("같은 시각 뒤"))
        self.assertLess(list_text.index("같은 시각 뒤"), list_text.index("두 번째"))
        self.assertIn("같은 시각 앞", detail.followup.messages[0][1]["embed"].title)


class _PartyMessage:
    def __init__(self, message_id, author_id=999):
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.edits = []
        self.deleted = False
        self.delete_calls = 0

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True
        self.delete_calls += 1


class _PartyChannel:
    def __init__(self, channel_id=50):
        self.id = channel_id
        self.messages = {}
        self.sent = []
        self.fetch_errors = {}

    async def fetch_message(self, message_id):
        if message_id in self.fetch_errors:
            raise self.fetch_errors[message_id]
        if message_id not in self.messages:
            raise discord.NotFound(_FakeResponse(), "missing")
        return self.messages[message_id]

    async def send(self, **kwargs):
        message = _PartyMessage(100 + len(self.sent))
        self.messages[message.id] = message
        self.sent.append((message, kwargs))
        return message


class _PartyGuild(FakeGuild):
    def __init__(self, guild_id=TEST_GUILD_ID, channel=None, members=None):
        super().__init__()
        self.id = guild_id
        self.channel = channel or _PartyChannel()
        self.members = members or {FakeUser.id: FakeUser()}

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None

    def get_member(self, user_id):
        return self.members.get(user_id)


class _PartyBot:
    def __init__(self, guild):
        self.guilds = [guild]
        self.user = SimpleNamespace(id=999)
        self.registered = []

    def add_view(self, view):
        self.registered.append(view)

    def get_guild(self, guild_id):
        return self.guilds[0] if self.guilds[0].id == guild_id else None


class PartyPanelTests(unittest.IsolatedAsyncioTestCase):
    def _make_cog(self, root, guild, games=None):
        party = SQLitePartyRepository(root / "party.db")
        settings = SQLiteGuildSettingsRepository(root / "settings.db")
        settings.set_party_channel(guild.id, guild.channel.id)
        bot = _PartyBot(guild)
        patches = patch.object(playwith_cog, "GAMES", games) if games else None
        if patches:
            patches.start()
            self.addCleanup(patches.stop)
        with patch("discord.ext.tasks.Loop.start"):
            cog = PlayWithCog(bot, party, settings)
        return cog, party, settings, bot

    def test_views_use_digest_ids_and_stay_within_component_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, _, bot = self._make_cog(pathlib.Path(directory), guild)

        self.assertEqual(set(cog.views), set(playwith_cog.GAMES))
        self.assertEqual(len(bot.registered), len(playwith_cog.GAMES))
        for game, view in cog.views.items():
            self.assertTrue(view.is_persistent())
            self.assertLessEqual(len(view.children), 25)
            expected = hashlib.sha256(game.encode("utf-8")).hexdigest()[:16]
            for child in view.children:
                self.assertIn(expected, child.custom_id)
                self.assertNotIn(game, child.custom_id)
                self.assertLess(len(child.custom_id), 100)
            expected_count = len(playwith_cog.GAMES[game]["roles"]) + 1
            self.assertEqual(len(view.children), expected_count if expected_count > 1 else 1)

    def test_role_ids_survive_reordering_and_change_on_rename(self):
        original = {"Game": {"max_players": 2, "roles": ["A", "B"]}}
        reordered = {"Game": {"max_players": 2, "roles": ["B", "A"]}}
        renamed = {"Game": {"max_players": 2, "roles": ["A", "C"]}}
        dummy = object()

        def role_ids(games):
            with patch.object(playwith_cog, "GAMES", games):
                view = playwith_cog.PartyPanelView(dummy, "Game")
            return {button.role: button.custom_id for button in view.children[1:]}

        first = role_ids(original)
        second = role_ids(reordered)
        third = role_ids(renamed)
        self.assertEqual(first["A"], second["A"])
        self.assertEqual(first["B"], second["B"])
        self.assertNotIn(first["B"], third.values())

    async def test_every_configured_game_gets_its_own_panel(self):
        games = {
            f"Game {index}": {"max_players": 2, "roles": []}
            for index in range(30)
        }
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, settings, bot = self._make_cog(
                pathlib.Path(directory), guild, games
            )
            await cog.ensure_panels(guild)
            panel_count = len(settings.get_party_panels(guild.id))

        self.assertEqual(len(cog.views), 30)
        self.assertEqual(len(bot.registered), 30)
        self.assertEqual(panel_count, 30)

    async def test_over_limit_game_is_disabled_without_stopping_other_panels(self):
        games = {
            "Too Many": {"max_players": 25, "roles": [f"r{i}" for i in range(25)]},
            "Okay": {"max_players": 2, "roles": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, _, bot = self._make_cog(pathlib.Path(directory), guild, games)
            await cog.ensure_panels(guild)

        self.assertEqual(len(bot.registered), 1)
        disabled = guild.channel.sent[0][1]
        self.assertIn("비활성", disabled["embed"].title)
        self.assertIsNone(disabled["view"])
        self.assertIsNotNone(guild.channel.sent[1][1]["view"])

    async def test_role_and_no_role_buttons_cover_create_change_leave_and_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            second_user = SimpleNamespace(id=456, mention="<@456>")
            guild = _PartyGuild(members={FakeUser.id: FakeUser(), 456: second_user})
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)

            game = "League of Legends"
            panel_id = settings.get_party_panels(guild.id)[game]
            inactive, top = cog.views[game].children[:2]
            premature = FakeInteraction(50, guild, message_id=panel_id)
            await top.callback(premature)
            self.assertIsNone(party.get_party(guild.id, game))
            self.assertIn("모집 시작", premature.followup.messages[0][0][0])

            await inactive.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertEqual(party.get_participants(guild.id, game), {FakeUser.id: None})
            status_click = FakeInteraction(
                50, guild, message_id=panel_id, user=second_user
            )
            await inactive.callback(status_click)
            self.assertNotIn(456, party.get_participants(guild.id, game))
            await top.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertEqual(party.get_participants(guild.id, game), {FakeUser.id: "탑"})
            await top.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertIsNone(party.get_party(guild.id, game))

            game = "PUBG"
            panel_id = settings.get_party_panels(guild.id)[game]
            toggle = cog.views[game].children[0]
            await toggle.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertEqual(party.get_user_party(guild.id, FakeUser.id), game)
            await toggle.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertIsNone(party.get_user_party(guild.id, FakeUser.id))

    async def test_repository_rejections_and_concurrent_role_clicks_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            users = {uid: SimpleNamespace(id=uid, mention=f"<@{uid}>") for uid in range(1, 8)}
            guild = _PartyGuild(members=users)
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            game = "League of Legends"
            panel_id = settings.get_party_panels(guild.id)[game]
            party.create_party(guild.id, game, 1_000)
            for uid, role in zip(range(1, 6), playwith_cog.GAMES[game]["roles"]):
                party.add_participant(guild.id, game, uid, role, 5)

            full = FakeInteraction(50, guild, message_id=panel_id, user=users[6])
            await cog.views[game].children[1].callback(full)
            self.assertIn("가득", full.followup.messages[0][0][0])
            self.assertIsNone(party.get_user_party(guild.id, 6))

            party.remove_participant(guild.id, game, 5)
            racer = users[7]
            first = FakeInteraction(50, guild, message_id=panel_id, user=racer)
            second = FakeInteraction(50, guild, message_id=panel_id, user=racer)
            await asyncio.gather(
                cog.views[game].children[4].callback(first),
                cog.views[game].children[5].callback(second),
            )
            self.assertEqual(party.get_participants(guild.id, game)[7], "서포터")
            self.assertTrue(first.followup.messages and second.followup.messages)

    async def test_host_persists_across_role_changes_and_transfers_on_departure(self):
        host = SimpleNamespace(id=1, mention="<@1>")
        successor = SimpleNamespace(id=2, mention="<@2>")
        guild = _PartyGuild(members={1: host, 2: successor})
        with tempfile.TemporaryDirectory() as directory:
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            game = "League of Legends"
            panel_id = settings.get_party_panels(guild.id)[game]
            buttons = cog.views[game].children

            await buttons[0].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=host)
            )
            await buttons[1].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=host)
            )
            await buttons[2].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=successor)
            )
            await buttons[3].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=host)
            )
            self.assertEqual(party.get_party_host(guild.id, game), 1)

            await buttons[3].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=host)
            )
            transferred_host = party.get_party_host(guild.id, game)
            embed = guild.channel.messages[panel_id].edits[-1]["embed"]

        self.assertEqual(transferred_host, 2)
        self.assertIn("방장: <@2>", embed.description)

    async def test_cross_game_clicks_keep_one_membership_and_no_empty_loser_party(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            lol = FakeInteraction(
                50,
                guild,
                message_id=settings.get_party_panels(guild.id)["League of Legends"],
            )
            pubg = FakeInteraction(
                50,
                guild,
                message_id=settings.get_party_panels(guild.id)["PUBG"],
            )
            await asyncio.gather(
                cog.views["League of Legends"].children[0].callback(lol),
                cog.views["PUBG"].children[0].callback(pubg),
            )
            joined = party.get_user_party(guild.id, FakeUser.id)
            active = [
                game for game in ("League of Legends", "PUBG")
                if party.get_party(guild.id, game) is not None
            ]

        self.assertIn(joined, active)
        self.assertEqual(len(active), 1)
        self.assertTrue(lol.followup.messages and pubg.followup.messages)

    async def test_panel_id_is_rechecked_after_waiting_for_game_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            game = "PUBG"
            panel_id = settings.get_party_panels(guild.id)[game]
            interaction = FakeInteraction(50, guild, message_id=panel_id)
            deferred = asyncio.Event()
            original_defer = interaction.response.defer

            async def recording_defer(**kwargs):
                await original_defer(**kwargs)
                deferred.set()

            interaction.response.defer = recording_defer
            lock = panel_lock(guild.id, f"party:{game}")
            await lock.acquire()
            task = asyncio.create_task(cog.views[game].children[0].callback(interaction))
            await deferred.wait()
            settings.set_party_panel(guild.id, game, panel_id + 1)
            lock.release()
            await task
            current_party = party.get_party(guild.id, game)

        self.assertIsNone(current_party)
        self.assertIn("최신", interaction.followup.messages[0][0][0])

    async def test_dm_cross_guild_stale_panel_and_deleted_member_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            game = "PUBG"
            button = cog.views[game].children[0]
            panel_id = settings.get_party_panels(guild.id)[game]

            dm = FakeInteraction(50, guild=None, guild_id=None, message_id=panel_id)
            await button.callback(dm)
            wrong = FakeInteraction(50, guild, guild_id=guild.id + 1, message_id=panel_id)
            await button.callback(wrong)
            stale = FakeInteraction(50, guild, message_id=panel_id + 1)
            await button.callback(stale)
            guild.members.clear()
            deleted = FakeInteraction(50, guild, message_id=panel_id)
            await button.callback(deleted)
            user_party = party.get_user_party(guild.id, FakeUser.id)

        self.assertIsNone(user_party)
        self.assertTrue(dm.response.messages and wrong.response.messages and deleted.response.messages)
        self.assertTrue(stale.followup.messages)

    async def test_ensure_panels_edits_recreates_and_cleans_only_bot_stale_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            current = next(iter(playwith_cog.GAMES))
            guild.channel.messages[10] = _PartyMessage(10)
            guild.channel.messages[20] = _PartyMessage(20)
            settings.set_party_panel(guild.id, current, 10)
            settings.set_party_panel(guild.id, "Removed", 20)
            await cog.ensure_panels(guild)

            self.assertTrue(guild.channel.messages[10].edits)
            self.assertTrue(guild.channel.messages[20].deleted)
            self.assertNotIn("Removed", settings.get_party_panels(guild.id))

            missing_game = list(playwith_cog.GAMES)[1]
            old_id = settings.get_party_panels(guild.id)[missing_game]
            guild.channel.messages.pop(old_id)
            await cog.ensure_panels(guild)
            self.assertNotEqual(settings.get_party_panels(guild.id)[missing_game], old_id)

    async def test_concurrent_stale_cleanup_deletes_once_and_preserves_nonbot_message(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            bot_message = _PartyMessage(20)
            user_message = _PartyMessage(21, author_id=1234)
            guild.channel.messages.update({20: bot_message, 21: user_message})
            settings.set_party_panel(guild.id, "Removed Bot", 20)
            settings.set_party_panel(guild.id, "Removed User", 21)

            await asyncio.gather(cog.ensure_panels(guild), cog.ensure_panels(guild))
            remaining = settings.get_party_panels(guild.id)

        self.assertEqual(bot_message.delete_calls, 1)
        self.assertFalse(user_message.deleted)
        self.assertNotIn("Removed Bot", remaining)
        self.assertNotIn("Removed User", remaining)

    async def test_transport_error_preserves_panel_row_and_renderer_reads_latest_state(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            game = "League of Legends"
            settings.set_party_panel(guild.id, game, 77)
            settings.set_party_panel(guild.id, "Removed", 88)
            guild.channel.fetch_errors[77] = discord.Forbidden(_FakeResponse(403), "no")
            guild.channel.fetch_errors[88] = discord.HTTPException(_FakeResponse(500), "no")
            await cog.ensure_panels(guild)
            self.assertEqual(settings.get_party_panels(guild.id)[game], 77)
            self.assertEqual(settings.get_party_panels(guild.id)["Removed"], 88)

            guild.channel.fetch_errors.clear()
            guild.channel.messages[77] = _PartyMessage(77)
            party.create_party(guild.id, game, 1_000)
            party.add_participant(guild.id, game, FakeUser.id, "탑", 5)
            await cog.render_game_panel(guild.id, game)
            embed = guild.channel.messages[77].edits[-1]["embed"]
            self.assertIn("1 / 5", embed.description)
            self.assertIn("탑: <@123>", embed.fields[0].value)

    async def test_startup_member_removal_and_expiry_render_only_affected_panels(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, _, _ = self._make_cog(pathlib.Path(directory), guild)
            with patch.object(cog, "ensure_panels") as ensure:
                await cog.on_ready()
                await cog.on_ready()
                ensure.assert_awaited_once_with(guild)

            party.create_party(guild.id, "PUBG", 1_000)
            party.add_participant(guild.id, "PUBG", FakeUser.id, None, 4)
            member = SimpleNamespace(id=FakeUser.id, guild=guild)
            with patch.object(cog, "render_game_panel") as render:
                await cog.on_member_remove(member)
                render.assert_awaited_once_with(guild.id, "PUBG")

            party.create_party(guild.id, "Overwatch", 1_000)
            expiry_lock_states = []
            delete_if_expired = party.delete_party_if_expired

            def recording_delete(guild_id, game, cutoff):
                expiry_lock_states.append(
                    panel_lock(guild_id, f"party:{game}").locked()
                )
                return delete_if_expired(guild_id, game, cutoff)

            with patch("module.playwith_cog.time.time", return_value=100_000), patch.object(
                party, "delete_party_if_expired", side_effect=recording_delete
            ), patch.object(cog, "render_game_panel") as render:
                await PlayWithCog.cleanup_parties.coro(cog)
                render.assert_awaited_once_with(guild.id, "Overwatch")
                self.assertEqual(expiry_lock_states, [True])

    async def test_startup_isolates_guild_failure_and_retries_next_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            first = _PartyGuild(TEST_GUILD_ID)
            second = _PartyGuild(TEST_GUILD_ID + 1)
            cog, _, _, bot = self._make_cog(pathlib.Path(directory), first)
            bot.guilds.append(second)
            attempts = []
            failed_once = False

            async def restore(guild):
                nonlocal failed_once
                attempts.append(guild.id)
                if guild is first and not failed_once:
                    failed_once = True
                    raise RuntimeError("temporary DB failure")

            with patch.object(cog, "ensure_panels", side_effect=restore), patch.object(
                playwith_cog.logger, "exception"
            ):
                await cog.on_ready()
                self.assertFalse(cog._panels_restored)
                await cog.on_ready()

        self.assertEqual(
            attempts,
            [first.id, second.id, first.id, second.id],
        )
        self.assertTrue(cog._panels_restored)

    def test_conditional_expiry_does_not_delete_a_new_party_incarnation(self):
        with tempfile.TemporaryDirectory() as directory:
            party = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            party.create_party(TEST_GUILD_ID, "PUBG", 1_000, 1)
            candidates = party.list_expired_parties(2_000)
            party.delete_party(TEST_GUILD_ID, "PUBG")
            party.create_party(TEST_GUILD_ID, "PUBG", 3_000, 2)

            deleted = party.delete_party_if_expired(
                TEST_GUILD_ID, "PUBG", 2_000
            )
            host = party.get_party_host(TEST_GUILD_ID, "PUBG")
            participants = party.get_participants(TEST_GUILD_ID, "PUBG")

        self.assertEqual(candidates, [(TEST_GUILD_ID, "PUBG")])
        self.assertFalse(deleted)
        self.assertEqual(host, 2)
        self.assertEqual(participants, {2: None})


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

    async def increment_forbidden_count(self, guild_id, user_id):
        self.counts.append(user_id)


def make_forbidden_cog(counter, words=("나쁜말",)):
    with tempfile.TemporaryDirectory() as directory:
        pathlib.Path(directory, "forbidden_words.json").write_text(
            json.dumps(list(words)), encoding="utf-8"
        )
        with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)), patch(
            "module.forbiddenfilter_cog.print"
        ):
            cog = forbiddenfilter_cog.ForbiddenFilterCog(
                SimpleNamespace(get_cog=lambda _: counter)
            )
    return cog


class ForbiddenFilterDegradesTest(unittest.TestCase):
    def test_missing_file_yields_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                self.assertEqual(forbiddenfilter_cog.load_forbidden_words(), [])

    def test_cog_constructs_without_word_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = forbiddenfilter_cog.ForbiddenFilterCog(bot=None)
        self.assertIsNone(cog._find_match("아무 말이나"))


class GuildBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """봇은 여러 서버에 설치된다. 경계는 '한 서버만 허용'이 아니라 '서버끼리 안 섞임'이다."""

    async def test_dm_messages_are_ignored(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)

        # DM은 귀속시킬 길드가 없어 집계 대상이 아니다.
        direct_message = FakeMessage("나쁜말", guild_id=None)
        await cog.on_message(direct_message)

        self.assertEqual(counter.counts, [])
        self.assertEqual(direct_message.sent, [])

    async def test_every_guild_is_screened_independently(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)

        first = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)
        second = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID + 1)
        for message in (first, second):
            await cog.on_message(message)

        # 어느 서버든 검사한다. 카운트는 서버별로 따로 쌓인다(스키마가 분리).
        self.assertEqual(counter.counts, [FakeUser.id, FakeUser.id])
        self.assertIn("나쁜말", first.sent[0])
        self.assertIn("나쁜말", second.sent[0])

    async def test_commands_are_registered_globally_without_guild_pinning(self):
        import module.main as main

        events = []

        class FakeTree:
            async def sync(self, *, guild=None):
                events.append("global" if guild is None else f"guild:{guild}")

        class FakeBot:
            tree = FakeTree()

            async def load_extension(self, extension):
                pass

        with patch.object(main, "_verify_databases"), patch.object(
            main, "DATA_DIR", pathlib.Path(tempfile.mkdtemp())
        ):
            await main.MyBot.setup_hook(FakeBot())

        # 공개 배포 봇은 어떤 서버에 초대될지 미리 알 수 없다.
        self.assertEqual(events, ["global"])
        self.assertFalse(hasattr(main, "DISCORD_GUILD_ID"))


class _FakeResponse:
    def __init__(self, status=404):
        self.status = status
        self.reason = "test"
        self.headers = {}
        self.text = ""


class _PanelMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _PanelChannel:
    def __init__(self):
        self.message = _PanelMessage(10)
        self.fetch_error = None
        self.sent = []

    async def fetch_message(self, message_id):
        if self.fetch_error:
            raise self.fetch_error
        return self.message

    async def send(self, **kwargs):
        message = _PanelMessage(11)
        self.sent.append((message, kwargs))
        return message


class PanelLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_edits_or_replaces_without_hiding_transport_errors(self):
        channel = _PanelChannel()
        embed = object()
        view = object()

        first = await upsert_panel(channel, 10, embed=embed, view=view)
        self.assertEqual(first.id, 10)
        self.assertEqual(channel.message.edits[-1], {"embed": embed, "view": view})

        channel.fetch_error = discord.NotFound(_FakeResponse(), "missing")
        replacement = await upsert_panel(channel, 10, embed=embed, view=view)
        self.assertEqual(replacement.id, 11)

        channel.fetch_error = discord.Forbidden(_FakeResponse(403), "forbidden")
        with self.assertRaises(discord.Forbidden):
            await upsert_panel(channel, 10, embed=embed, view=view)

        channel.fetch_error = discord.HTTPException(_FakeResponse(500), "failed")
        with self.assertRaises(discord.HTTPException):
            await upsert_panel(channel, 10, embed=embed, view=view)

    def test_panel_locks_are_scoped_by_guild_and_key(self):
        try:
            self.assertIs(panel_lock(1, "party:A"), panel_lock(1, "party:A"))
            self.assertIsNot(panel_lock(1, "party:A"), panel_lock(2, "party:A"))
        finally:
            drop_panel_locks(1)
            drop_panel_locks(2)


class _SetupChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"


class _SetupGuild:
    def __init__(self, guild_id=999):
        self.id = guild_id
        self.default_role = object()
        self.me = object()
        self.categories = []
        self.channels = {}
        self.created_categories = []
        self.created_channels = []
        self.system_channel = None

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def create_category(self, name, *, overwrites):
        category = SimpleNamespace(name=name, overwrites=overwrites)
        self.categories.append(category)
        self.created_categories.append(category)
        return category

    async def create_text_channel(self, name, *, category):
        channel = _SetupChannel(len(self.channels) + 1, name)
        self.channels[channel.id] = channel
        self.created_channels.append((channel, category))
        return channel


class _SetupBot:
    def __init__(self, play_cog=None):
        self.views = []
        self.play_cog = play_cog

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self.play_cog if name == "PlayWithCog" else None


class _DeferredSetupResponse:
    def __init__(self, events):
        self.events = events

    async def defer(self, **kwargs):
        self.events.append(("defer", kwargs))

    async def send_message(self, *args, **kwargs):
        self.events.append(("response", args, kwargs))


class _DeferredSetupFollowup:
    def __init__(self, events):
        self.events = events

    async def send(self, *args, **kwargs):
        self.events.append(("followup", args, kwargs))


class GuildSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_completion_ensures_party_panels(self):
        calls = []

        async def ensure_panels(guild):
            calls.append(guild)

        play_cog = SimpleNamespace(ensure_panels=ensure_panels)
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            guild = _SetupGuild()
            cog = GuildSettingsCog(_SetupBot(play_cog), settings)
            await cog._ensure_bot_channels(guild)

        self.assertEqual(calls, [guild])

    async def test_deleted_party_channel_is_recreated_with_panels_on_next_setup(self):
        calls = []

        async def ensure_panels(guild):
            calls.append(guild)

        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            guild = _SetupGuild()
            old_party = _SetupChannel(30, "old-party")
            music = _SetupChannel(31, "music")
            guild.channels = {30: old_party, 31: music}
            settings.set_party_channel(guild.id, 30)
            settings.set_music_channel(guild.id, 31)
            cog = GuildSettingsCog(
                _SetupBot(SimpleNamespace(ensure_panels=ensure_panels)), settings
            )

            await cog.on_guild_channel_delete(
                SimpleNamespace(id=30, guild=guild)
            )
            guild.channels.pop(30)
            party, returned_music = await cog._ensure_bot_channels(guild)

        self.assertIsNone(guild.get_channel(30))
        self.assertIs(returned_music, music)
        self.assertNotEqual(party.id, 30)
        self.assertEqual(calls, [guild])

    async def test_setup_creates_private_channels_once_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            bot = _SetupBot()
            cog = GuildSettingsCog(bot, settings)
            guild = _SetupGuild()

            first = await cog.ensure_bot_channels(guild)
            second = await cog.ensure_bot_channels(guild)
            concurrent_guild = _SetupGuild(1_000)
            concurrent = await asyncio.gather(
                cog._ensure_bot_channels(concurrent_guild),
                cog._ensure_bot_channels(concurrent_guild),
            )

        self.assertEqual([category.name for category in guild.created_categories], ["🤖 봇"])
        self.assertEqual(
            [channel.name for channel, _ in guild.created_channels], ["🎮-파티", "🎵-음악"]
        )
        self.assertEqual(first, second)
        category = guild.created_categories[0]
        self.assertIs(category.overwrites[guild.default_role].send_messages, False)
        bot_permissions = category.overwrites[guild.me]
        self.assertTrue(bot_permissions.view_channel)
        self.assertTrue(bot_permissions.send_messages)
        self.assertTrue(bot_permissions.read_message_history)
        self.assertTrue(bot_permissions.embed_links)
        self.assertTrue(bot_permissions.attach_files)
        self.assertEqual(len(bot.views), 1)
        self.assertIsInstance(bot.views[0], SetupView)
        self.assertTrue(bot.views[0].is_persistent())
        self.assertEqual(bot.views[0].children[0].custom_id, "setup:start")
        self.assertEqual(len(concurrent_guild.created_categories), 1)
        self.assertEqual(len(concurrent_guild.created_channels), 2)
        self.assertEqual(concurrent[0], concurrent[1])

    async def test_setup_reuses_stored_live_channels_without_renaming_them(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            cog = GuildSettingsCog(_SetupBot(), settings)
            guild = _SetupGuild()
            party = _SetupChannel(30, "party-custom-name")
            music = _SetupChannel(31, "music-custom-name")
            guild.channels = {party.id: party, music.id: music}
            settings.set_party_channel(guild.id, party.id)
            settings.set_music_channel(guild.id, music.id)

            result = await cog.ensure_bot_channels(guild)

        self.assertEqual(result, (party, music))
        self.assertEqual((party.name, music.name), ("party-custom-name", "music-custom-name"))
        self.assertEqual(guild.created_categories, [])
        self.assertEqual(guild.created_channels, [])

    async def test_join_notice_requires_a_sendable_system_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            cog = GuildSettingsCog(
                _SetupBot(), SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            )
            guild = _SetupGuild()
            await cog.on_guild_join(guild)

            sent = []

            class SystemChannel:
                def __init__(self, send_messages):
                    self.send_messages = send_messages

                def permissions_for(self, member):
                    return SimpleNamespace(send_messages=self.send_messages)

                async def send(self, *args, **kwargs):
                    sent.append((args, kwargs))

            guild.system_channel = SystemChannel(False)
            await cog.on_guild_join(guild)
            guild.system_channel = SystemChannel(True)
            await cog.on_guild_join(guild)

        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0][1]["view"], cog.setup_view)

    async def test_setup_button_rejects_users_without_manage_guild(self):
        with tempfile.TemporaryDirectory() as directory:
            cog = GuildSettingsCog(
                _SetupBot(), SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            )
            interaction = SimpleNamespace(
                guild=_SetupGuild(),
                user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
                response=RecordingResponse(),
            )
            await cog.setup_view.children[0].callback(interaction)

        self.assertTrue(interaction.response.messages[0][1]["ephemeral"])
        self.assertIn("관리 권한", interaction.response.messages[0][0][0])

    async def test_authorized_setup_defers_then_uses_followup_for_button_and_slash(self):
        with tempfile.TemporaryDirectory() as directory:
            cog = GuildSettingsCog(
                _SetupBot(), SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            )
            guild = _SetupGuild()
            manager = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True))
            button_events = []
            button = SimpleNamespace(
                guild=guild,
                user=manager,
                response=_DeferredSetupResponse(button_events),
                followup=_DeferredSetupFollowup(button_events),
            )
            await cog.setup_view.children[0].callback(button)

            slash_events = []
            slash = SimpleNamespace(
                guild=guild,
                response=_DeferredSetupResponse(slash_events),
                followup=_DeferredSetupFollowup(slash_events),
            )
            await GuildSettingsCog._start.callback(cog, slash)

        self.assertEqual(
            button_events[0], ("defer", {"ephemeral": True, "thinking": True})
        )
        self.assertEqual(button_events[1][0], "followup")
        self.assertTrue(button_events[1][2]["ephemeral"])
        self.assertEqual(slash_events[0], ("defer", {"ephemeral": True}))
        self.assertEqual(slash_events[1][0], "followup")
        self.assertTrue(slash_events[1][2]["ephemeral"])




class RankingCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        repository = SQLiteAttendanceRepository(
            pathlib.Path(self.temp_dir.name) / "attendance.db"
        )
        repository.add_points(TEST_GUILD_ID, 1, 500)
        repository.add_points(TEST_GUILD_ID, 2, 400)
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
        before = FakeMessage("안녕하세요", guild_id=TEST_GUILD_ID)
        after = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(self.counter.counts, [FakeUser.id])
        self.assertIn("나쁜말", after.sent[0])

    async def test_edit_of_an_already_caught_message_is_not_double_counted(self):
        before = FakeMessage("나쁜말 하나", guild_id=TEST_GUILD_ID)
        after = FakeMessage("나쁜말 둘", guild_id=TEST_GUILD_ID)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(self.counter.counts, [])
        self.assertEqual(after.sent, [])

    async def test_unchanged_content_is_not_rescreened(self):
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)

        await self.cog.on_message_edit(message, message)

        self.assertEqual(self.counter.counts, [])

    async def test_clean_edit_stays_clean(self):
        before = FakeMessage("안녕", guild_id=TEST_GUILD_ID)
        after = FakeMessage("반가워요", guild_id=TEST_GUILD_ID)

        await self.cog.on_message_edit(before, after)

        self.assertEqual(self.counter.counts, [])
        self.assertEqual(after.sent, [])


class PartyCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_party_rejects_further_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = "PUBG"  # 역할 없는 게임
            repository.create_party(TEST_GUILD_ID, game, 1_000)
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            for user_id in range(playwith_cog.GAMES[game]["max_players"]):
                self.assertTrue(await cog.add_participant(TEST_GUILD_ID, game, user_id))

            self.assertFalse(await cog.add_participant(TEST_GUILD_ID, game, 999))
            self.assertIsNone(repository.get_user_party(TEST_GUILD_ID, 999))


class PartyMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_leaving_member_frees_party_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(TEST_GUILD_ID, game, 1_000)
            repository.add_participant(TEST_GUILD_ID, game, 42, "탑")
            repository.add_participant(TEST_GUILD_ID, game, 43, "미드")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            member = SimpleNamespace(
                id=42, guild=SimpleNamespace(id=TEST_GUILD_ID)
            )
            await cog.on_member_remove(member)

            self.assertIsNone(repository.get_user_party(TEST_GUILD_ID, 42))
            self.assertEqual(repository.get_participants(TEST_GUILD_ID, game), {43: "미드"})
            self.assertIsNotNone(repository.get_party(TEST_GUILD_ID, game))

    async def test_last_leaving_member_disbands_the_party(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(playwith_cog.GAMES))
            repository.create_party(TEST_GUILD_ID, game, 1_000)
            repository.add_participant(TEST_GUILD_ID, game, 42, "탑")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PlayWithCog(bot=None, repository=repository)

            await cog.on_member_remove(
                SimpleNamespace(id=42, guild=SimpleNamespace(id=TEST_GUILD_ID))
            )

            self.assertIsNone(repository.get_party(TEST_GUILD_ID, game))


class BackupConnectionTests(unittest.TestCase):
    def test_backup_database_connections_are_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "party.db"
            temporary = pathlib.Path(directory) / "backup.db"
            SQLitePartyRepository(source)

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                backup._backup_one(source, temporary)
                backup.verify_database(
                    temporary,
                    {"parties", "participants"},
                    source_name="party_data.db",
                )
                gc.collect()

        self.assertFalse(
            [warning for warning in caught if issubclass(warning.category, ResourceWarning)]
        )


class SettingsLoaderTest(unittest.TestCase):
    def _with_settings_dir(self, directory):
        return patch.object(config, "SETTINGS_DIR", pathlib.Path(directory))

    def test_reads_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "a.json").write_text('{"k": 1}', encoding="utf-8")
            with self._with_settings_dir(directory):
                self.assertEqual(
                    config.load_settings_json("a.json", "b.json", default={}), {"k": 1}
                )

    def test_falls_back_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "b.json").write_text('{"k": 2}', encoding="utf-8")
            with self._with_settings_dir(directory):
                self.assertEqual(
                    config.load_settings_json("a.json", "b.json", default={}), {"k": 2}
                )

    def test_returns_default_when_all_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._with_settings_dir(directory):
                self.assertEqual(config.load_settings_json("a.json", default=[]), [])

    def test_broken_json_falls_back_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "a.json").write_text("{not json", encoding="utf-8")
            with self._with_settings_dir(directory):
                self.assertEqual(config.load_settings_json("a.json", default={}), {})

    def test_non_utf8_json_falls_back_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "a.json").write_bytes(b"\xff")
            with self._with_settings_dir(directory):
                self.assertEqual(config.load_settings_json("a.json", default={}), {})

    def test_integer_beyond_digit_limit_falls_back_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "a.json").write_text(
                "1" * 5_000, encoding="utf-8"
            )
            with self._with_settings_dir(directory):
                self.assertEqual(config.load_settings_json("a.json", default={}), {})

    def test_excessive_nesting_falls_back_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "a.json").write_text(
                "[" * 100_000 + "0" + "]" * 100_000, encoding="utf-8"
            )
            with self._with_settings_dir(directory):
                self.assertEqual(config.load_settings_json("a.json", default={}), {})

    def test_process_control_exceptions_still_escape(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "module.config.json.load", side_effect=KeyboardInterrupt
        ):
            (pathlib.Path(directory) / "a.json").touch()
            with self._with_settings_dir(directory), self.assertRaises(KeyboardInterrupt):
                config.load_settings_json("a.json", default={})


class GamesExternalizationTest(unittest.TestCase):
    def test_games_load_from_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "games.json").write_text(
                json.dumps({"Test Game": {"max_players": 2, "roles": ["A", "B"]}}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                games = config.load_games()
        self.assertEqual(games["Test Game"]["max_players"], 2)

    def test_malformed_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "games.json").write_text(
                json.dumps({"Good": {"max_players": 2, "roles": []}, "Bad": "문자열"}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                games = config.load_games()
        self.assertIn("Good", games)
        self.assertNotIn("Bad", games)

    def test_discord_component_bounds_are_enforced(self):
        roster = {
            "": {"max_players": 2, "roles": []},
            "G" * 90: {"max_players": 2, "roles": []},
            "Too Many Roles": {
                "max_players": 2,
                "roles": [f"Role {index}" for index in range(26)],
            },
            "Empty Role": {"max_players": 2, "roles": [""]},
            "Long Role": {"max_players": 2, "roles": ["R" * 101]},
            "G" * 89: {"max_players": 2, "roles": ["R" * 100]},
        }
        roster.update(
            {f"Game {index}": {"max_players": 2, "roles": []} for index in range(25)}
        )
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "games.json").write_text(
                json.dumps(roster), encoding="utf-8"
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                games = config.load_games()

        self.assertEqual(
            list(games), ["G" * 89, *(f"Game {index}" for index in range(25))]
        )

    def test_boolean_max_players_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "games.json").write_text(
                json.dumps({"Boolean Capacity": {"max_players": True, "roles": []}}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                games = config.load_games()

        self.assertNotIn("Boolean Capacity", games)

    def test_largest_roster_and_aggregate_role_bounds_are_enforced(self):
        roles = [f"{index:02d}" + "r" * 37 for index in range(25)]
        roster = {
            "Largest": {"max_players": 25, "roles": roles},
            "Too Many Players": {"max_players": 26, "roles": []},
            "Too Much Role Text": {
                "max_players": 25,
                "roles": [*roles[:-1], roles[-1] + "xx"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "games.json").write_text(
                json.dumps(roster), encoding="utf-8"
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                games = config.load_games()

        self.assertEqual(games, {"Largest": roster["Largest"]})


class PersonaExternalizationTest(unittest.TestCase):
    def setUp(self):
        self.openai_key = patch("module.hyacine_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)

    def test_persona_comes_from_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"system_prompt": "테스트 프롬프트", "greeting": "테스트 인사"}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = HyacineChatCog(bot=None)
        self.assertEqual(cog.system_prompt, "테스트 프롬프트")
        self.assertEqual(cog.greeting, "테스트 인사")

    def test_missing_persona_keys_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"system_prompt": "프롬프트만"}), encoding="utf-8"
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = HyacineChatCog(bot=None)
        self.assertEqual(cog.system_prompt, "프롬프트만")
        self.assertTrue(cog.greeting)

    def test_missing_system_prompt_keeps_hyacine_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"greeting": "테스트 인사"}), encoding="utf-8"
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = HyacineChatCog(bot=None)

        self.assertIn("히아킨", cog.system_prompt)
        self.assertIn("회색둥이 씨", cog.system_prompt)
        self.assertEqual(cog.greeting, "테스트 인사")

    def test_constructors_take_no_nickname(self):
        with self.assertRaises(TypeError):
            HyacineChatCog(bot=None, nickname="회색")
        with self.assertRaises(TypeError):
            HyacineImageCog(bot=None, nickname="회색")


class PersonaSessionRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_refreshes_persona_without_changing_old_session(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "module.hyacine_chat_cog.OPENAI_API_KEY", "sk-test-dummy"
        ):
            persona_path = pathlib.Path(directory) / "persona.json"
            persona_path.write_text(
                json.dumps({"system_prompt": "old prompt", "greeting": "old greeting"}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                attendance = RecordingAttendance()
                cog = HyacineChatCog(
                    bot=SimpleNamespace(get_cog=lambda _: attendance)
                )
                old_session = cog.get_session(1)
                persona_path.write_text(
                    json.dumps({"system_prompt": "new prompt", "greeting": "new greeting"}),
                    encoding="utf-8",
                )
                self.assertEqual(cog.greeting, "old greeting")
                new_session = cog.get_session(2)

                captured_instructions = []

                async def response(**kwargs):
                    captured_instructions.append(kwargs["instructions"])
                    return SimpleNamespace(
                        output_text="reply",
                        model="test-model",
                        usage=SimpleNamespace(
                            input_tokens=1, output_tokens=1, total_tokens=2
                        ),
                    )

                cog.client = SimpleNamespace(
                    responses=SimpleNamespace(create=response)
                )
                await cog._run_talk(
                    FakeInteraction(channel_id=1), "old", None, "test-model", "none", 0,
                    "light", config.LIMIT_LIGHT,
                )
                await cog._run_talk(
                    FakeInteraction(channel_id=2), "new", None, "test-model", "none", 0,
                    "light", config.LIMIT_LIGHT,
                )
                old_hello = FakeInteraction(channel_id=1)
                new_hello = FakeInteraction(channel_id=2)
                await HyacineChatCog._hello.callback(cog, old_hello)
                await HyacineChatCog._hello.callback(cog, new_hello)

        self.assertEqual(old_session.system_prompt, "old prompt")
        self.assertEqual(new_session.system_prompt, "new prompt")
        self.assertEqual(old_session.greeting, "old greeting")
        self.assertEqual(new_session.greeting, "new greeting")
        self.assertEqual(cog.greeting, "new greeting")
        self.assertEqual(captured_instructions, ["old prompt", "new prompt"])
        self.assertIn("old greeting", old_hello.response.messages[0][0][0])
        self.assertIn("new greeting", new_hello.response.messages[0][0][0])
