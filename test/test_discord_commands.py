import asyncio
import contextlib
import dataclasses
import gc
import hashlib
import inspect
import json
import os
import pathlib
import re
import secrets
import stat
import tempfile
import time
import unittest
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import discord
import httpx
import openai
from aiohttp import CookieJar, FormData
from aiohttp.test_utils import TestClient, TestServer
from discord.ext import commands

import module.ai_chat_cog as ai_chat_cog
import module.ai_image_cog as ai_image_cog
import module.backup as backup
import module.config as config
import module.database as database_module
import module.enka_profiles as enka_profiles
import module.export_legacy as export_legacy
import module.forbidden_filter_cog as forbidden_filter_cog
import module.game_profile_cog as game_profile_cog
import module.greeting_cog as greeting_cog
import module.i18n as i18n
import module.main as bot_main
import module.member_profile_cog as member_profile_cog
import module.panel as panel_module
import module.party_cog as party_cog
import module.web_admin_cog as web_admin_cog
from module.ai_chat_cog import AIChatCog
from module.ai_image_cog import AIImageCog
from module.database import (
    SQLiteGameUidRepository,
    SQLiteGuildSettingsRepository,
    SQLitePartyRepository,
    SQLiteUsageRepository,
)
from module.guild_settings_cog import GuildSettingsCog, SetupView
from module.member_profile_cog import MemberProfileCog
from module.panel import drop_panel_locks, panel_lock, upsert_panel
from module.party_cog import PartyCog
from module.scheduled_event_cog import ScheduledEventCog
from module.usage_cog import KST_TIMEZONE, UsageCog

# 단일 운영 길드를 전제하던 상수가 사라졌다. 테스트는 임의의 길드 하나를 쓴다.
TEST_GUILD_ID = 4_242

_PARTY_TEST_GAMES = {
    "League of Legends": {"max_players": 5, "roles": ["탑", "정글", "미드", "원딜", "서포터"]},
    "PUBG": {"max_players": 4, "roles": []},
    "Overwatch": {"max_players": 5, "roles": ["딜러1", "딜러2", "탱커", "힐러1", "힐러2"]},
}


def setUpModule():
    games_patch = patch.object(party_cog, "GAMES", _PARTY_TEST_GAMES)
    games_patch.start()
    unittest.addModuleCleanup(games_patch.stop)


class MinimalConfigTest(unittest.TestCase):
    def test_only_discord_token_is_required(self):
        with patch.object(config, "DISCORD_TOKEN", "t"), \
             patch.object(config, "OPENAI_API_KEY", None), \
             patch.object(config, "GOOGLE_API_KEY", None), \
             patch.object(config, "ADMIN_TOKEN", None):
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
        self.assertGreater(config.AI_USAGE_RETENTION_DAYS, 0)

    def test_short_admin_token_is_rejected(self):
        with patch.object(config, "DISCORD_TOKEN", "t"), patch.object(
            config, "ADMIN_TOKEN", "short"
        ), self.assertRaisesRegex(RuntimeError, "ADMIN_TOKEN"):
            config.validate_config()


class LocalDataSecurityTests(unittest.TestCase):
    def test_environment_files_fail_closed_on_unsafe_mode_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            secrets_file = root / ".env.secrets"
            runtime_file = root / ".env.runtime"
            secrets_file.write_text("DISCORD_TOKEN=dummy\n", encoding="utf-8")
            runtime_file.write_text("DATA_DIR=runtime/data\n", encoding="utf-8")
            secrets_file.chmod(0o644)
            runtime_file.chmod(0o600)
            with self.assertRaises(PermissionError) as rejected:
                config._load_env_files(root)
            self.assertNotIn("dummy", str(rejected.exception))

            secrets_file.chmod(0o600)
            with patch.object(config, "load_dotenv") as loaded:
                config._load_env_files(root)
            self.assertEqual(loaded.call_count, 2)

            runtime_file.unlink()
            runtime_file.symlink_to(secrets_file)
            with self.assertRaises(PermissionError):
                config._load_env_files(root)

    def test_sqlite_database_and_sidecars_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            data_directory = pathlib.Path(directory) / "data"
            data_directory.mkdir(mode=0o755)
            database_path = data_directory / "usage.db"
            repository = SQLiteUsageRepository(database_path)
            repository.increment_forbidden_count(1, 2)
            database_path.chmod(0o644)

            SQLiteUsageRepository(database_path)

            self.assertEqual(stat.S_IMODE(data_directory.stat().st_mode), 0o700)
            for path in (
                database_path,
                database_path.with_name("usage.db-wal"),
                database_path.with_name("usage.db-shm"),
            ):
                if path.exists():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_legacy_export_is_exclusive_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            destination = root / "legacy.json"
            export_legacy.write_export(destination, data_dir=root / "missing")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                export_legacy.write_export(destination, data_dir=root / "missing")

    def test_ai_usage_prunes_expired_rows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database_module, "AI_USAGE_RETENTION_DAYS", 30
        ):
            repository = SQLiteUsageRepository(
                pathlib.Path(directory) / "usage.db"
            )
            repository.consume_ai_usage(1, "2026-06-01", "light", 3)
            repository.consume_ai_usage(1, "2026-08-01", "light", 3)

            self.assertEqual(
                repository.get_ai_usage(1, "2026-06-01", "light"), 0
            )
            self.assertEqual(
                repository.get_ai_usage(1, "2026-08-01", "light"), 1
            )


class MemberDataLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_departure_deletes_guild_scoped_personal_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            usage_repository = SQLiteUsageRepository(root / "usage.db")
            game_uid_repository = SQLiteGameUidRepository(root / "profile.db")
            usage_repository.increment_forbidden_count(7, 8)
            game_uid_repository.set_uid(7, 8, "genshin", "123456789")
            member = SimpleNamespace(guild=SimpleNamespace(id=7), id=8)

            await UsageCog(None, usage_repository).on_member_remove(member)
            await game_profile_cog.GameProfileCog(
                None,
                game_uid_repository,
                showcase_service=SimpleNamespace(),
            ).on_member_remove(member)

            self.assertEqual(usage_repository.get_forbidden_count(7, 8), 0)
            self.assertEqual(game_uid_repository.list_uids(7, 8), {})

class ConditionalExtensionTest(unittest.TestCase):
    def test_all_extensions_load_when_every_key_present(self):
        with patch.object(
            bot_main,
            "OPTIONAL_DEPENDENCY_VALUES",
            {"OPENAI_API_KEY": "a", "GOOGLE_API_KEY": "b"},
        ):
            names = bot_main.available_extensions()
        self.assertIn("module.ai_chat_cog", names)
        self.assertIn("module.ai_image_cog", names)

    def test_ai_extensions_skipped_without_keys(self):
        with patch.object(
            bot_main,
            "OPTIONAL_DEPENDENCY_VALUES",
            {"OPENAI_API_KEY": None, "GOOGLE_API_KEY": None},
        ):
            names = bot_main.available_extensions()
        self.assertNotIn("module.ai_chat_cog", names)
        self.assertNotIn("module.ai_image_cog", names)

    def test_core_extensions_survive_with_no_optional_keys(self):
        with patch.object(
            bot_main,
            "OPTIONAL_DEPENDENCY_VALUES",
            {"OPENAI_API_KEY": None, "GOOGLE_API_KEY": None},
        ):
            names = bot_main.available_extensions()
        for required in (
            "module.guild_settings_cog",
            "module.party_cog",
            "module.forbidden_filter_cog",
            "module.usage_cog",
        ):
            self.assertIn(required, names)


class RecordingResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False
        self.defer_kwargs = []
        self.modal = None

    async def send_message(self, *args, **kwargs):
        if self.messages:
            raise RuntimeError("interaction already has an initial response")
        self.messages.append((args, kwargs))

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs.append(kwargs)

    async def send_modal(self, modal):
        self.modal = modal


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


class _EventSettingsRepository:
    def __init__(self, event_channel_id=None):
        self.event_channel_id = event_channel_id

    def get_event_channel(self, guild_id):
        return self.event_channel_id


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


class RecordingUsage:
    def __init__(self, reserve_result=("2026-08-04", 1), usage=None):
        self.reservations = []
        self.releases = []
        self.reserve_result = reserve_result
        self.usage = usage or {}

    async def reserve_ai_usage(self, user_id, usage_category, daily_limit):
        self.reservations.append((user_id, usage_category, daily_limit))
        return self.reserve_result

    async def release_ai_usage(self, user_id, usage_date, usage_category):
        self.releases.append((user_id, usage_date, usage_category))
        return True

    async def get_ai_usage(self, user_id, usage_category):
        return self.usage.get(usage_category, 0)


class ImageCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.google_key = patch("module.ai_image_cog.GOOGLE_API_KEY", "test-dummy")
        self.google_key.start()
        self.addCleanup(self.google_key.stop)

    async def test_temporary_image_directory_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            ai_image_cog, "DATA_DIR", pathlib.Path(directory) / "data"
        ):
            cog = AIImageCog(SimpleNamespace(get_cog=lambda _: None))
            self.assertEqual(
                stat.S_IMODE(cog.temporary_image_directory.stat().st_mode),
                0o700,
            )

    async def test_daily_limit_stops_before_the_provider_call(self):
        usage_cog = RecordingUsage(reserve_result=None)
        interaction = FakeInteraction(channel_id=1)
        cog = AIImageCog(SimpleNamespace(get_cog=lambda _: usage_cog))
        provider_calls = []
        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kwargs: provider_calls.append(kwargs)
            )
        )

        await AIImageCog._generate_image.callback(cog, interaction, "test")

        self.assertEqual(
            usage_cog.reservations,
            [(123, "image", config.LIMIT_IMAGE)],
        )
        self.assertEqual(provider_calls, [])
        self.assertIn("오늘 사용 횟수", interaction.response.messages[-1][0][0])

    async def test_defer_failure_releases_the_reserved_slot(self):
        usage_cog = RecordingUsage()
        interaction = FakeInteraction(channel_id=1)
        cog = AIImageCog(SimpleNamespace(get_cog=lambda _: usage_cog))

        async def fail_defer():
            raise RuntimeError("defer transport failed")

        interaction.response.defer = fail_defer
        with patch("module.ai_image_cog.print"), patch(
            "module.ai_image_cog.traceback.print_exc"
        ):
            await AIImageCog._generate_image.callback(cog, interaction, "test")

        self.assertEqual(usage_cog.releases, [(123, "2026-08-04", "image")])

    async def test_gemini_quota_error_has_actionable_billing_guidance(self):
        usage_cog = RecordingUsage()
        interaction = FakeInteraction(channel_id=1)
        cog = AIImageCog(SimpleNamespace(get_cog=lambda _: usage_cog))
        quota_error = ai_image_cog.genai_errors.ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "quota exhausted",
                }
            },
        )

        def generate_content(**_):
            raise quota_error

        cog.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content)
        )
        with patch("module.ai_image_cog.print") as printed, patch(
            "module.ai_image_cog.traceback.print_exc"
        ) as print_traceback:
            await AIImageCog._generate_image.callback(cog, interaction, "test")

        message = interaction.followup.messages[-1][0][0]
        self.assertIn("요청 할당량 또는 결제 한도", message)
        self.assertIn("Google AI Studio", message)
        self.assertEqual(usage_cog.releases, [(123, "2026-08-04", "image")])
        self.assertIn(
            "quota/billing",
            " ".join(str(call.args[0]) for call in printed.call_args_list),
        )
        print_traceback.assert_not_called()

    async def test_empty_provider_response_log_does_not_reflect_response_or_prompt(self):
        usage_cog = RecordingUsage()
        interaction = FakeInteraction(channel_id=1)
        cog = AIImageCog(SimpleNamespace(get_cog=lambda _: usage_cog))

        class SensitiveResponse:
            parts = []
            candidates = [SimpleNamespace(finish_reason="SAFETY")]

            def __str__(self):
                return "response-secret-marker"

        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_: SensitiveResponse()
            )
        )
        with patch("module.ai_image_cog.print") as printed:
            await AIImageCog._generate_image.callback(
                cog, interaction, "prompt-secret-marker"
            )

        log_text = " ".join(
            " ".join(str(argument) for argument in call.args)
            for call in printed.call_args_list
        )
        self.assertIn("SAFETY", log_text)
        self.assertNotIn("response-secret-marker", log_text)
        self.assertNotIn("prompt-secret-marker", log_text)

    async def test_long_prompt_is_truncated_in_embed(self):
        usage_cog = RecordingUsage()
        interaction = FakeInteraction(channel_id=1)
        cog = AIImageCog(SimpleNamespace(get_cog=lambda _: usage_cog))
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        cog.temporary_image_directory = pathlib.Path(temporary_directory.name)
        cog.bot = SimpleNamespace(
            get_cog=lambda _: usage_cog,
            loop=SimpleNamespace(create_task=lambda coro: coro.close()),
        )
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))
        captured = {}

        def generate(**kwargs):
            captured["contents"] = kwargs["contents"]
            return SimpleNamespace(parts=[part])

        cog.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        prompt = "가" * 5_000

        await AIImageCog._generate_image.callback(cog, interaction, prompt)

        description = interaction.followup.messages[0][1]["embed"].description
        self.assertLessEqual(len(description), 1_100)
        self.assertTrue(description.endswith("…"))
        # 모델에는 원본이 그대로 간다.
        self.assertEqual(captured["contents"], [prompt])
        image_path = next(pathlib.Path(temporary_directory.name).iterdir())
        self.assertEqual(stat.S_IMODE(image_path.stat().st_mode), 0o600)

    async def test_failed_discord_upload_deletes_temporary_image_immediately(self):
        usage_cog = RecordingUsage()
        interaction = FakeInteraction(channel_id=1)
        interaction.followup = RecordingFollowup(fail_on_call=1)
        cog = AIImageCog(SimpleNamespace(get_cog=lambda _: usage_cog))
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        cog.temporary_image_directory = pathlib.Path(temporary_directory.name)
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"png"))
        cog.client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_: SimpleNamespace(parts=[part])
            )
        )

        with patch("module.ai_image_cog.print"), patch(
            "module.ai_image_cog.traceback.print_exc"
        ):
            await AIImageCog._generate_image.callback(cog, interaction, "test")

        self.assertEqual(
            list(pathlib.Path(temporary_directory.name).iterdir()), []
        )


class DisappearingUsageBot:
    def __init__(self, usage_cog):
        self.usage_cog = usage_cog
        self.calls = 0

    def get_cog(self, name):
        # 이름을 확인한다. 무시하면 UsageCog 개명 시 이 목이 조용히 통과한다.
        if name != UsageCog.__name__:
            return None
        self.calls += 1
        return self.usage_cog if self.calls == 1 else None


class ChatCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.ai_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)
        self.usage_cog = RecordingUsage()
        self.cog = AIChatCog(
            bot=SimpleNamespace(get_cog=lambda _: self.usage_cog)
        )

    def test_chat_command_set_replaces_switching_commands(self):
        names = {command.name for command in self.cog.get_app_commands()}

        self.assertTrue({"기본대화", "고급대화"}.issubset(names))
        self.assertFalse({"대화", "기본", "고급"} & names)

    async def test_basic_and_advanced_commands_route_exact_model_settings(self):
        calls = []

        async def record_run_chat(*args):
            calls.append(args)

        self.cog._run_chat = record_run_chat
        basic = FakeInteraction(channel_id=1)
        advanced = FakeInteraction(channel_id=1)

        await AIChatCog._light_chat.callback(self.cog, basic, "기본 대화")
        await AIChatCog._deep_chat.callback(self.cog, advanced, "고급 대화")

        self.assertEqual(
            calls,
            [
                (basic, "기본 대화", None, "gpt-5.6-terra", "none", "light", config.LIMIT_LIGHT),
                (advanced, "고급 대화", None, "gpt-5.6-sol", "medium", "deep", config.LIMIT_DEEP),
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

            def get_ai_usage(self, user_id, usage_date, usage_category):
                self.calls.append((user_id, usage_date, usage_category))
                return self.counts[usage_category]

        repository = RecordingUsageRepository()
        usage_cog = UsageCog(bot=None, usage_repository=repository)
        self.cog.bot = SimpleNamespace(get_cog=lambda _: usage_cog)
        interaction = FakeInteraction(channel_id=1)
        self.cog.get_or_create_session(1).last_usage = {
            "model": "gpt-5.6-sol",
            "input_tokens": 12,
            "output_tokens": 34,
            "total_tokens": 46,
        }

        with patch("module.usage_cog.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026, 8, 5, 0, 1, tzinfo=KST_TIMEZONE
            )
            await AIChatCog._status.callback(self.cog, interaction)

        message = interaction.response.messages[-1][0][0]
        self.assertIs(interaction.response.messages[-1][1].get("ephemeral"), True)
        self.assertIn("사용자별 · 봇 인스턴스 전체", message)
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
        self.assertEqual(
            mocked_datetime.now.call_args_list, [call(KST_TIMEZONE)] * 3
        )

    async def test_status_preserves_models_when_usage_cog_is_unavailable(self):
        self.cog.bot = None
        interaction = FakeInteraction(channel_id=1)
        self.cog.get_or_create_session(1).last_usage = {
            "model": "gpt-5.6-sol",
            "total_tokens": 46,
        }

        await AIChatCog._status.callback(self.cog, interaction)

        message = interaction.response.messages[-1][0][0]
        self.assertIn("gpt-5.6-terra", message)
        self.assertIn("gpt-5.6-sol", message)
        self.assertIn("46", message)
        self.assertIn("확인할 수 없어요", message)

    async def test_daily_limit_stops_before_the_provider_call(self):
        usage_cog = RecordingUsage(reserve_result=None)
        self.cog.bot = SimpleNamespace(get_cog=lambda _: usage_cog)
        interaction = FakeInteraction(channel_id=1)
        provider_calls = []

        async def provider(**kwargs):
            provider_calls.append(kwargs)

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=provider)
        )

        await self.cog._run_chat(
            interaction, "test", None, "gpt-5.6-terra", "none",
            "light", config.LIMIT_LIGHT,
        )

        self.assertEqual(provider_calls, [])
        self.assertIn("오늘 사용 횟수", interaction.response.messages[-1][0][0])

    async def test_pre_api_failure_releases_the_reserved_slot(self):
        """API 호출 전에 실패하면 일일 한도를 돌려준다."""
        usage_cog = RecordingUsage()
        self.cog.bot = SimpleNamespace(get_cog=lambda _: usage_cog)
        interaction = FakeInteraction(channel_id=1)

        async def fail_defer():
            raise RuntimeError("defer transport failed")

        interaction.response.defer = fail_defer
        with patch("module.ai_chat_cog.print"), patch(
            "module.ai_chat_cog.traceback.print_exc"
        ):
            await self.cog._run_chat(
                interaction, "test", None, "gpt-5.6-terra", "none",
                "light", config.LIMIT_LIGHT,
            )

        self.assertEqual(usage_cog.releases, [(123, "2026-08-04", "light")])

    async def test_exhausted_openai_credit_has_actionable_message_without_traceback(self):
        interaction = FakeInteraction(channel_id=1)
        error = openai.RateLimitError(
            "quota exhausted",
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            ),
            body={
                "error": {
                    "type": "insufficient_quota",
                    "code": "credit_balance_exhausted",
                }
            },
        )

        async def response(**kwargs):
            raise error

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=response)
        )
        with patch("module.ai_chat_cog.traceback.print_exc") as print_exc, patch(
            "module.ai_chat_cog.logger.warning"
        ) as warning:
            await self.cog._run_chat(
                interaction, "test", None, "gpt-5.6-terra", "none",
                "light", config.LIMIT_LIGHT,
            )

        message = interaction.followup.messages[-1][0][0]
        self.assertIn("OpenAI API 크레딧", message)
        self.assertNotIn("/이미지", message)
        self.assertNotIn("Gemini API", message)
        print_exc.assert_not_called()
        warning.assert_called_once()

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
        await self.cog._run_chat(
            interaction, "", attachment, "gpt-5.6-terra", "none",
            "light", config.LIMIT_LIGHT,
        )

        self.assertIn(attachment.url, repr(captured["input"]))
        self.assertNotIn(
            attachment.url, repr(list(self.cog.get_or_create_session(1).history))
        )

    def test_splitter_never_exceeds_discord_limit_around_code_fences(self):
        text = "```\n" + "a" * 1_993 + "\n" + "bbbb\n"

        chunks = self.cog._split_for_discord(text)

        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 2_000 for chunk in chunks))
        self.assertIn("bbbb", "".join(chunks))

        tiny_chunks = self.cog._split_for_discord("```\n" + "a" * 20, limit=8)
        self.assertTrue(all(len(chunk) <= 8 for chunk in tiny_chunks))

    async def test_long_user_echo_is_chunked_before_sending(self):
        interaction = FakeInteraction(channel_id=1)

        async def response(**kwargs):
            return SimpleNamespace(
                output_text="응답",
                model="gpt-5.6-terra",
                usage=SimpleNamespace(
                    input_tokens=1, output_tokens=1, total_tokens=2
                ),
            )

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=response)
        )
        await self.cog._run_chat(
            interaction, "가" * 2_500, None, "gpt-5.6-terra", "none",
            "light", config.LIMIT_LIGHT,
        )

        sent = [args[0] for args, _ in interaction.followup.messages]
        self.assertTrue(all(len(message) <= 2_000 for message in sent))
        self.assertIn("응답", sent[-1])

    async def test_transport_failure_does_not_commit_invisible_history(self):
        interaction = FakeInteraction(channel_id=1)
        interaction.followup = RecordingFollowup(fail_on_call=1)

        async def response(**kwargs):
            return SimpleNamespace(
                output_text="provider reply",
                model="gpt-5.6-terra",
                usage=SimpleNamespace(
                    input_tokens=1, output_tokens=1, total_tokens=2
                ),
            )

        self.cog.client = SimpleNamespace(
            responses=SimpleNamespace(create=response)
        )
        with patch("module.ai_chat_cog.print"), patch(
            "module.ai_chat_cog.traceback.print_exc"
        ):
            await self.cog._run_chat(
                interaction, "user text", None, "gpt-5.6-terra", "none",
                "light", config.LIMIT_LIGHT,
            )

        session = self.cog.get_or_create_session(1)
        self.assertEqual(
            [item["role"] for item in session.history], ["system"]
        )

class ChatConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.ai_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)

    async def test_same_channel_calls_do_not_interleave_history(self):
        usage_cog = RecordingUsage()
        cog = AIChatCog(bot=SimpleNamespace(get_cog=lambda _: usage_cog))
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
            cog._run_chat(FakeInteraction(channel_id=1), "first", None, "gpt-5.6-terra", "none", "light", config.LIMIT_LIGHT),
            cog._run_chat(FakeInteraction(channel_id=1), "second", None, "gpt-5.6-terra", "none", "light", config.LIMIT_LIGHT),
        )

        roles = [m["role"] for m in cog.get_or_create_session(1).history if m["role"] != "system"]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    async def test_active_session_survives_eviction(self):
        cog = AIChatCog(bot=None)
        cog.MAX_CHANNEL_SESSIONS = 2
        active = cog.get_or_create_session(1)
        async with active.lock:
            for channel_id in range(2, 12):
                cog.get_or_create_session(channel_id)
            self.assertIs(cog.get_or_create_session(1), active)


class AICooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.openai_key = patch("module.ai_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)
        self.google_key = patch("module.ai_image_cog.GOOGLE_API_KEY", "test-dummy")
        self.google_key.start()
        self.addCleanup(self.google_key.stop)

    async def test_every_ai_command_carries_a_cooldown(self):
        chat = AIChatCog(bot=None)
        image = AIImageCog(bot=None)
        commands = {
            "기본대화": AIChatCog._light_chat,
            "고급대화": AIChatCog._deep_chat,
            "이미지": AIImageCog._generate_image,
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
                AIChatCog._light_chat,
                AIChatCog._deep_chat,
                AIImageCog._generate_image,
            )
        )
        for model_label in ("GPT-5.6", "Terra", "Sol", "Nano Banana"):
            self.assertNotIn(model_label, descriptions)

    async def test_cooldown_notice_is_ephemeral_and_charges_nothing(self):
        usage_cog = RecordingUsage()
        cog = AIChatCog(bot=DisappearingUsageBot(usage_cog))
        interaction = FakeInteraction(channel_id=1)
        error = discord.app_commands.CommandOnCooldown(
            discord.app_commands.Cooldown(1, 15), 7.4
        )

        await cog.cog_app_command_error(interaction, error)

        args, kwargs = interaction.response.messages[-1]
        self.assertIs(kwargs.get("ephemeral"), True)
        self.assertIn("7초", args[0])

class CommandPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = pathlib.Path(self.temporary_directory.name)
        self.usage = UsageCog(
            bot=None,
            usage_repository=SQLiteUsageRepository(root / "usage.db"),
        )
        self.member_profile = MemberProfileCog(
            bot=None, usage_repository=self.usage.usage_repository
        )
        self.party_repository = SQLitePartyRepository(root / "party.db")
        with patch("discord.ext.tasks.Loop.start"):
            self.play = PartyCog(
                bot=None, party_repository=self.party_repository
            )

    async def test_profile_success_is_ephemeral(self):
        interaction = FakeInteraction(channel_id=1)
        await MemberProfileCog._profile.callback(
            self.member_profile, interaction
        )
        self.assertIs(interaction.response.messages[-1][1].get("ephemeral"), True)

    async def test_profile_command_lives_only_in_member_profile_cog(self):
        # usage_cog는 한도·카운트 저장소다. 슬래시 명령이 남아 있으면 같은
        # /프로필이 두 번 등록되거나 파일 이름과 내용이 다시 어긋난다.
        self.assertEqual(list(self.usage.get_app_commands()), [])
        self.assertEqual(
            {c.name for c in self.member_profile.get_app_commands()},
            {"프로필", "프로필설정"},
        )

    async def test_bio_round_trip_and_removal(self):
        repository = self.member_profile.usage_repository
        interaction = FakeInteraction(channel_id=1)
        await MemberProfileCog._set_profile.callback(
            self.member_profile, interaction, "정원에서 기다립니다"
        )
        self.assertEqual(
            repository.get_bio(TEST_GUILD_ID, interaction.user.id),
            "정원에서 기다립니다",
        )

        profile = FakeInteraction(channel_id=1, user=interaction.user)
        await MemberProfileCog._profile.callback(self.member_profile, profile)
        embed = profile.response.messages[-1][1]["embed"]
        self.assertIn(
            "정원에서 기다립니다", [field.value for field in embed.fields]
        )

        # 빈 입력은 삭제다.
        await MemberProfileCog._set_profile.callback(
            self.member_profile, FakeInteraction(channel_id=1, user=interaction.user), ""
        )
        self.assertIsNone(repository.get_bio(TEST_GUILD_ID, interaction.user.id))

    async def test_profile_omits_bio_field_when_empty(self):
        interaction = FakeInteraction(channel_id=1)
        await MemberProfileCog._profile.callback(self.member_profile, interaction)
        embed = interaction.response.messages[-1][1]["embed"]
        self.assertNotIn("📝 자기소개", [field.name for field in embed.fields])

    async def test_bio_over_limit_is_rejected(self):
        repository = self.member_profile.usage_repository
        interaction = FakeInteraction(channel_id=1)
        await MemberProfileCog._set_profile.callback(
            self.member_profile, interaction, "가" * (member_profile_cog.BIO_MAX_LENGTH + 1)
        )
        self.assertIn("201자", interaction.response.messages[-1][0][0])
        self.assertIsNone(repository.get_bio(TEST_GUILD_ID, interaction.user.id))

        ok = FakeInteraction(channel_id=1, user=interaction.user)
        await MemberProfileCog._set_profile.callback(
            self.member_profile, ok, "가" * member_profile_cog.BIO_MAX_LENGTH
        )
        self.assertEqual(
            len(repository.get_bio(TEST_GUILD_ID, ok.user.id)),
            member_profile_cog.BIO_MAX_LENGTH,
        )

    async def test_bio_is_isolated_per_guild(self):
        repository = self.member_profile.usage_repository
        user = FakeUser()
        a = FakeInteraction(channel_id=1, guild_id=1111, user=user)
        await MemberProfileCog._set_profile.callback(self.member_profile, a, "A서버 소개")

        b = FakeInteraction(channel_id=1, guild_id=2222, user=user)
        await MemberProfileCog._profile.callback(self.member_profile, b)
        embed = b.response.messages[-1][1]["embed"]
        self.assertNotIn("📝 자기소개", [field.name for field in embed.fields])
        self.assertEqual(repository.get_bio(1111, user.id), "A서버 소개")

    async def test_bio_is_removed_when_member_leaves(self):
        repository = self.member_profile.usage_repository
        user = FakeUser()
        await MemberProfileCog._set_profile.callback(
            self.member_profile,
            FakeInteraction(channel_id=1, user=user),
            "곧 나갑니다",
        )
        member = SimpleNamespace(
            guild=SimpleNamespace(id=TEST_GUILD_ID), id=user.id
        )
        await self.usage.on_member_remove(member)
        self.assertIsNone(repository.get_bio(TEST_GUILD_ID, user.id))

    async def test_legacy_party_slash_commands_are_removed(self):
        for command in ("모집", "파티", "나가기", "변경"):
            self.assertNotIn(command, PartyCog.__dict__)

    async def test_event_commands_reach_their_backend_from_any_unrestricted_channel(self):
        guild = FakeGuild()
        interaction = FakeInteraction(channel_id=-1, guild=guild)
        cog = ScheduledEventCog(bot=None, settings_repository=_EventSettingsRepository())
        await ScheduledEventCog._show_events.callback(cog, interaction, 1)

        self.assertEqual(guild.fetch_scheduled_events_calls, 1)

    async def test_event_commands_only_run_in_the_configured_channel(self):
        guild = FakeGuild()
        cog = ScheduledEventCog(
            bot=None,
            settings_repository=_EventSettingsRepository(event_channel_id=77),
        )
        blocked = FakeInteraction(channel_id=66, guild=guild)
        await ScheduledEventCog._show_events.callback(cog, blocked, None)

        self.assertEqual(guild.fetch_scheduled_events_calls, 0)
        self.assertIn("<#77>", blocked.response.messages[0][0][0])
        self.assertTrue(blocked.response.messages[0][1]["ephemeral"])

        allowed = FakeInteraction(channel_id=77, guild=guild)
        await ScheduledEventCog._show_events.callback(cog, allowed, None)
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
        cog = ScheduledEventCog(
            bot=None,
            settings_repository=_EventSettingsRepository(),
        )

        listing = FakeInteraction(channel_id=1, guild=guild)
        detail = FakeInteraction(channel_id=1, guild=guild)
        await ScheduledEventCog._show_events.callback(cog, listing, None)
        await ScheduledEventCog._show_events.callback(cog, detail, 1)

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
        self.addCleanup(drop_panel_locks, guild.id)
        bot = _PartyBot(guild)
        patches = patch.object(party_cog, "GAMES", games) if games else None
        if patches:
            patches.start()
            self.addCleanup(patches.stop)
        with patch("discord.ext.tasks.Loop.start"):
            cog = PartyCog(bot, party, settings)
        return cog, party, settings, bot

    async def _select(self, cog, settings, guild, game, user=None):
        selector_id = settings.get_party_panels(guild.id)[
            party_cog.SELECTOR_PANEL_KEY
        ]
        button = next(
            child for child in cog.selector_view.children if child.game == game
        )
        interaction = FakeInteraction(
            guild.channel.id, guild, message_id=selector_id, user=user
        )
        await button.callback(interaction)
        return interaction, settings.get_party_panels(guild.id).get(game)

    def test_views_use_digest_ids_and_stay_within_component_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, _, bot = self._make_cog(pathlib.Path(directory), guild)

        self.assertEqual(set(cog.party_views), set(party_cog.GAMES))
        self.assertEqual(len(bot.registered), len(party_cog.GAMES) + 1)
        self.assertEqual(len(cog.selector_view.children), len(party_cog.GAMES))
        for game, view in cog.party_views.items():
            self.assertTrue(view.is_persistent())
            self.assertLessEqual(len(view.children), 25)
            expected = hashlib.sha256(game.encode("utf-8")).hexdigest()[:16]
            for child in view.children:
                self.assertIn(expected, child.custom_id)
                self.assertNotIn(game, child.custom_id)
                self.assertLess(len(child.custom_id), 100)
            expected_count = len(party_cog.GAMES[game]["roles"]) + 1
            self.assertEqual(len(view.children), expected_count if expected_count > 1 else 2)

    async def test_initial_panel_is_only_the_game_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, settings, _ = self._make_cog(pathlib.Path(directory), guild)

            await cog.ensure_panels(guild)

            panels = settings.get_party_panels(guild.id)
            self.assertEqual(set(panels), {party_cog.SELECTOR_PANEL_KEY})
            self.assertEqual(len(guild.channel.sent), 1)
            self.assertIn("게임 선택", guild.channel.sent[0][1]["embed"].title)

    async def test_selecting_a_game_creates_the_party_and_its_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            selector_id = settings.get_party_panels(guild.id)[
                party_cog.SELECTOR_PANEL_KEY
            ]

            button = next(
                child
                for child in cog.selector_view.children
                if child.game == "League of Legends"
            )
            await button.callback(FakeInteraction(50, guild, message_id=selector_id))

            self.assertEqual(
                party.get_participants(guild.id, "League of Legends"),
                {FakeUser.id: None},
            )
            self.assertIn("League of Legends", settings.get_party_panels(guild.id))

    async def test_active_panel_has_a_dedicated_leave_button(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            selector_id = settings.get_party_panels(guild.id)[
                party_cog.SELECTOR_PANEL_KEY
            ]
            selector = next(
                child for child in cog.selector_view.children if child.game == "PUBG"
            )
            await selector.callback(FakeInteraction(50, guild, message_id=selector_id))
            panel_id = settings.get_party_panels(guild.id)["PUBG"]
            leave = next(
                child for child in cog.party_views["PUBG"].children if child.action == "leave"
            )

            interaction = FakeInteraction(50, guild, message_id=panel_id)
            await leave.callback(interaction)

            self.assertIsNone(party.get_user_party(guild.id, FakeUser.id))
            self.assertNotIn("PUBG", settings.get_party_panels(guild.id))
            self.assertIn("나갔습니다", interaction.followup.messages[0][0][0])

    def test_role_ids_survive_reordering_and_change_on_rename(self):
        original = {"Game": {"max_players": 2, "roles": ["A", "B"]}}
        reordered = {"Game": {"max_players": 2, "roles": ["B", "A"]}}
        renamed = {"Game": {"max_players": 2, "roles": ["A", "C"]}}
        dummy = object()

        def role_ids(games):
            with patch.object(party_cog, "GAMES", games):
                view = party_cog.PartyPanelView(dummy, "Game")
            return {
                button.role: button.custom_id
                for button in view.children
                if button.role is not None
            }

        first = role_ids(original)
        second = role_ids(reordered)
        third = role_ids(renamed)
        self.assertEqual(first["A"], second["A"])
        self.assertEqual(first["B"], second["B"])
        self.assertNotIn(first["B"], third.values())

    async def test_twenty_five_games_share_one_selector_panel(self):
        games = {
            f"Game {index}": {"max_players": 2, "roles": []}
            for index in range(25)
        }
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, settings, bot = self._make_cog(
                pathlib.Path(directory), guild, games
            )
            await cog.ensure_panels(guild)
            panel_count = len(settings.get_party_panels(guild.id))

        self.assertEqual(len(cog.party_views), 25)
        self.assertEqual(len(cog.selector_view.children), 25)
        self.assertEqual(len(bot.registered), 26)
        self.assertEqual(panel_count, 1)

    async def test_over_limit_role_view_is_not_registered(self):
        games = {
            "Too Many": {"max_players": 25, "roles": [f"r{i}" for i in range(25)]},
            "Okay": {"max_players": 2, "roles": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, _, _, bot = self._make_cog(pathlib.Path(directory), guild, games)
        self.assertEqual(len(bot.registered), 2)
        self.assertEqual(cog.party_views["Too Many"].children, [])
        self.assertTrue(cog.party_views["Okay"].children)

    async def test_role_and_no_role_panels_cover_join_change_and_leave(self):
        with tempfile.TemporaryDirectory() as directory:
            second_user = SimpleNamespace(id=456, mention="<@456>")
            guild = _PartyGuild(members={FakeUser.id: FakeUser(), 456: second_user})
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)

            game = "League of Legends"
            _, panel_id = await self._select(cog, settings, guild, game)
            self.assertEqual(party.get_participants(guild.id, game), {FakeUser.id: None})
            top = next(child for child in cog.party_views[game].children if child.role == "탑")
            jungle = next(child for child in cog.party_views[game].children if child.role == "정글")
            leave = next(child for child in cog.party_views[game].children if child.action == "leave")
            await top.callback(FakeInteraction(50, guild, message_id=panel_id))
            await jungle.callback(
                FakeInteraction(50, guild, message_id=panel_id, user=second_user)
            )
            await leave.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertEqual(party.get_participants(guild.id, game), {456: "정글"})
            await leave.callback(
                FakeInteraction(50, guild, message_id=panel_id, user=second_user)
            )
            self.assertIsNone(party.get_party(guild.id, game))

            game = "PUBG"
            _, panel_id = await self._select(cog, settings, guild, game)
            join, leave = cog.party_views[game].children
            await join.callback(
                FakeInteraction(50, guild, message_id=panel_id, user=second_user)
            )
            self.assertEqual(party.get_user_party(guild.id, 456), game)
            await leave.callback(
                FakeInteraction(50, guild, message_id=panel_id, user=second_user)
            )
            await leave.callback(FakeInteraction(50, guild, message_id=panel_id))
            self.assertIsNone(party.get_user_party(guild.id, FakeUser.id))

    async def test_repository_rejections_and_concurrent_role_clicks_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            users = {uid: SimpleNamespace(id=uid, mention=f"<@{uid}>") for uid in range(1, 8)}
            guild = _PartyGuild(members=users)
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            game = "League of Legends"
            _, panel_id = await self._select(
                cog, settings, guild, game, user=users[1]
            )
            for uid, role in zip(range(1, 6), party_cog.GAMES[game]["roles"]):
                party.add_participant(guild.id, game, uid, role, 5)

            full = FakeInteraction(50, guild, message_id=panel_id, user=users[6])
            await cog.party_views[game].children[0].callback(full)
            self.assertIn("가득", full.followup.messages[0][0][0])
            self.assertIsNone(party.get_user_party(guild.id, 6))

            party.remove_participant(guild.id, game, 5)
            racer = users[7]
            first = FakeInteraction(50, guild, message_id=panel_id, user=racer)
            second = FakeInteraction(50, guild, message_id=panel_id, user=racer)
            await asyncio.gather(
                cog.party_views[game].children[3].callback(first),
                cog.party_views[game].children[4].callback(second),
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
            _, panel_id = await self._select(cog, settings, guild, game, user=host)
            buttons = cog.party_views[game].children

            await buttons[0].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=host)
            )
            await buttons[1].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=successor)
            )
            await buttons[2].callback(
                FakeInteraction(50, guild, message_id=panel_id, user=host)
            )
            self.assertEqual(party.get_party_host(guild.id, game), 1)

            await buttons[-1].callback(
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
            selector_id = settings.get_party_panels(guild.id)[
                party_cog.SELECTOR_PANEL_KEY
            ]
            lol = FakeInteraction(
                50,
                guild,
                message_id=selector_id,
            )
            pubg = FakeInteraction(
                50,
                guild,
                message_id=selector_id,
            )
            lol_button = next(
                child
                for child in cog.selector_view.children
                if child.game == "League of Legends"
            )
            pubg_button = next(
                child for child in cog.selector_view.children if child.game == "PUBG"
            )
            await asyncio.gather(
                lol_button.callback(lol),
                pubg_button.callback(pubg),
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
            _, panel_id = await self._select(cog, settings, guild, game)
            interaction = FakeInteraction(50, guild, message_id=panel_id)
            deferred = asyncio.Event()
            original_defer = interaction.response.defer

            async def recording_defer(**kwargs):
                await original_defer(**kwargs)
                deferred.set()

            interaction.response.defer = recording_defer
            lock = panel_lock(guild.id, f"party:{game}")
            await lock.acquire()
            task = asyncio.create_task(cog.party_views[game].children[-1].callback(interaction))
            await deferred.wait()
            settings.set_party_panel(guild.id, game, panel_id + 1)
            lock.release()
            await task
            current_party = party.get_party(guild.id, game)

        self.assertIsNotNone(current_party)
        self.assertIn("최신", interaction.followup.messages[0][0][0])

    async def test_dm_cross_guild_stale_panel_and_deleted_member_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            await cog.ensure_panels(guild)
            game = "PUBG"
            _, panel_id = await self._select(cog, settings, guild, game)
            button = cog.party_views[game].children[-1]

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

        self.assertEqual(user_party, game)
        self.assertTrue(dm.response.messages and wrong.response.messages and deleted.response.messages)
        self.assertTrue(stale.followup.messages)

    async def test_ensure_panels_edits_recreates_and_cleans_only_bot_stale_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            guild = _PartyGuild()
            cog, party, settings, _ = self._make_cog(pathlib.Path(directory), guild)
            current = next(iter(party_cog.GAMES))
            guild.channel.messages[10] = _PartyMessage(10)
            guild.channel.messages[20] = _PartyMessage(20)
            settings.set_party_panel(guild.id, current, 10)
            settings.set_party_panel(guild.id, "Removed", 20)
            party.create_party(guild.id, current, 1_000, FakeUser.id)
            await cog.ensure_panels(guild)

            self.assertTrue(guild.channel.messages[10].edits)
            self.assertTrue(guild.channel.messages[20].deleted)
            self.assertNotIn("Removed", settings.get_party_panels(guild.id))

            missing_game = list(party_cog.GAMES)[1]
            party.create_party(guild.id, missing_game, 1_000, 456)
            await cog.render_game_panel(guild.id, missing_game)
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

            with patch("module.party_cog.time.time", return_value=100_000), patch.object(
                party, "delete_party_if_expired", side_effect=recording_delete
            ), patch.object(cog, "render_game_panel") as render:
                await PartyCog.cleanup_parties.coro(cog)
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
                party_cog.logger, "exception"
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
    def __init__(self, content, guild_id=None, channel_id=1, author_is_bot=False):
        self.content = content
        self.author = SimpleNamespace(
            bot=author_is_bot, id=FakeUser.id, mention=FakeUser.mention
        )
        self.guild = (
            None
            if guild_id is None
            else SimpleNamespace(id=guild_id)
        )
        self.channel = SimpleNamespace(id=channel_id, send=self._send)
        self.sent = []

    async def _send(self, text):
        self.sent.append(text)


class RecordingForbiddenCounts:
    def __init__(self):
        self.counts = []

    async def increment_forbidden_count(self, guild_id, user_id):
        self.counts.append(user_id)


class RecordingFilterSettings:
    """금지어 on/off만 기억하는 GuildSettingsRepository 대역."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.reads = 0

    def get_forbidden_filter_enabled(self, guild_id):
        self.reads += 1
        return self.enabled


def make_forbidden_cog(
    counter,
    words=("나쁜말",),
    document=None,
    settings_repository=None,
):
    with tempfile.TemporaryDirectory() as directory:
        pathlib.Path(directory, "forbidden_words.json").write_text(
            json.dumps(list(words) if document is None else document),
            encoding="utf-8",
        )
        with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)), patch(
            "module.forbidden_filter_cog.print"
        ):
            cog = forbidden_filter_cog.ForbiddenFilterCog(
                SimpleNamespace(get_cog=lambda name: counter),
                settings_repository or RecordingFilterSettings(),
            )
    return cog


class ForbiddenFilterDegradesTest(unittest.TestCase):
    def test_missing_file_yields_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                self.assertEqual(forbidden_filter_cog.load_forbidden_words(), [])

    def test_cog_constructs_without_word_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = forbidden_filter_cog.ForbiddenFilterCog(
                    bot=None,
                    settings_repository=RecordingFilterSettings(),
                )
        self.assertIsNone(cog._find_forbidden_word("아무 말이나"))


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

            def get_cog(self, name):
                return object()

        with patch.object(main, "_verify_databases"), patch.object(
            main, "DATA_DIR", pathlib.Path(tempfile.mkdtemp())
        ):
            await main.HyacineBot.setup_hook(FakeBot())

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

    def test_panel_channel_requires_every_runtime_permission(self):
        guild = _SetupGuild()
        channel = _SetupChannel(1, "party", read_message_history=False)

        self.assertFalse(panel_module.is_sendable_panel_channel(guild, channel))

    async def test_stored_panel_from_previous_channel_is_not_current(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "discord.ext.tasks.Loop.start"
        ):
            root = pathlib.Path(directory)
            settings = SQLiteGuildSettingsRepository(root / "settings.db")
            settings.set_party_channel(1, 77)
            settings.set_party_panel(1, party_cog.SELECTOR_PANEL_KEY, 100)
            cog = PartyCog(
                None,
                party_repository=SQLitePartyRepository(root / "party.db"),
                settings_repository=settings,
            )
            interaction = SimpleNamespace(
                guild_id=1,
                channel_id=88,
                message=SimpleNamespace(id=100),
            )

            self.assertFalse(await cog._is_current_selector(interaction))
            interaction.channel_id = 77
            self.assertTrue(await cog._is_current_selector(interaction))


class _SetupChannel:
    type = discord.ChannelType.text

    def __init__(self, channel_id, name, **permission_overrides):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.permissions = {
            "view_channel": True,
            "send_messages": True,
            "read_message_history": True,
            "embed_links": True,
            "attach_files": True,
            **permission_overrides,
        }
        self.edits = []

    def permissions_for(self, member):
        return SimpleNamespace(**self.permissions)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]


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
    def __init__(self, play_cog=None, filter_cog=None):
        self.views = []
        self.guilds = []
        self.play_cog = play_cog
        self.filter_cog = filter_cog

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        if name == "PartyCog":
            return self.play_cog
        return self.filter_cog if name == "ForbiddenFilterCog" else None


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


class GuildSettingsCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_web_settings_are_saved_together(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(
                pathlib.Path(directory) / "settings.db"
            )
            settings.set_party_channel(TEST_GUILD_ID, 99)
            settings.set_party_panel(TEST_GUILD_ID, "selector", 999)
            settings.set_guild_settings(
                TEST_GUILD_ID,
                11,
                22,
                33,
                True,
                False,
            )

            self.assertEqual(settings.get_party_channel(TEST_GUILD_ID), 11)
            self.assertEqual(settings.get_announcement_channel(TEST_GUILD_ID), 22)
            self.assertEqual(settings.get_event_channel(TEST_GUILD_ID), 33)
            self.assertTrue(settings.get_allow_host_announce(TEST_GUILD_ID))
            self.assertFalse(
                settings.get_forbidden_filter_enabled(TEST_GUILD_ID)
            )
            self.assertEqual(
                settings.get_party_panels(TEST_GUILD_ID), {"selector": 999}
            )

    async def test_show_reports_the_filter_state(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(
                pathlib.Path(directory) / "settings.db"
            )
            cog = GuildSettingsCog(_SetupBot(), settings)
            settings.set_forbidden_filter_enabled(TEST_GUILD_ID, False)
            settings.set_event_channel(TEST_GUILD_ID, 77)

            interaction = SimpleNamespace(
                guild_id=TEST_GUILD_ID, response=RecordingResponse()
            )
            await GuildSettingsCog._show_settings.callback(cog, interaction)

        embed = interaction.response.messages[0][1]["embed"]
        field = next(f for f in embed.fields if f.name == "금지어 필터")
        self.assertEqual(field.value, "꺼짐")
        event_field = next(
            f for f in embed.fields if f.name == "이벤트 전용 채널"
        )
        self.assertEqual(event_field.value, "<#77>")
        self.assertIn("/설정 공지허용", embed.footer.text)


class GreetingOutsideAIGateTests(unittest.IsolatedAsyncioTestCase):
    def test_greeting_loads_without_any_ai_key(self):
        env = {"ADMIN_TOKEN": "t", "OPENAI_API_KEY": None, "GOOGLE_API_KEY": None}
        with patch.object(bot_main, "OPTIONAL_DEPENDENCY_VALUES", env):
            names = bot_main.available_extensions()
        self.assertIn("module.greeting_cog", names)
        self.assertNotIn("module.ai_chat_cog", names)

    async def test_greeting_reads_the_current_persona(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"greeting": "첫 인사"}), encoding="utf-8"
            )
            cog = greeting_cog.GreetingCog(bot=None)
            interaction = FakeInteraction(channel_id=1)
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                await greeting_cog.GreetingCog._hello.callback(cog, interaction)
                (pathlib.Path(directory) / "persona.json").write_text(
                    json.dumps({"greeting": "바뀐 인사"}), encoding="utf-8"
                )
                later = FakeInteraction(channel_id=1)
                await greeting_cog.GreetingCog._hello.callback(cog, later)

        self.assertIn("첫 인사", interaction.response.messages[0][0][0])
        self.assertIn("바뀐 인사", later.response.messages[0][0][0])


class GuildSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_only_supported_settings_commands_remain(self):
        self.assertEqual(
            {command.name for command in GuildSettingsCog.설정.commands},
            {"시작", "공지허용", "확인"},
        )

    async def test_guild_administrator_controls_host_announcement_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(
                pathlib.Path(directory) / "settings.db"
            )
            cog = GuildSettingsCog(_SetupBot(), settings)
            interaction = SimpleNamespace(
                guild_id=TEST_GUILD_ID,
                response=RecordingResponse(),
            )

            await GuildSettingsCog._set_host_announcements.callback(
                cog, interaction, True
            )
            self.assertTrue(settings.get_allow_host_announce(TEST_GUILD_ID))

        self.assertIn("허용", interaction.response.messages[0][0][0])

    async def test_settings_commands_are_administrator_only(self):
        permissions = GuildSettingsCog.설정.default_permissions
        self.assertTrue(permissions.administrator)
        self.assertFalse(permissions.manage_guild)

        with tempfile.TemporaryDirectory() as directory:
            cog = GuildSettingsCog(
                _SetupBot(),
                SQLiteGuildSettingsRepository(
                    pathlib.Path(directory) / "settings.db"
                ),
            )
            administrator = SimpleNamespace(
                user=SimpleNamespace(
                    guild_permissions=SimpleNamespace(administrator=True)
                )
            )
            manager = SimpleNamespace(
                user=SimpleNamespace(
                    guild_permissions=SimpleNamespace(administrator=False)
                )
            )

            self.assertTrue(await cog.interaction_check(administrator))
            with self.assertRaises(discord.app_commands.MissingPermissions):
                await cog.interaction_check(manager)

    async def test_show_marks_a_stored_channel_that_can_no_longer_host_panels(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(
                pathlib.Path(directory) / "settings.db"
            )
            cog = GuildSettingsCog(_SetupBot(), settings)
            guild = _SetupGuild()
            channel = _SetupChannel(88, "blocked", embed_links=False)
            guild.channels[channel.id] = channel
            settings.set_party_channel(guild.id, channel.id)
            interaction = SimpleNamespace(
                guild=guild,
                guild_id=guild.id,
                response=RecordingResponse(),
            )

            await GuildSettingsCog._show_settings.callback(cog, interaction)

            embed = interaction.response.messages[0][1]["embed"]
            field = next(f for f in embed.fields if f.name == "파티 패널 채널")
            self.assertIn("권한 부족", field.value)

    async def test_setup_completion_ensures_party_panels(self):
        party_calls = []

        async def ensure_panels(guild):
            party_calls.append(guild)

        play_cog = SimpleNamespace(ensure_panels=ensure_panels)
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            guild = _SetupGuild()
            cog = GuildSettingsCog(_SetupBot(play_cog), settings)
            await cog._ensure_bot_channels(guild)

        self.assertEqual(party_calls, [guild])

    async def test_deleted_party_channel_is_recreated_with_panels_on_next_setup(self):
        calls = []

        async def ensure_panels(guild):
            calls.append(guild)

        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            guild = _SetupGuild()
            old_party = _SetupChannel(30, "old-party")
            guild.channels = {30: old_party}
            settings.set_party_channel(guild.id, 30)
            cog = GuildSettingsCog(
                _SetupBot(SimpleNamespace(ensure_panels=ensure_panels)), settings
            )

            await cog.on_guild_channel_delete(
                SimpleNamespace(id=30, guild=guild)
            )
            guild.channels.pop(30)
            party = await cog._ensure_bot_channels(guild)

        self.assertIsNone(guild.get_channel(30))
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
            [channel.name for channel, _ in guild.created_channels], ["🎮-디스코-파티"]
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
        self.assertEqual(len(concurrent_guild.created_channels), 1)
        self.assertEqual(concurrent[0], concurrent[1])

    async def test_setup_reuses_stored_live_channels_without_renaming_them(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            cog = GuildSettingsCog(_SetupBot(), settings)
            guild = _SetupGuild()
            party = _SetupChannel(30, "party-custom-name")
            guild.channels = {party.id: party}
            settings.set_party_channel(guild.id, party.id)

            result = await cog.ensure_bot_channels(guild)

        self.assertIs(result, party)
        self.assertEqual(party.name, "party-custom-name")
        self.assertEqual(guild.created_categories, [])
        self.assertEqual(guild.created_channels, [])

    async def test_setup_renames_the_legacy_default_party_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            cog = GuildSettingsCog(_SetupBot(), settings)
            guild = _SetupGuild()
            party = _SetupChannel(30, "🎮-파티")
            guild.channels = {party.id: party}
            settings.set_party_channel(guild.id, party.id)

            result = await cog.ensure_bot_channels(guild)

        self.assertIs(result, party)
        self.assertEqual(party.name, "🎮-디스코-파티")
        self.assertEqual(party.edits, [{"name": "🎮-디스코-파티"}])

    async def test_ready_renames_the_legacy_default_party_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            bot = _SetupBot()
            cog = GuildSettingsCog(bot, settings)
            guild = _SetupGuild()
            party = _SetupChannel(30, "🎮-파티")
            guild.channels = {party.id: party}
            bot.guilds = [guild]
            settings.set_party_channel(guild.id, party.id)

            await cog.on_ready()

        self.assertEqual(party.name, "🎮-디스코-파티")
        self.assertEqual(party.edits, [{"name": "🎮-디스코-파티"}])

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




class ForbiddenResponseTests(unittest.IsolatedAsyncioTestCase):

    async def test_burst_answers_and_counts_every_hit(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)
        messages = [FakeMessage("나쁜말", guild_id=TEST_GUILD_ID) for _ in range(5)]

        for message in messages:
            await cog.on_message(message)

        self.assertEqual([len(m.sent) for m in messages], [1, 1, 1, 1, 1])
        self.assertEqual(counter.counts, [FakeUser.id] * 5)

    async def test_leaving_a_guild_drops_its_process_state(self):
        cog = make_forbidden_cog(RecordingForbiddenCounts())
        await cog.on_message(FakeMessage("나쁜말", guild_id=TEST_GUILD_ID))
        await cog.on_message(FakeMessage("나쁜말", guild_id=99, channel_id=3))

        await cog.on_guild_remove(SimpleNamespace(id=TEST_GUILD_ID))

        self.assertNotIn(TEST_GUILD_ID, cog._enabled_by_guild)

    async def test_bot_messages_are_never_screened(self):
        """봇 응답이 다시 걸리면 봇끼리 핑퐁이 돈다. 회귀 방지."""
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID, author_is_bot=True)

        await cog.on_message(message)

        self.assertEqual(message.sent, [])
        self.assertEqual(counter.counts, [])

    async def test_send_failure_does_not_lose_the_moderation_count(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(counter)
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)
        message.channel.send = AsyncMock(side_effect=RuntimeError("send failed"))

        with patch.object(forbidden_filter_cog.logger, "exception"):
            await cog.on_message(message)

        self.assertEqual(counter.counts, [FakeUser.id])

    async def test_count_failure_does_not_suppress_the_channel_response(self):
        counter = SimpleNamespace(
            increment_forbidden_count=AsyncMock(
                side_effect=RuntimeError("database failed")
            )
        )
        cog = make_forbidden_cog(counter)
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)

        with patch.object(forbidden_filter_cog.logger, "exception"):
            await cog.on_message(message)

        self.assertEqual(len(message.sent), 1)


class ForbiddenPolicyDocumentTests(unittest.IsolatedAsyncioTestCase):
    """배열(구형)과 객체(신형) 두 형태를 모두 받는다."""

    def test_legacy_array_keeps_working(self):
        policy = forbidden_filter_cog.canonicalize_forbidden_policy(["Bad", " ", 1])
        self.assertEqual(policy.words, ["bad", "1"])
        self.assertEqual(policy.template, forbidden_filter_cog.DEFAULT_TEMPLATE)
        self.assertEqual(policy.allow, [])

    def test_object_form_reads_template_and_allow(self):
        policy = forbidden_filter_cog.canonicalize_forbidden_policy(
            {"words": ["시장"], "template": "{mention} 금지: {word}", "allow": ["시장님"]}
        )
        self.assertEqual((policy.words, policy.allow), (["시장"], ["시장님"]))
        self.assertEqual(policy.template, "{mention} 금지: {word}")

    def test_terms_use_message_normalization_and_drop_duplicates(self):
        policy = forbidden_filter_cog.canonicalize_forbidden_policy(
            {
                "words": ["금지 어", "금지-어", "  "],
                "allow": ["금지어가 들어간 멀쩡한 표현"],
            }
        )

        self.assertEqual(policy.words, ["금지어"])
        self.assertEqual(policy.allow, ["금지어가들어간멀쩡한표현"])

    def test_document_canonicalization_preserves_shape(self):
        self.assertEqual(
            forbidden_filter_cog.canonicalize_forbidden_document(["A"]), ["a"]
        )
        self.assertEqual(
            forbidden_filter_cog.canonicalize_forbidden_document({"words": ["A"]}),
            {
                "words": ["a"],
                "template": forbidden_filter_cog.DEFAULT_TEMPLATE,
                "allow": [],
            },
        )

    def test_strict_rejects_malformed_objects(self):
        for invalid in ({}, {"words": "not a list"}, {"words": [], "template": ""},
                        {"words": [], "allow": "no"}, "string"):
            with self.subTest(document=invalid), self.assertRaises(ValueError):
                forbidden_filter_cog.canonicalize_forbidden_policy(invalid, strict=True)

    def test_strict_bounds_terms_counts_and_rendered_template(self):
        invalid_documents = (
            {"words": ["x" * 101]},
            {"words": [str(index) for index in range(1_001)]},
            {"words": [], "allow": ["x" * 101]},
            {"words": ["x"], "template": "{word}" * 21},
        )

        for document in invalid_documents:
            with self.subTest(document=list(document)), self.assertRaises(ValueError):
                forbidden_filter_cog.canonicalize_forbidden_policy(
                    document, strict=True
                )

    def test_runtime_drops_oversized_terms_and_uses_default_template(self):
        with patch.object(forbidden_filter_cog, "print") as warned:
            policy = forbidden_filter_cog.canonicalize_forbidden_policy(
                {
                    "words": ["정상", "x" * 101],
                    "template": "z" * 2_001,
                    "allow": ["y" * 101],
                }
            )

        self.assertEqual(policy.words, ["정상"])
        self.assertEqual(policy.allow, [])
        self.assertEqual(policy.template, forbidden_filter_cog.DEFAULT_TEMPLATE)
        self.assertTrue(warned.called)

    async def test_template_substitutes_only_two_placeholders(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(
            counter,
            document={
                "words": ["나쁜말"],
                "template": "{mention}: {word} / {bot.token} {0}",
            },
        )
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)

        await cog.on_message(message)

        # {mention}·{word}만 치환되고 나머지 중괄호는 문자 그대로 남는다.
        self.assertEqual(message.sent, ["<@123>: 나쁜말 / {bot.token} {0}"])

    async def test_allow_list_beats_a_substring_hit(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(
            counter, document={"words": ["시장"], "allow": ["시장님"]}
        )
        allowed = FakeMessage("시장님 안녕하세요", guild_id=TEST_GUILD_ID)
        caught = FakeMessage("시장 갑니다", guild_id=TEST_GUILD_ID, channel_id=2)

        await cog.on_message(allowed)
        await cog.on_message(caught)

        self.assertEqual(allowed.sent, [])
        self.assertEqual(len(caught.sent), 1)
        self.assertEqual(counter.counts, [FakeUser.id])

    async def test_allow_list_still_catches_a_hit_outside_it(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(
            counter, document={"words": ["시장"], "allow": ["시장님"]}
        )
        message = FakeMessage("시장님과 시장 사람들", guild_id=TEST_GUILD_ID)

        await cog.on_message(message)

        self.assertEqual(counter.counts, [FakeUser.id])

    async def test_spaced_allow_phrase_beats_separator_obfuscation(self):
        counter = RecordingForbiddenCounts()
        cog = make_forbidden_cog(
            counter,
            document={
                "words": ["금지 어"],
                "allow": ["금지어가 들어간 멀쩡한 표현"],
            },
        )
        allowed = FakeMessage(
            "금지어가 들어간 멀쩡한 표현", guild_id=TEST_GUILD_ID
        )
        caught = FakeMessage("금-지 어", guild_id=TEST_GUILD_ID, channel_id=2)

        await cog.on_message(allowed)
        await cog.on_message(caught)

        self.assertEqual(allowed.sent, [])
        self.assertEqual(len(caught.sent), 1)
        self.assertEqual(counter.counts, [FakeUser.id])


class ForbiddenGuildToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_guild_is_not_screened(self):
        counter = RecordingForbiddenCounts()
        settings_repository = RecordingFilterSettings(enabled=False)
        cog = make_forbidden_cog(
            counter, settings_repository=settings_repository
        )
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)

        await cog.on_message(message)

        self.assertEqual(message.sent, [])
        self.assertEqual(counter.counts, [])

    async def test_setting_is_read_once_per_guild_then_cached(self):
        settings_repository = RecordingFilterSettings()
        cog = make_forbidden_cog(
            RecordingForbiddenCounts(),
            settings_repository=settings_repository,
        )

        for _ in range(3):
            await cog.on_message(FakeMessage("깨끗한 말", guild_id=TEST_GUILD_ID))

        self.assertEqual(settings_repository.reads, 1)

    async def test_invalidate_guild_forces_a_reread(self):
        settings_repository = RecordingFilterSettings()
        cog = make_forbidden_cog(
            RecordingForbiddenCounts(),
            settings_repository=settings_repository,
        )
        await cog.on_message(FakeMessage("깨끗한 말", guild_id=TEST_GUILD_ID))

        cog.invalidate_guild(TEST_GUILD_ID)
        settings_repository.enabled = False
        message = FakeMessage("나쁜말", guild_id=TEST_GUILD_ID)
        await cog.on_message(message)

        self.assertEqual(settings_repository.reads, 2)
        self.assertEqual(message.sent, [])


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

    async def test_reload_prepares_off_loop_then_publishes_state(self):
        pattern = forbidden_filter_cog._build_pattern(["새금지어"])
        policy = forbidden_filter_cog.ForbiddenPolicy(
            words=["새금지어"],
            template=forbidden_filter_cog.DEFAULT_TEMPLATE,
            allow=[],
        )
        with patch.object(
            forbidden_filter_cog.asyncio,
            "to_thread",
            AsyncMock(return_value=(policy, pattern, None)),
        ) as to_thread, patch("module.forbidden_filter_cog.print"):
            loaded = await self.cog.reload_forbidden_words()
        to_thread.assert_awaited_once_with(forbidden_filter_cog._load_filter_state)
        self.assertEqual(loaded, ["새금지어"])
        self.assertEqual(self.cog._forbidden_words, ["새금지어"])
        self.assertIs(self.cog._forbidden_pattern, pattern)

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
                cog = PartyCog(bot=None, party_repository=repository)

            for user_id in range(party_cog.GAMES[game]["max_players"]):
                self.assertTrue(await cog.add_participant(TEST_GUILD_ID, game, user_id))

            self.assertFalse(await cog.add_participant(TEST_GUILD_ID, game, 999))
            self.assertIsNone(repository.get_user_party(TEST_GUILD_ID, 999))


class PartyMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_leaving_member_frees_party_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLitePartyRepository(pathlib.Path(directory) / "party.db")
            game = next(iter(party_cog.GAMES))
            repository.create_party(TEST_GUILD_ID, game, 1_000)
            repository.add_participant(TEST_GUILD_ID, game, 42, "탑")
            repository.add_participant(TEST_GUILD_ID, game, 43, "미드")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PartyCog(bot=None, party_repository=repository)

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
            game = next(iter(party_cog.GAMES))
            repository.create_party(TEST_GUILD_ID, game, 1_000)
            repository.add_participant(TEST_GUILD_ID, game, 42, "탑")
            with patch("discord.ext.tasks.Loop.start"):
                cog = PartyCog(bot=None, party_repository=repository)

            await cog.on_member_remove(
                SimpleNamespace(id=42, guild=SimpleNamespace(id=TEST_GUILD_ID))
            )

            self.assertIsNone(repository.get_party(TEST_GUILD_ID, game))


class BackupConnectionTests(unittest.TestCase):
    @staticmethod
    def _backup_tree(root):
        data = pathlib.Path(root) / "data"
        backups = pathlib.Path(root) / "backups"
        settings = pathlib.Path(root) / "settings"
        SQLiteUsageRepository(data / "usage_data.db")
        SQLitePartyRepository(data / "party_data.db")
        SQLiteGuildSettingsRepository(data / "guild_settings.db")
        SQLiteGameUidRepository(data / "game_uid_data.db")
        settings.mkdir()
        return data, backups, settings

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

    def test_pre_manifest_failures_remove_every_published_backup_file(self):
        fixed = datetime(2026, 8, 19, tzinfo=timezone.utc)
        timestamp = "20260819T000000Z"
        failures = ("hash", "manifest-write", "manifest-replace")

        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                data, backups, settings = self._backup_tree(root)
                real_replace = backup.os.replace

                def replace(source, destination):
                    if failure == "manifest-replace" and str(destination).endswith(
                        "-manifest.json"
                    ):
                        raise OSError("manifest replace failed")
                    return real_replace(source, destination)

                patches = [
                    patch.object(backup, "DATA_DIR", data),
                    patch.object(backup, "BACKUP_DIR", backups),
                    patch.object(backup, "SETTINGS_DIR", settings),
                    patch.object(backup.os, "replace", side_effect=replace),
                ]
                if failure == "hash":
                    patches.append(
                        patch.object(
                            backup, "_sha256", side_effect=OSError("hash failed")
                        )
                    )
                if failure == "manifest-write":
                    patches.append(
                        patch.object(
                            backup.json, "dump", side_effect=OSError("write failed")
                        )
                    )

                with contextlib.ExitStack() as stack:
                    for configured in patches:
                        stack.enter_context(configured)
                    with self.assertRaises(OSError):
                        backup.create_backup_set(fixed)

                self.assertEqual(list(backups.glob(f"{timestamp}-*")), [])

    def test_post_manifest_fsync_failure_keeps_a_complete_backup_set(self):
        fixed = datetime(2026, 8, 19, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            data, backups, settings = self._backup_tree(root)
            real_replace = backup.os.replace
            real_fsync_directory = backup._fsync_directory
            manifest_published = False

            def replace(source, destination):
                nonlocal manifest_published
                result = real_replace(source, destination)
                if str(destination).endswith("-manifest.json"):
                    manifest_published = True
                return result

            def fsync_directory(path):
                if manifest_published:
                    raise OSError("post-publication fsync failed")
                return real_fsync_directory(path)

            with patch.object(backup, "DATA_DIR", data), patch.object(
                backup, "BACKUP_DIR", backups
            ), patch.object(backup, "SETTINGS_DIR", settings), patch.object(
                backup.os, "replace", side_effect=replace
            ), patch.object(
                backup, "_fsync_directory", side_effect=fsync_directory
            ):
                with self.assertRaises(OSError):
                    backup.create_backup_set(fixed)

            manifest = backups / "20260819T010000Z-manifest.json"
            self.assertTrue(manifest.exists())
            self.assertEqual(set(backup.verify_backup_set(manifest)), set(backup.DATABASES))


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
            list(games), ["G" * 89, *(f"Game {index}" for index in range(24))]
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
        roles = [f"{index:02d}" + "r" * 37 for index in range(24)]
        roster = {
            "Largest": {"max_players": 25, "roles": roles},
            "Too Many Players": {"max_players": 26, "roles": []},
            "Too Much Role Text": {
                "max_players": 25,
                "roles": [*roles[:-1], roles[-1] + "x" * 43],
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
        self.openai_key = patch("module.ai_chat_cog.OPENAI_API_KEY", "sk-test-dummy")
        self.openai_key.start()
        self.addCleanup(self.openai_key.stop)

    def test_persona_comes_from_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"system_prompt": "테스트 프롬프트", "greeting": "테스트 인사"}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = AIChatCog(bot=None)
        self.assertEqual(cog.system_prompt, "테스트 프롬프트")

    def test_missing_persona_keys_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"system_prompt": "프롬프트만"}), encoding="utf-8"
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = AIChatCog(bot=None)
        self.assertEqual(cog.system_prompt, "프롬프트만")

    def test_missing_system_prompt_keeps_hyacine_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "persona.json").write_text(
                json.dumps({"greeting": "테스트 인사"}), encoding="utf-8"
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                cog = AIChatCog(bot=None)

        self.assertIn("히아킨", cog.system_prompt)
        self.assertIn("회색둥이 씨", cog.system_prompt)

    def test_persona_field_limits_reject_saves_and_fallback_at_runtime(self):
        invalid = {
            "system_prompt": "p" * 16_001,
            "greeting": "g" * 1_975,
        }

        with self.assertRaises(ValueError):
            config._validate_settings_document("persona.json", invalid)
        with patch.object(ai_chat_cog, "print") as warned:
            persona = ai_chat_cog.canonicalize_persona(invalid)

        self.assertEqual(persona, ai_chat_cog.DEFAULT_PERSONA)
        self.assertTrue(warned.called)

    def test_constructors_take_no_nickname(self):
        with self.assertRaises(TypeError):
            AIChatCog(bot=None, nickname="회색")
        with self.assertRaises(TypeError):
            AIImageCog(bot=None, nickname="회색")


class PersonaSessionRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_refreshes_persona_without_changing_old_session(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "module.ai_chat_cog.OPENAI_API_KEY", "sk-test-dummy"
        ):
            persona_path = pathlib.Path(directory) / "persona.json"
            persona_path.write_text(
                json.dumps({"system_prompt": "old prompt", "greeting": "old greeting"}),
                encoding="utf-8",
            )
            with patch.object(config, "SETTINGS_DIR", pathlib.Path(directory)):
                usage_cog = RecordingUsage()
                cog = AIChatCog(
                    bot=SimpleNamespace(get_cog=lambda _: usage_cog)
                )
                old_session = cog.get_or_create_session(1)
                persona_path.write_text(
                    json.dumps({"system_prompt": "new prompt", "greeting": "new greeting"}),
                    encoding="utf-8",
                )
                new_session = cog.get_or_create_session(2)

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
                await cog._run_chat(
                    FakeInteraction(channel_id=1), "old", None, "test-model", "none",
                    "light", config.LIMIT_LIGHT,
                )
                await cog._run_chat(
                    FakeInteraction(channel_id=2), "new", None, "test-model", "none",
                    "light", config.LIMIT_LIGHT,
                )
        self.assertEqual(old_session.system_prompt, "old prompt")
        self.assertEqual(new_session.system_prompt, "new prompt")
        self.assertEqual(captured_instructions, ["old prompt", "new prompt"])


class _StubShowcaseService:
    """ShowcaseService 대역. 네트워크 없이 명령 경로만 확인한다."""

    def __init__(self, results=None):
        # {(game, uid): Showcase 또는 예외}
        self.results = results or {}
        self.calls = []

    def get_adapter(self, game):
        return enka_profiles.ADAPTERS[game]

    async def fetch_showcase(self, game, uid, *, use_cache=True):
        self.calls.append((game, uid))
        result = self.results.get((game, uid))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise enka_profiles.ShowcaseLookupError("없는 계정")
        return result

    async def close(self):
        pass


def _showcase(nickname="플레이어", level=70, characters=()):
    return enka_profiles.Showcase(
        nickname=nickname, level=level, characters=list(characters)
    )


def _character(name="에버나이트", level=80, art="https://example.invalid/a.png"):
    return enka_profiles.ShowcaseCharacter(name=name, level=level, art_url=art)


class _ProfileInteraction:
    def __init__(self, guild_id=TEST_GUILD_ID, user_id=FakeUser.id):
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id, display_name="테스트 유저")
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()


def _choice(value):
    return discord.app_commands.Choice(name=value, value=value)


class ProfileRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = SQLiteGameUidRepository(
            pathlib.Path(self.directory.name) / "profile.db"
        )
        self.showcase_service = _StubShowcaseService()
        self.cog = game_profile_cog.GameProfileCog(
            bot=None,
            game_uid_repository=self.repository,
            showcase_service=self.showcase_service,
        )

    async def test_registration_defers_then_stores_a_validated_uid(self):
        self.showcase_service.results[("hsr", "800333171")] = _showcase(
            characters=[_character()]
        )
        interaction = _ProfileInteraction()

        await game_profile_cog.GameProfileCog._register.callback(
            self.cog, interaction, _choice("hsr"), " 800333171 "
        )

        # Enka 조회가 3초를 넘길 수 있으므로 먼저 ACK해야 한다.
        self.assertEqual(interaction.response.defer_kwargs, [{"ephemeral": True}])
        self.assertEqual(
            self.repository.get_uid(TEST_GUILD_ID, FakeUser.id, "hsr"), "800333171"
        )
        self.assertIn("800333171", interaction.followup.messages[0][0][0])

    async def test_bad_uid_format_never_reaches_the_network(self):
        interaction = _ProfileInteraction()

        await game_profile_cog.GameProfileCog._register.callback(
            self.cog, interaction, _choice("hsr"), "12ab"
        )

        self.assertEqual(self.showcase_service.calls, [])
        self.assertIsNone(self.repository.get_uid(TEST_GUILD_ID, FakeUser.id, "hsr"))
        self.assertIn("9자리", interaction.followup.messages[0][0][0])

    async def test_non_ascii_uid_digits_never_reach_the_network(self):
        for uid in ("²" * 9, "８" * 9):
            with self.subTest(uid=uid):
                interaction = _ProfileInteraction()
                await game_profile_cog.GameProfileCog._register.callback(
                    self.cog, interaction, _choice("hsr"), uid
                )

                self.assertEqual(self.showcase_service.calls, [])
                self.assertIsNone(
                    self.repository.get_uid(TEST_GUILD_ID, FakeUser.id, "hsr")
                )
                self.assertIn("9자리", interaction.followup.messages[0][0][0])

    async def test_a_uid_that_does_not_exist_is_refused_at_registration(self):
        interaction = _ProfileInteraction()

        await game_profile_cog.GameProfileCog._register.callback(
            self.cog, interaction, _choice("hsr"), "800333171"
        )

        self.assertIsNone(self.repository.get_uid(TEST_GUILD_ID, FakeUser.id, "hsr"))
        self.assertIn("❌", interaction.followup.messages[0][0][0])

    async def test_empty_showcase_registers_but_explains_the_game_menu(self):
        self.showcase_service.results[("zzz", "1000032854")] = _showcase(
            characters=[]
        )
        interaction = _ProfileInteraction()

        await game_profile_cog.GameProfileCog._register.callback(
            self.cog, interaction, _choice("zzz"), "1000032854"
        )

        self.assertEqual(
            self.repository.get_uid(TEST_GUILD_ID, FakeUser.id, "zzz"), "1000032854"
        )
        text = interaction.followup.messages[0][0][0]
        self.assertIn(enka_profiles.ADAPTERS["zzz"].showcase_help, text)

    async def test_uids_are_partitioned_per_guild(self):
        self.showcase_service.results[("hsr", "800333171")] = _showcase()
        self.showcase_service.results[("hsr", "800000001")] = _showcase()

        await game_profile_cog.GameProfileCog._register.callback(
            self.cog, _ProfileInteraction(guild_id=1), _choice("hsr"), "800333171"
        )
        await game_profile_cog.GameProfileCog._register.callback(
            self.cog, _ProfileInteraction(guild_id=2), _choice("hsr"), "800000001"
        )

        self.assertEqual(self.repository.get_uid(1, FakeUser.id, "hsr"), "800333171")
        self.assertEqual(self.repository.get_uid(2, FakeUser.id, "hsr"), "800000001")

        await self.cog.on_guild_remove(SimpleNamespace(id=1))

        self.assertIsNone(self.repository.get_uid(1, FakeUser.id, "hsr"))
        self.assertEqual(self.repository.get_uid(2, FakeUser.id, "hsr"), "800000001")

    async def test_unregister_reports_whether_anything_was_removed(self):
        self.repository.set_uid(TEST_GUILD_ID, FakeUser.id, "gi", "618285856")

        first = _ProfileInteraction()
        await game_profile_cog.GameProfileCog._unregister.callback(
            self.cog, first, _choice("gi")
        )
        second = _ProfileInteraction()
        await game_profile_cog.GameProfileCog._unregister.callback(
            self.cog, second, _choice("gi")
        )

        self.assertIn("지웠습니다", first.response.messages[0][0][0])
        self.assertIn("없습니다", second.response.messages[0][0][0])
        self.assertIsNone(self.repository.get_uid(TEST_GUILD_ID, FakeUser.id, "gi"))


class ProfileCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = SQLiteGameUidRepository(
            pathlib.Path(self.directory.name) / "profile.db"
        )
        self.showcase_service = _StubShowcaseService()
        self.cog = game_profile_cog.GameProfileCog(
            bot=None,
            game_uid_repository=self.repository,
            showcase_service=self.showcase_service,
        )

    async def _run_card(self, game="hsr", interaction=None):
        interaction = interaction or _ProfileInteraction()
        await game_profile_cog.GameProfileCog._card.callback(
            self.cog, interaction, _choice(game)
        )
        return interaction

    def _seed(self):
        self.repository.set_uid(TEST_GUILD_ID, FakeUser.id, "hsr", "800333171")
        self.showcase_service.results[("hsr", "800333171")] = _showcase(
            nickname="Visions",
            level=70,
            characters=[_character("에버나이트", 80), _character("은랑", 79)],
        )

    def test_command_is_named_game_card(self):
        names = {command.name for command in self.cog.get_app_commands()}

        self.assertIn("게임카드", names)
        self.assertNotIn("프로필카드", names)

    async def test_card_shows_only_the_first_character_with_a_large_image(self):
        self._seed()
        interaction = await self._run_card()

        embed = interaction.followup.messages[0][1]["embed"]
        self.assertEqual(embed.title, "테스트 유저 · 붕괴: 스타레일")
        self.assertEqual(embed.description, "Visions Lv.70\n에버나이트 Lv.80")
        self.assertNotIn("은랑", embed.description)
        self.assertEqual(embed.image.url, "https://example.invalid/a.png")

    async def test_empty_showcase_explains_the_in_game_menu(self):
        """UID가 멀쩡해도 진열장이 비어 있을 수 있다. 그때가 최우선 UX다."""
        self.repository.set_uid(TEST_GUILD_ID, FakeUser.id, "gi", "618285856")
        self.showcase_service.results[("gi", "618285856")] = _showcase(
            characters=[]
        )

        interaction = await self._run_card("gi")

        embed = interaction.followup.messages[0][1]["embed"]
        self.assertIn("진열장이 비어 있습니다", embed.description)
        self.assertIn(enka_profiles.ADAPTERS["gi"].showcase_help, embed.description)

    async def test_showcase_help_is_written_per_game(self):
        helps = {a.key: a.showcase_help for a in enka_profiles.ADAPTERS.values()}
        self.assertEqual(len(set(helps.values())), len(helps))

    async def test_card_without_registration_points_at_the_register_command(self):
        interaction = await self._run_card()

        self.assertEqual(self.showcase_service.calls, [])
        self.assertIn("/등록", interaction.followup.messages[0][0][0])
        self.assertTrue(interaction.followup.messages[0][1]["ephemeral"])

    async def test_lookup_failure_is_reported_without_leaking_internals(self):
        self.repository.set_uid(TEST_GUILD_ID, FakeUser.id, "hsr", "800333171")
        self.showcase_service.results[("hsr", "800333171")] = enka_profiles.ShowcaseLookupError(
            "Enka Network에 연결하지 못했습니다."
        )

        interaction = await self._run_card()

        text = interaction.followup.messages[0][0][0]
        self.assertIn("Enka Network에 연결하지 못했습니다.", text)
        self.assertTrue(interaction.followup.messages[0][1]["ephemeral"])

    async def test_card_defers_before_any_network_work(self):
        self.repository.set_uid(TEST_GUILD_ID, FakeUser.id, "hsr", "800333171")
        self.showcase_service.results[("hsr", "800333171")] = _showcase()

        interaction = await self._run_card()


        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.messages, [])

    async def test_registration_is_read_from_this_guild_only(self):
        self.repository.set_uid(1, FakeUser.id, "hsr", "800333171")
        self.showcase_service.results[("hsr", "800333171")] = _showcase()

        elsewhere = await self._run_card(interaction=_ProfileInteraction(guild_id=2))

        self.assertIn("/등록", elsewhere.followup.messages[0][0][0])


class ShowcaseServiceTests(unittest.IsolatedAsyncioTestCase):
    """네트워크 계층의 캐시·백오프. enka 클라이언트는 대역으로 세운다."""

    class _FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0
            self.closed = False

        async def start(self):
            pass

        async def fetch_showcase(self, uid):
            self.calls += 1
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        async def close(self):
            self.closed = True

    def _service(self, responses, convert=None):
        client = self._FakeClient(responses)
        adapter = dataclasses.replace(
            enka_profiles.ADAPTERS["hsr"],
            _client_factory=lambda: client,
            _showcase_converter=convert or (lambda response: response),
        )
        return enka_profiles.ShowcaseService({"hsr": adapter}), client

    async def test_repeat_lookups_inside_the_window_hit_the_cache(self):
        showcase = _showcase()
        service, client = self._service([showcase])

        first = await service.fetch_showcase("hsr", "800333171")
        second = await service.fetch_showcase("hsr", "800333171")

        self.assertIs(first, second)
        self.assertEqual(client.calls, 1)

    async def test_expired_cache_refetches(self):
        service, client = self._service([_showcase("A"), _showcase("B")])
        await service.fetch_showcase("hsr", "800333171")
        # 창이 지난 상황을 시계 대신 만료 시각으로 만든다.
        expires_at, showcase = service._cache[("hsr", "800333171")]
        service._cache[("hsr", "800333171")] = (
            expires_at - enka_profiles.CACHE_TTL_SECONDS - 1,
            showcase,
        )

        again = await service.fetch_showcase("hsr", "800333171")

        self.assertEqual(again.nickname, "B")
        self.assertEqual(client.calls, 2)

    async def test_missing_player_does_not_trip_the_backoff(self):
        import enka

        service, _ = self._service([enka.errors.PlayerDoesNotExistError()] * 4)
        for _ in range(4):
            with self.assertRaises(enka_profiles.ShowcaseLookupError):
                await service.fetch_showcase("hsr", "800333171")
        self.assertEqual(service._backoff_by_game, {})

    async def test_repeated_transport_failures_back_off(self):
        import enka

        service, client = self._service(
            [enka.errors.GeneralServerError()] * enka_profiles.BACKOFF_FAILURE_THRESHOLD
        )
        for _ in range(enka_profiles.BACKOFF_FAILURE_THRESHOLD):
            with self.assertRaises(enka_profiles.ShowcaseLookupError):
                await service.fetch_showcase("hsr", "800333171")

        calls_before = client.calls
        with self.assertRaises(enka_profiles.ShowcaseLookupError) as caught:
            await service.fetch_showcase("hsr", "800333171")

        self.assertIn("잠시 쉬는 중", str(caught.exception))
        self.assertEqual(client.calls, calls_before)

    async def test_cache_is_bounded(self):
        responses = [_showcase(str(n)) for n in range(enka_profiles.CACHE_MAX_ENTRIES + 5)]
        service, _ = self._service(responses)
        for index in range(enka_profiles.CACHE_MAX_ENTRIES + 5):
            await service.fetch_showcase("hsr", f"80000{index:04d}")
        self.assertLessEqual(len(service._cache), enka_profiles.CACHE_MAX_ENTRIES)

    async def test_close_releases_the_client(self):
        service, client = self._service([_showcase()])
        await service.fetch_showcase("hsr", "800333171")
        await service.close()
        self.assertTrue(client.closed)

    async def test_unknown_game_is_refused(self):
        service, _ = self._service([])
        with self.assertRaises(enka_profiles.ShowcaseLookupError):
            service.get_adapter("wuwa")

    def test_construction_touches_no_network(self):
        """회선 없이도 봇은 기동해야 한다. 클라이언트는 첫 조회에서 열린다."""
        self.assertEqual(enka_profiles.ShowcaseService()._clients, {})

    def test_profile_extension_needs_no_api_key(self):
        env = {"ADMIN_TOKEN": None, "OPENAI_API_KEY": None, "GOOGLE_API_KEY": None}
        with patch.object(bot_main, "OPTIONAL_DEPENDENCY_VALUES", env):
            self.assertIn("module.game_profile_cog", bot_main.available_extensions())


class _WebSettingsRepository:
    def __init__(self, guild_ids=(), channels=None):
        self.guild_ids = list(guild_ids)
        self.channels = dict(channels or {})
        self.party_channels = {}
        self.event_channels = {}
        self.forbidden_filters = {}

    def list_announcement_guild_ids(self):
        return list(self.guild_ids)

    def get_party_channel(self, guild_id):
        value = self.party_channels.get(guild_id)
        if isinstance(value, Exception):
            raise value
        return value

    def get_announcement_channel(self, guild_id):
        value = self.channels.get(guild_id)
        if isinstance(value, Exception):
            raise value
        return value

    def get_event_channel(self, guild_id):
        value = self.event_channels.get(guild_id)
        if isinstance(value, Exception):
            raise value
        return value

    def set_event_channel(self, guild_id, channel_id):
        self.event_channels[guild_id] = channel_id

    def set_party_channel(self, guild_id, channel_id):
        value = self.party_channels.get(guild_id)
        if isinstance(value, Exception):
            raise value
        self.party_channels[guild_id] = channel_id

    def set_announcement_channel(self, guild_id, channel_id):
        self.channels[guild_id] = channel_id

    def set_forbidden_filter_enabled(self, guild_id, enabled):
        self.forbidden_filters[guild_id] = enabled

    def get_allow_host_announce(self, guild_id):
        return guild_id in self.guild_ids

    def get_forbidden_filter_enabled(self, guild_id):
        return self.forbidden_filters.get(guild_id, True)

    def set_guild_settings(
        self,
        guild_id,
        party_channel_id,
        announcement_channel_id,
        event_channel_id,
        allow_host_announce,
        forbidden_filter_enabled,
    ):
        self.party_channels[guild_id] = party_channel_id
        self.channels[guild_id] = announcement_channel_id
        self.event_channels[guild_id] = event_channel_id
        if allow_host_announce:
            if guild_id not in self.guild_ids:
                self.guild_ids.append(guild_id)
        elif guild_id in self.guild_ids:
            self.guild_ids.remove(guild_id)
        self.forbidden_filters[guild_id] = forbidden_filter_enabled


class _WebBot:
    def __init__(self, user=None):
        self.guilds = []
        self.cogs = {}
        # READY 이전에는 bot.user가 None이다.
        self.user = user

    def get_guild(self, guild_id):
        return next((guild for guild in self.guilds if guild.id == guild_id), None)

    def get_cog(self, name):
        return self.cogs.get(name)


class WebAdminExtensionTests(unittest.IsolatedAsyncioTestCase):
    def test_web_admin_is_the_only_extension_skipped_without_admin_token(self):
        env = {
            "ADMIN_TOKEN": None,
            "OPENAI_API_KEY": "openai",
            "GOOGLE_API_KEY": "google",
        }
        with patch.object(bot_main, "OPTIONAL_DEPENDENCY_VALUES", env):
            names = bot_main.available_extensions()
        self.assertNotIn("module.web_admin_cog", names)
        self.assertIn("module.guild_settings_cog", names)
        self.assertIn("module.ai_chat_cog", names)

    def test_web_admin_loads_only_with_admin_token(self):
        env = {
            "ADMIN_TOKEN": "secret",
            "OPENAI_API_KEY": None,
            "GOOGLE_API_KEY": None,
        }
        with patch.object(bot_main, "OPTIONAL_DEPENDENCY_VALUES", env):
            names = bot_main.available_extensions()
        self.assertIn("module.web_admin_cog", names)
        self.assertNotIn("module.ai_chat_cog", names)

    async def test_web_admin_defaults_to_loopback_and_accepts_container_bind(self):
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        cog = web_admin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.dict(os.environ, {}, clear=True), patch.object(
            web_admin_cog.web, "AppRunner", return_value=runner
        ), patch.object(web_admin_cog.web, "TCPSite", return_value=site) as site_factory:
            await cog.start()
            site_factory.assert_called_once_with(runner, "127.0.0.1", 8080)
            await cog.cog_unload()
            await cog.close()  # bot close after extension unload
        runner.cleanup.assert_awaited_once()

        container_runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        container_site = SimpleNamespace(start=AsyncMock())
        container_cog = web_admin_cog.WebAdminCog(
            _WebBot(), _WebSettingsRepository()
        )
        with patch.dict(os.environ, {"WEB_ADMIN_HOST": "0.0.0.0"}, clear=True), patch.object(
            web_admin_cog.web, "AppRunner", return_value=container_runner
        ), patch.object(
            web_admin_cog.web, "TCPSite", return_value=container_site
        ) as site_factory:
            await container_cog.start()
            site_factory.assert_called_once_with(container_runner, "0.0.0.0", 8080)
            await container_cog.close()

    async def test_web_admin_rejects_unsupported_bind_host(self):
        cog = web_admin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.dict(os.environ, {"WEB_ADMIN_HOST": "192.0.2.1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "WEB_ADMIN_HOST"):
                await cog.start()

    async def test_bot_close_cleans_runner_before_isolated_superclass_close(self):
        events = []
        runner = SimpleNamespace(
            cleanup=AsyncMock(side_effect=lambda: events.append("runner"))
        )
        cog = web_admin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        cog.web_runner = runner
        bot = object.__new__(bot_main.HyacineBot)
        superclass_close = AsyncMock(side_effect=lambda: events.append("super"))
        with patch.object(bot_main.HyacineBot, "get_cog", return_value=cog), patch.object(
            commands.Bot, "close", superclass_close
        ):
            await bot.close()
        runner.cleanup.assert_awaited_once_with()
        superclass_close.assert_awaited_once_with()
        self.assertEqual(events, ["runner", "super"])

    async def test_cancelled_start_cleans_unowned_runner(self):
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock(side_effect=asyncio.CancelledError))
        cog = web_admin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.object(web_admin_cog.web, "AppRunner", return_value=runner), patch.object(
            web_admin_cog.web, "TCPSite", return_value=site
        ):
            with self.assertRaises(asyncio.CancelledError):
                await cog.start()
        runner.cleanup.assert_awaited_once_with()

    async def test_cancelled_runner_setup_is_cleaned(self):
        runner = SimpleNamespace(
            setup=AsyncMock(side_effect=asyncio.CancelledError), cleanup=AsyncMock()
        )
        cog = web_admin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.object(web_admin_cog.web, "AppRunner", return_value=runner):
            with self.assertRaises(asyncio.CancelledError):
                await cog.start()
        runner.cleanup.assert_awaited_once_with()

    async def test_cancelled_cog_registration_cleans_started_runner(self):
        cog = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        bot = SimpleNamespace(add_cog=AsyncMock(side_effect=asyncio.CancelledError))
        with patch.object(web_admin_cog, "WebAdminCog", return_value=cog):
            with self.assertRaises(asyncio.CancelledError):
                await web_admin_cog.setup(bot)
        cog.close.assert_awaited_once_with()


class WebAdminAtomicSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings_dir = pathlib.Path(self.directory.name) / "settings"
        self.settings_dir.mkdir()
        self.patch = patch.object(config, "SETTINGS_DIR", self.settings_dir)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.directory.cleanup)

    def test_valid_write_flushes_replaces_and_syncs_directory_in_order(self):
        target = self.settings_dir / "forbidden_words.json"
        target.write_bytes(b'["old"]\n')
        events = []
        real_fsync = config.os.fsync
        real_replace = config.os.replace
        real_named_temporary = config.tempfile.NamedTemporaryFile
        file_descriptor = None
        file_mode = None

        class RecordingTemporaryFile:
            def __init__(self, fp):
                self.fp = fp

            @property
            def name(self):
                return self.fp.name

            @property
            def closed(self):
                return self.fp.closed

            def __enter__(self):
                self.fp.__enter__()
                return self

            def __exit__(self, *args):
                return self.fp.__exit__(*args)

            def write(self, payload):
                return self.fp.write(payload)

            def flush(self):
                events.append("flush")
                return self.fp.flush()

            def fileno(self):
                return self.fp.fileno()

            def close(self):
                return self.fp.close()

            def __getattr__(self, name):
                return getattr(self.fp, name)

        def record_named_temporary(*args, **kwargs):
            nonlocal file_descriptor, file_mode
            fp = real_named_temporary(*args, **kwargs)
            file_descriptor = fp.fileno()
            file_mode = os.fstat(file_descriptor).st_mode & 0o777
            return RecordingTemporaryFile(fp)

        def record_fsync(fd):
            events.append("file-fsync" if fd == file_descriptor else "directory-fsync")
            return real_fsync(fd)

        def record_replace(source, destination, **kwargs):
            events.append("replace")
            self.assertEqual(kwargs["src_dir_fd"], kwargs["dst_dir_fd"])
            self.assertEqual(pathlib.Path(source).name, source)
            self.assertEqual(destination, "forbidden_words.json")
            return real_replace(source, destination, **kwargs)

        with patch.object(config.os, "fsync", side_effect=record_fsync), patch.object(
            config.os, "replace", side_effect=record_replace
        ), patch.object(
            config.tempfile, "NamedTemporaryFile", side_effect=record_named_temporary
        ) as opened:
            config.atomic_write_settings("forbidden_words.json", ["new"])

        self.assertEqual(
            events, ["flush", "file-fsync", "replace", "directory-fsync"]
        )
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), ["new"])
        self.assertEqual(opened.call_args.kwargs["dir"], self.settings_dir)
        self.assertIs(opened.call_args.kwargs["delete"], False)
        self.assertEqual(file_mode, 0o600)
        self.assertEqual(list(self.settings_dir.glob(".forbidden_words.json.*")), [])

    def test_loader_valid_games_default_roles_is_accepted_and_invalid_types_rejected(self):
        document = {"Game": {"max_players": 2}}
        config.atomic_write_settings("games.json", document)
        self.assertEqual(
            json.loads((self.settings_dir / "games.json").read_text(encoding="utf-8")),
            document,
        )
        with patch.object(config, "SETTINGS_DIR", self.settings_dir):
            self.assertEqual(
                config.load_games(),
                {"Game": {"max_players": 2, "roles": []}},
            )
        original = (self.settings_dir / "games.json").read_bytes()
        for invalid in (
            {"Game": {"max_players": True}},
            {"Game": {"max_players": 2, "roles": [1]}},
            {"Game": "invalid"},
        ):
            with self.subTest(document=invalid), self.assertRaises(ValueError):
                config.atomic_write_settings("games.json", invalid)
            self.assertEqual((self.settings_dir / "games.json").read_bytes(), original)

    def test_persona_and_forbidden_validation_share_loader_semantics(self):
        config.atomic_write_settings("persona.json", {"greeting": "hello"})
        self.assertEqual(
            json.loads((self.settings_dir / "persona.json").read_text()),
            {"greeting": "hello"},
        )
        config.atomic_write_settings("forbidden_words.json", [1, "", "Word"])
        self.assertEqual(
            json.loads((self.settings_dir / "forbidden_words.json").read_text()),
            ["1", "word"],
        )
        with patch.object(config, "SETTINGS_DIR", self.settings_dir):
            self.assertEqual(forbidden_filter_cog.load_forbidden_words(), ["1", "word"])
        self.assertEqual(
            forbidden_filter_cog.canonicalize_forbidden_words([1, {}, ""]),
            ["1"],
        )
        for name, invalid in (
            ("persona.json", []),
            ("persona.json", {"greeting": 3}),
            ("forbidden_words.json", {}),
        ):
            with self.subTest(name=name, document=invalid), self.assertRaises(ValueError):
                config.atomic_write_settings(name, invalid)

    def test_invalid_disallowed_and_symlink_documents_preserve_bytes(self):
        target = self.settings_dir / "games.json"
        original = b'{"Good":{"max_players":2,"roles":[]}}\n'
        target.write_bytes(original)
        with self.assertRaises(ValueError):
            config.atomic_write_settings("games.json", {"Bad": "not an object"})
        self.assertEqual(target.read_bytes(), original)
        with self.assertRaises(ValueError):
            config.atomic_write_settings("../games.json", {})
        self.assertEqual(target.read_bytes(), original)

        link = self.settings_dir / "persona.json"
        link.symlink_to(target)
        self.assertEqual(
            web_admin_cog.WebAdminCog._settings_text("ko", "persona.json"),
            ("", ""),
        )
        with self.assertRaises(ValueError):
            config.atomic_write_settings("persona.json", {})
        self.assertEqual(target.read_bytes(), original)

    def test_oversized_output_and_replace_failure_preserve_original(self):
        target = self.settings_dir / "forbidden_words.json"
        original = b'["old"]\n'
        target.write_bytes(original)
        with self.assertRaises(ValueError):
            config.atomic_write_settings(
                "forbidden_words.json", ["x" * config.MAX_SETTINGS_BYTES]
            )
        self.assertEqual(target.read_bytes(), original)

        with patch.object(config.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                config.atomic_write_settings("forbidden_words.json", ["valid"])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.settings_dir.glob(".forbidden_words.json.*")), [])

    def test_directory_replacement_cannot_redirect_atomic_write_or_read(self):
        target = self.settings_dir / "forbidden_words.json"
        target.write_bytes(b'["old"]\n')
        moved = self.settings_dir.with_name("pinned-settings")
        real_named_temporary = config.tempfile.NamedTemporaryFile

        def swap_after_create(*args, **kwargs):
            temporary_file = real_named_temporary(*args, **kwargs)
            self.settings_dir.rename(moved)
            self.settings_dir.mkdir()
            return temporary_file

        with patch.object(
            config.tempfile, "NamedTemporaryFile", side_effect=swap_after_create
        ):
            config.atomic_write_settings("forbidden_words.json", ["new"])
        self.assertEqual(json.loads((moved / "forbidden_words.json").read_text()), ["new"])
        self.assertFalse((self.settings_dir / "forbidden_words.json").exists())

        (self.settings_dir / "persona.json").write_text('{"greeting":"decoy"}')
        real_open_directory = config._open_settings_directory

        def swap_before_read(*, create):
            directory_fd = real_open_directory(create=create)
            replacement = self.settings_dir.with_name("decoy-settings")
            self.settings_dir.rename(replacement)
            moved.rename(self.settings_dir)
            return directory_fd

        with patch.object(config, "_open_settings_directory", side_effect=swap_before_read):
            data = config.read_settings_bytes("persona.json")
        self.assertEqual(data, b'{"greeting":"decoy"}')

    def test_settings_directory_must_be_process_owned_and_private(self):
        target = self.settings_dir / "forbidden_words.json"
        target.write_bytes(b'["old"]\n')
        self.settings_dir.chmod(0o777)
        try:
            with self.assertRaises(PermissionError):
                config.atomic_write_settings("forbidden_words.json", ["new"])
            with self.assertRaises(PermissionError):
                config.read_settings_bytes("forbidden_words.json")
        finally:
            self.settings_dir.chmod(0o700)
        with patch.object(config.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(PermissionError):
                config.atomic_write_settings("forbidden_words.json", ["new"])
        self.assertEqual(target.read_bytes(), b'["old"]\n')

    def test_temporary_entry_inode_mismatch_fails_closed_and_cleans_up(self):
        target = self.settings_dir / "forbidden_words.json"
        original = b'["old"]\n'
        target.write_bytes(original)
        real_stat = config.os.stat
        temporary_stats = 0

        def substituted_stat(path, *args, **kwargs):
            nonlocal temporary_stats
            result = real_stat(path, *args, **kwargs)
            if str(path).startswith(".forbidden_words.json."):
                temporary_stats += 1
                if temporary_stats == 2:
                    values = list(result)
                    values[1] += 1
                    return os.stat_result(values)
            return result

        with patch.object(config.os, "stat", side_effect=substituted_stat):
            with self.assertRaises(RuntimeError):
                config.atomic_write_settings("forbidden_words.json", ["new"])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.settings_dir.glob(".forbidden_words.json.*")), [])

    def test_cleanup_closes_directory_fd_even_when_temp_unlink_fails(self):
        captured = []
        real_open_directory = config._open_settings_directory

        def record_directory(*, create):
            directory_fd = real_open_directory(create=create)
            captured.append(directory_fd)
            return directory_fd

        with patch.object(config, "_open_settings_directory", side_effect=record_directory), patch.object(
            config.os, "replace", side_effect=OSError("replace failed")
        ), patch.object(config.os, "unlink", side_effect=PermissionError("unlink failed")):
            with self.assertRaises(PermissionError):
                config.atomic_write_settings("forbidden_words.json", ["new"])
        with self.assertRaises(OSError):
            os.fstat(captured[0])

    def test_named_temporary_context_failure_closes_and_unlinks(self):
        real_named_temporary = config.tempfile.NamedTemporaryFile
        temporary_file = real_named_temporary(
            mode="wb", dir=self.settings_dir, delete=False
        )

        class BrokenContext:
            name = temporary_file.name

            @property
            def closed(self):
                return temporary_file.closed

            def close(self):
                temporary_file.close()

            def __enter__(self):
                raise OSError("wrapper failed")

            def __exit__(self, *args):
                return False

        with patch.object(
            config.tempfile, "NamedTemporaryFile", return_value=BrokenContext()
        ):
            with self.assertRaises(OSError):
                config.atomic_write_settings("forbidden_words.json", ["new"])
        self.assertTrue(temporary_file.closed)
        self.assertFalse(pathlib.Path(temporary_file.name).exists())


class WebAdminI18nCatalogTests(unittest.TestCase):
    def test_both_languages_define_exactly_the_same_keys(self):
        # 키가 빠지면 fallback으로 키 이름이 화면에 드러난다. 불완전한 번역을
        # 배포 전에 잡도록 두 언어의 키 집합을 정확히 맞춘다.
        korean = set(i18n.STRINGS["ko"])
        english = set(i18n.STRINGS["en"])
        self.assertEqual(korean, english, korean ^ english)
        self.assertEqual(set(i18n.STRINGS), set(i18n.SUPPORTED_LANGUAGES))

    def test_no_catalog_entry_is_empty(self):
        for language, catalog in i18n.STRINGS.items():
            for key, value in catalog.items():
                self.assertTrue(value.strip(), f"{language}:{key}")

    def test_format_placeholders_match_across_languages(self):
        # {reason}이 한쪽에만 있으면 그 언어에서 원인이 통째로 사라진다.
        fields = lambda s: set(re.findall(r"\{([a-z_]+)\}", s))
        for key, korean in i18n.STRINGS["ko"].items():
            self.assertEqual(
                fields(korean), fields(i18n.STRINGS["en"][key]), key
            )

    def test_every_template_key_exists_in_the_catalog(self):
        used = set()
        for name in ("admin_index.html", "admin_login.html"):
            source = (web_admin_cog.TEMPLATE_DIR / name).read_text(encoding="utf-8")
            used |= set(re.findall(r"\{\{t:([a-z0-9_]+)\}\}", source))
        self.assertTrue(used)
        self.assertEqual(used - set(i18n.STRINGS["ko"]), set())

    def test_every_catalog_key_is_actually_used(self):
        used = set()
        for name in ("admin_index.html", "admin_login.html"):
            used |= set(
                re.findall(
                    r"\{\{t:([a-z0-9_]+)\}\}",
                    (web_admin_cog.TEMPLATE_DIR / name).read_text(encoding="utf-8"),
                )
            )
        source = pathlib.Path(
            inspect.getsourcefile(web_admin_cog)
        ).read_text(encoding="utf-8")
        used |= set(re.findall(r"""["']([a-z0-9_]+)["']""", source))
        unused = set(i18n.STRINGS["ko"]) - used
        self.assertEqual(unused, set(), f"쓰이지 않는 키: {sorted(unused)}")

    def test_language_resolution_order_and_fallback(self):
        def request(query=None, cookies=None, headers=None):
            return SimpleNamespace(
                query=query or {}, cookies=cookies or {}, headers=headers or {}
            )

        self.assertEqual(i18n.resolve_language(request()), "ko")
        self.assertEqual(
            i18n.resolve_language(request(query={"lang": "en"})), "en"
        )
        # 쿼리가 쿠키를 이긴다.
        self.assertEqual(
            i18n.resolve_language(
                request(query={"lang": "ko"}, cookies={"admin_lang": "en"})
            ),
            "ko",
        )
        # 쿠키가 헤더를 이긴다.
        self.assertEqual(
            i18n.resolve_language(
                request(
                    cookies={"admin_lang": "en"},
                    headers={"Accept-Language": "ko-KR"},
                )
            ),
            "en",
        )
        self.assertEqual(
            i18n.resolve_language(
                request(headers={"Accept-Language": "en-GB,en;q=0.9"})
            ),
            "en",
        )
        # 알 수 없는 값은 조용히 기본값으로.
        for bad in ("fr", "", "EN", "en; DROP TABLE"):
            self.assertEqual(i18n.resolve_language(request(query={"lang": bad})), "ko", bad)
        # 미들웨어의 500 경로는 어떤 객체가 와도 터지면 안 된다.
        self.assertEqual(i18n.resolve_language(object()), "ko")

    def test_translate_falls_back_instead_of_raising(self):
        self.assertEqual(i18n.translate("en", "no_such_key"), "no_such_key")
        self.assertEqual(i18n.translate("fr", "logout"), i18n.STRINGS["ko"]["logout"])
        # 파라미터가 모자라도 500이 아니라 원문이 나온다.
        self.assertEqual(
            i18n.translate("ko", "notice_save_failed"),
            i18n.STRINGS["ko"]["notice_save_failed"],
        )


class WebAdminStylesheetTests(unittest.TestCase):
    def setUp(self):
        self.css = web_admin_cog.STYLESHEET_PATH.read_text(encoding="utf-8")

    def test_stylesheet_holds_the_design_tokens_once(self):
        # 토큰이 한 곳에만 있어야 두 화면의 색이 갈라지지 않는다.
        self.assertIn("--blurple: #5865f2", self.css)
        self.assertEqual(self.css.count(":root {"), 1)
        self.assertIn("@media (max-width: 960px)", self.css)

    def test_stylesheet_scopes_login_only_rules(self):
        # 두 화면의 .brand-mark 크기가 일부러 다르다. 로그인 규칙이 한정되지
        # 않으면 인덱스 화면을 덮어쓴다.
        self.assertIn("body.login {", self.css)
        self.assertIn(".login .login-shell {", self.css)
        self.assertNotIn("\n.login-shell {", self.css)

    def test_templates_carry_no_inline_style_block(self):
        for name in ("admin_index.html", "admin_login.html"):
            source = (web_admin_cog.TEMPLATE_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("<style", source, name)
            self.assertIn('href="/static/admin.css"', source, name)


class WebAdminTemplateDesignTests(unittest.TestCase):
    def test_login_template_uses_local_discord_design(self):
        login_page = web_admin_cog.WebAdminCog._render_template(
            "admin_login.html", lang="ko", error=""
        )
        self.assertIn('<link rel="stylesheet" href="/static/admin.css">', login_page)
        self.assertNotIn("<style", login_page)
        self.assertIn('<body class="login">', login_page)
        self.assertIn('for="admin-token"', login_page)
        self.assertIn('id="admin-token"', login_page)
        self.assertNotIn("<script", login_page)
        self.assertNotIn("https://", login_page)

    def test_index_template_uses_responsive_design_and_keeps_form_contracts(self):
        page = web_admin_cog.WebAdminCog._render_template(
            "admin_index.html",
            lang="ko",
            csrf="csrf",
            notice="",
            persona_prompt="prompt",
            persona_greeting="hello",
            forbidden_words='["bad"]',
            games='{"Game":{"max_players":2,"roles":[]}}',
            guild_rows=(
                '<tr><td><form action="/guilds/settings">'
                '<select name="party_channel_id"></select>'
                '<select name="announcement_channel_id"></select>'
                '<select name="event_channel_id"></select>'
                '<select name="forbidden_filter_enabled"></select>'
                '<button name="setting" value="party_channel_id"></button>'
                '</form></td></tr>'
            ),
            guild_options='<option value="">전체</option>',
        )
        self.assertIn('<link rel="stylesheet" href="/static/admin.css">', page)
        self.assertNotIn("<style", page)
        self.assertIn('class="shell"', page)
        self.assertIn('href="#main-content"', page)
        for action in (
            "/logout",
            "/settings/persona.json",
            "/settings/forbidden_words.json",
            "/settings/games.json",
            "/guilds/settings",
            "/announce",
        ):
            self.assertIn(f'action="{action}"', page)
        for name in (
            "csrf",
            "system_prompt",
            "greeting",
            "document",
            "guild_id",
            "party_channel_id",
            "announcement_channel_id",
            "event_channel_id",
            "forbidden_filter_enabled",
            "setting",
            "title",
            "body",
            "image",
            "color",
        ):
            self.assertIn(f'name="{name}"', page)
        for control_id in (
            "system-prompt",
            "greeting",
            "forbidden-words",
            "games",
            "guild-select",
            "announce-color",
            "announce-title-input",
            "announce-body",
            "announce-image",
        ):
            self.assertIn(f'for="{control_id}"', page)
            self.assertIn(f'id="{control_id}"', page)
        self.assertIn('scope="col"', page)
        self.assertIn('aria-live="polite"', page)
        self.assertIn("Discord 문법 가이드", page)
        self.assertIn("**굵게**", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("https://", page)


class WebAdminMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_security_headers_middleware_accepts_aiohttp_handler_keyword(self):
        handler = AsyncMock(return_value=web_admin_cog.web.Response(text="ok"))
        request = SimpleNamespace(
            method="GET", path="/", headers={"Host": "127.0.0.1:8080"}
        )

        response = await web_admin_cog._security_headers(
            request,
            handler=handler,
        )

        handler.assert_awaited_once_with(request)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def test_security_middleware_rejects_untrusted_host(self):
        request = SimpleNamespace(
            method="GET", path="/", headers={"Host": "attacker.example"}
        )
        with self.assertRaises(web_admin_cog.web.HTTPBadRequest) as rejected:
            await web_admin_cog._security_headers(
                request, handler=AsyncMock()
            )
        self.assertEqual(
            rejected.exception.headers["X-Content-Type-Options"], "nosniff"
        )

    async def test_unhandled_web_error_reports_only_the_core_cause(self):
        async def handler(_):
            raise RuntimeError("secret internal detail")

        request = SimpleNamespace(
            method="POST",
            path="/guilds/settings",
            headers={"Host": "127.0.0.1"},
        )
        with patch.object(web_admin_cog.logger, "exception"):
            response = await web_admin_cog._security_headers(request, handler)

        self.assertEqual(response.status, 500)
        self.assertIn("예상하지 못한 서버 오류 (RuntimeError)", response.text)
        self.assertNotIn("secret internal detail", response.text)

    async def test_guild_settings_handler_persists_selected_setting(self):
        permissions = SimpleNamespace(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
        )
        channel = SimpleNamespace(
            id=77,
            name="events",
            type=discord.ChannelType.text,
            permissions_for=lambda member: permissions,
        )
        guild = SimpleNamespace(
            id=1,
            name="테스트 길드",
            me=object(),
            get_channel=lambda channel_id: channel if channel_id == 77 else None,
        )
        bot = _WebBot()
        bot.guilds = [guild]
        ensure_panels = AsyncMock()
        bot.cogs = {
            "PartyCog": SimpleNamespace(ensure_panels=ensure_panels),
        }
        repository = _WebSettingsRepository()
        cog = web_admin_cog.WebAdminCog(bot, repository)
        session = web_admin_cog.AdminSession(csrf="csrf", expires_at=1)
        response = web_admin_cog.web.Response(text="ok")

        with patch.object(
            cog,
            "_read_form",
            AsyncMock(
                return_value={
                    "csrf": "csrf",
                    "guild_id": "1",
                    "setting": "party_channel_id",
                    "party_channel_id": "77",
                }
            ),
        ), patch.object(cog, "_require_session"), patch.object(
            cog,
            "_require_csrf",
            return_value=("session", session),
        ), patch.object(
            cog,
            "_index_response",
            AsyncMock(return_value=response),
        ):
            result = await cog.guild_settings_post(object())

        self.assertIs(result, response)
        self.assertEqual(repository.party_channels[1], 77)
        ensure_panels.assert_awaited_once_with(guild)

    async def test_guild_settings_failure_reports_a_sanitized_cause(self):
        guild = SimpleNamespace(id=1, name="테스트 길드", get_channel=lambda _: None)
        bot = _WebBot()
        bot.guilds = [guild]
        repository = _WebSettingsRepository()
        repository.party_channels[1] = RuntimeError("private database path")
        cog = web_admin_cog.WebAdminCog(bot, repository)
        session = web_admin_cog.AdminSession(csrf="csrf", expires_at=1)

        with patch.object(
            cog,
            "_read_form",
            AsyncMock(
                return_value={
                    "csrf": "csrf",
                    "guild_id": "1",
                    "setting": "party_channel_id",
                    "party_channel_id": "",
                }
            ),
        ), patch.object(cog, "_require_session"), patch.object(
            cog,
            "_require_csrf",
            return_value=("session", session),
        ), patch.object(
            cog,
            "_index_response",
            AsyncMock(return_value=web_admin_cog.web.Response(text="ok")),
        ) as render, patch.object(web_admin_cog.logger, "exception"):
            await cog.guild_settings_post(object())

        notice = render.await_args.args[2]
        self.assertIn("예상하지 못한 오류가 발생했습니다 (RuntimeError)", notice)
        self.assertNotIn("private database path", notice)

    async def test_guild_overview_marks_the_selected_event_channel(self):
        permissions = SimpleNamespace(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
        )
        permission_calls = 0

        def permissions_for(_):
            nonlocal permission_calls
            permission_calls += 1
            return permissions

        channel = SimpleNamespace(
            id=77,
            name="events",
            type=discord.ChannelType.text,
            permissions_for=permissions_for,
        )
        guild = SimpleNamespace(
            id=1,
            name="<테스트>",
            me=object(),
            text_channels=[channel],
            get_channel=lambda channel_id: channel if channel_id == 77 else None,
        )
        bot = _WebBot()
        bot.guilds = [guild]
        repository = _WebSettingsRepository()
        repository.party_channels[1] = 77
        repository.channels[1] = 77
        repository.event_channels[1] = 77
        repository.guild_ids = [1]
        repository.forbidden_filters[1] = False
        cog = web_admin_cog.WebAdminCog(bot, repository)

        rows, _, problem = await cog._guild_overview("ko", "csrf")

        self.assertEqual(problem, "")
        self.assertIn('action="/guilds/settings"', rows)
        self.assertEqual(rows.count('<option value="77" selected>#events</option>'), 3)
        self.assertNotIn('name="allow_host_announce"', rows)
        self.assertIn("/설정 공지허용", rows)
        self.assertIn('name="forbidden_filter_enabled"', rows)
        self.assertIn('<option value="0" selected>꺼짐</option>', rows)
        self.assertNotIn("<테스트>", rows)
        self.assertEqual(permission_calls, 1)

    def test_generic_value_error_is_not_reflected(self):
        reason = web_admin_cog.WebAdminCog._operation_error_reason(
            "ko",
            ValueError("private filesystem path")
        )
        self.assertNotIn("private filesystem path", reason)


class WebAdminHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings_dir = pathlib.Path(self.directory.name)
        for name, document in {
            "persona.json": {"system_prompt": "prompt", "greeting": "hello"},
            "forbidden_words.json": ["bad"],
            "games.json": {"Game": {"max_players": 2, "roles": []}},
        }.items():
            (self.settings_dir / name).write_text(
                json.dumps(document), encoding="utf-8"
            )
        self.token_patch = patch.object(config, "ADMIN_TOKEN", "operator-secret")
        self.dir_patch = patch.object(config, "SETTINGS_DIR", self.settings_dir)
        self.token_patch.start()
        self.dir_patch.start()
        self.bot = _WebBot()
        self.repository = _WebSettingsRepository()
        self.cog = web_admin_cog.WebAdminCog(self.bot, self.repository)
        self.client = TestClient(
            TestServer(self.cog.app), cookie_jar=CookieJar(unsafe=True)
        )
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.dir_patch.stop()
        self.token_patch.stop()
        self.directory.cleanup()

    async def _login(self):
        response = await self.client.post(
            "/login", data={"token": "operator-secret"}, allow_redirects=False
        )
        self.assertEqual(response.status, 303)
        session_id = response.cookies[web_admin_cog.SESSION_COOKIE].value
        return session_id, self.cog.admin_sessions[session_id].csrf

    async def test_session_extend_pushes_expiry_and_rebakes_cookie(self):
        session_id, csrf = await self._login()
        before = self.cog.admin_sessions[session_id].expires_at
        # 만료가 가까워진 상태를 만든다.
        self.cog.admin_sessions[session_id] = dataclasses.replace(
            self.cog.admin_sessions[session_id],
            expires_at=time.monotonic() + 60,
        )

        response = await self.client.post(
            "/session/extend", data={"csrf": csrf}, allow_redirects=False
        )
        self.assertEqual((response.status, response.headers["Location"]), (303, "/"))
        self.assertGreaterEqual(
            self.cog.admin_sessions[session_id].expires_at, before
        )
        self.assertEqual(
            response.cookies[web_admin_cog.SESSION_COOKIE]["max-age"],
            str(web_admin_cog.SESSION_TTL_SECONDS),
        )
        # csrf는 그대로여야 열려 있던 폼이 계속 동작한다.
        self.assertEqual(self.cog.admin_sessions[session_id].csrf, csrf)

    async def test_session_extend_requires_csrf_and_a_live_session(self):
        session_id, csrf = await self._login()
        response = await self.client.post("/session/extend", data={})
        self.assertEqual(response.status, 403)
        response = await self.client.post(
            "/session/extend", data={"csrf": "wrong"}
        )
        self.assertEqual(response.status, 403)

        # 만료된 세션의 연장은 401이다.
        self.cog.admin_sessions[session_id] = dataclasses.replace(
            self.cog.admin_sessions[session_id],
            expires_at=time.monotonic() - 1,
        )
        response = await self.client.post("/session/extend", data={"csrf": csrf})
        self.assertEqual(response.status, 401)

    async def test_index_shows_remaining_session_time(self):
        session_id, csrf = await self._login()
        self.cog.admin_sessions[session_id] = dataclasses.replace(
            self.cog.admin_sessions[session_id],
            expires_at=time.monotonic() + 7 * 3600 + 52 * 60,
        )
        page = await (await self.client.get("/")).text()
        self.assertIn("남은 시간: 7시간 51분", page)
        self.assertIn("KST)", page)
        self.assertIn('action="/session/extend"', page)
        # JS는 쓰지 않는다 — CSP를 넓히지 않기로 한 결정이다.
        self.assertNotIn("<script", page)

    async def test_remaining_text_is_empty_without_a_matching_session(self):
        self.assertEqual(self.cog._session_remaining_text("ko", "nobody"), "")

    async def test_lang_query_switches_the_whole_page(self):
        await self._login()
        korean = await (await self.client.get("/")).text()
        self.assertIn("관리자 콘솔", korean)

        english = await (await self.client.get("/?lang=en")).text()
        self.assertIn("Admin console", english)
        self.assertIn('<html lang="en">', english)
        self.assertIn("Send announcement", english)
        self.assertIn("Sign out", english)
        self.assertNotIn("관리자 콘솔", english)

    async def test_language_choice_survives_in_a_cookie(self):
        response = await self.client.get("/login?lang=en")
        self.assertEqual(response.cookies[i18n.LANGUAGE_COOKIE].value, "en")

        # 쿼리 없이 열어도 쿠키가 언어를 유지한다.
        page = await (await self.client.get("/login")).text()
        self.assertIn("Administrator sign-in", page)

        # POST 뒤 리다이렉트된 화면도 마찬가지다.
        await self._login()
        page = await (await self.client.get("/")).text()
        self.assertIn("Admin console", page)

    async def test_accept_language_header_is_the_last_resort(self):
        await self._login()
        page = await (
            await self.client.get("/", headers={"Accept-Language": "en-US,en;q=0.9"})
        ).text()
        self.assertIn("Admin console", page)

    async def test_unknown_lang_falls_back_to_korean_without_a_cookie(self):
        response = await self.client.get("/login?lang=fr")
        self.assertNotIn(i18n.LANGUAGE_COOKIE, response.cookies)
        self.assertIn("관리자 로그인", await response.text())

    async def test_language_switch_links_are_plain_anchors(self):
        page = await (await self.client.get("/login")).text()
        self.assertIn('href="?lang=en"', page)
        self.assertIn('href="?lang=ko"', page)
        # JS를 쓰지 않기로 한 결정이 링크로 지켜지는지 본다.
        self.assertNotIn("<script", page)

    async def test_server_generated_notices_follow_the_language(self):
        _, csrf = await self._login()
        response = await self.client.post(
            "/settings/games.json",
            data={"csrf": csrf, "document": '{"Game":{"max_players":2,"roles":[]}}'},
            params={"lang": "en"},
        )
        body = await response.text()
        self.assertIn("Restart the bot to apply it.", body)
        self.assertNotIn("봇을 재시작하세요", body)

    async def test_error_responses_follow_the_language(self):
        await self._login()
        response = await self.client.post("/announce", data={}, params={"lang": "en"})
        self.assertEqual(response.status, 403)
        self.assertIn("Invalid CSRF token", await response.text())

        response = await self.client.post("/announce", data={}, params={"lang": "ko"})
        self.assertIn("잘못된 CSRF 토큰", await response.text())

    async def test_stylesheet_is_served_without_auth(self):
        # 로그인 화면이 이 스타일시트를 쓰므로 인증 밖이어야 한다.
        response = await self.client.get("/static/admin.css")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/css")
        body = await response.text()
        self.assertIn("--blurple: #5865f2", body)
        for key, value in web_admin_cog.SECURITY_HEADERS.items():
            self.assertEqual(response.headers[key], value)

    async def test_static_route_serves_only_the_one_fixed_path(self):
        # web.static()을 쓰지 않으므로 경로 조작으로 다른 파일에 닿을 수 없다.
        for path in (
            "/static/../web_admin_cog.py",
            "/static/admin.css/../../config.py",
            "/static/",
            "/static/other.css",
        ):
            response = await self.client.get(path, allow_redirects=False)
            self.assertNotEqual(response.status, 200, path)

    async def test_bot_name_falls_back_and_is_escaped(self):
        # READY 이전에는 bot.user가 None이다.
        self.assertEqual(self.cog._bot_name(), "Bot")
        login = await (await self.client.get("/login")).text()
        self.assertIn("<title>Bot 관리자 로그인</title>", login)

        self.bot.user = SimpleNamespace(display_name='<script>x</script>&Bot')
        login = await (await self.client.get("/login")).text()
        self.assertNotIn("<script>x</script>", login)
        self.assertIn("&lt;script&gt;", login)

        await self._login()
        index = await (await self.client.get("/")).text()
        self.assertNotIn("<script>x</script>", index)
        self.assertIn("&lt;script&gt;", index)
        self.assertNotIn("Hyacine", index)

    async def test_bot_name_reaches_both_screens(self):
        self.bot.user = SimpleNamespace(display_name="정원지기")
        login = await (await self.client.get("/login")).text()
        self.assertIn("<title>정원지기 관리자 로그인</title>", login)
        self.assertIn('<span class="brand-name">정원지기</span>', login)
        self.assertIn('aria-hidden="true">정</span>', login)

        await self._login()
        index = await (await self.client.get("/")).text()
        self.assertIn("<title>정원지기 Bot Control Center</title>", index)
        self.assertIn('<div class="breadcrumb">정원지기 / ', index)
        self.assertIn("정원지기 local administration", index)

    async def test_auth_redirect_headers_cookie_and_non_reflection(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual((response.status, response.headers["Location"]), (302, "/login"))
        for key, value in web_admin_cog.SECURITY_HEADERS.items():
            self.assertEqual(response.headers[key], value)

        response = await self.client.post("/announce", data={})
        self.assertEqual(response.status, 401)
        oversized = await self.client.post(
            "/announce",
            data=b"x" * (web_admin_cog.MAX_BODY_BYTES + 1),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(oversized.status, 401)
        malformed = await self.client.post(
            "/settings/games.json",
            data=b"csrf=%ZZ",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(malformed.status, 401)
        failed = await self.client.post(
            "/login", data={"token": "operator-secret-wrong"}
        )
        self.assertEqual(failed.status, 401)
        self.assertNotIn("operator-secret-wrong", await failed.text())

        with patch.object(
            web_admin_cog.secrets,
            "compare_digest",
            wraps=secrets.compare_digest,
        ) as compared:
            session_id, csrf = await self._login()
        compared.assert_called_with(b"operator-secret", b"operator-secret")
        self.assertGreaterEqual(len(session_id), 32)
        self.assertGreaterEqual(len(csrf), 32)
        self.assertNotEqual(session_id, "operator-secret")
        cookie = self.client.session.cookie_jar.filter_cookies(
            self.client.make_url("/")
        )[web_admin_cog.SESSION_COOKIE]
        self.assertNotEqual(cookie.value, "operator-secret")

        index = await self.client.get("/")
        page = await index.text()
        self.assertNotIn("operator-secret", page)

    async def test_login_is_rate_limited_and_untrusted_host_is_rejected(self):
        for _ in range(web_admin_cog.LOGIN_FAILURE_LIMIT):
            failed = await self.client.post(
                "/login", data={"token": "wrong"}
            )
            self.assertEqual(failed.status, 401)
        limited = await self.client.post(
            "/login", data={"token": "operator-secret"}
        )
        self.assertEqual(limited.status, 429)
        self.assertEqual(
            limited.headers["Retry-After"],
            str(web_admin_cog.LOGIN_FAILURE_WINDOW_SECONDS),
        )

        rejected = await self.client.get(
            "/login", headers={"Host": "attacker.example"}
        )
        self.assertEqual(rejected.status, 400)

    async def test_cookie_attributes_csrf_logout_and_restart_memory(self):
        response = await self.client.post(
            "/login", data={"token": "operator-secret"}, allow_redirects=False
        )
        morsel = response.cookies[web_admin_cog.SESSION_COOKIE]
        self.assertTrue(morsel["httponly"])
        self.assertEqual(morsel["samesite"], "Strict")
        self.assertEqual(morsel["path"], "/")
        self.assertEqual(morsel["max-age"], str(web_admin_cog.SESSION_TTL_SECONDS))
        session_id = morsel.value
        csrf = self.cog.admin_sessions[session_id].csrf

        rejected = await self.client.post(
            "/settings/forbidden_words.json", data={"document": "[]"}
        )
        self.assertEqual(rejected.status, 403)
        rejected = await self.client.post(
            "/logout", data={"csrf": "wrong"}, allow_redirects=False
        )
        self.assertEqual(rejected.status, 403)
        self.assertIn(session_id, self.cog.admin_sessions)

        logout = await self.client.post(
            "/logout", data={"csrf": csrf}, allow_redirects=False
        )
        self.assertEqual(logout.status, 303)
        self.assertNotIn(session_id, self.cog.admin_sessions)
        self.assertEqual(
            logout.cookies[web_admin_cog.SESSION_COOKIE]["max-age"], "0"
        )
        restarted = web_admin_cog.WebAdminCog(self.bot, self.repository)
        self.assertEqual(restarted.admin_sessions, {})

    async def test_guild_settings_can_be_set_and_cleared_from_the_admin_page(self):
        _, csrf = await self._login()
        channel = _AnnouncementChannel(name="events")
        guild = _AnnouncementGuild({77: channel}, guild_id=1, name="테스트 길드")
        self.bot.guilds = [guild]
        self.repository.guild_ids = [1]
        self.bot.cogs = {
            "PartyCog": SimpleNamespace(ensure_panels=AsyncMock()),
            "ForbiddenFilterCog": SimpleNamespace(invalidate_guild=Mock()),
        }

        for setting, value in (
            ("party_channel_id", "77"),
            ("announcement_channel_id", "77"),
            ("event_channel_id", "77"),
            ("forbidden_filter_enabled", "0"),
        ):
            selected = await self.client.post(
                "/guilds/settings",
                data={
                    "csrf": csrf,
                    "guild_id": "1",
                    "setting": setting,
                    setting: value,
                },
            )
            self.assertEqual(selected.status, 200)
            self.assertIn("설정을 저장했습니다", await selected.text())
        self.assertEqual(self.repository.party_channels[1], 77)
        self.assertEqual(self.repository.channels[1], 77)
        self.assertEqual(self.repository.event_channels[1], 77)
        self.assertIn(1, self.repository.guild_ids)
        self.assertFalse(self.repository.forbidden_filters[1])

        for setting, value in (
            ("party_channel_id", ""),
            ("announcement_channel_id", ""),
            ("event_channel_id", ""),
            ("forbidden_filter_enabled", "1"),
        ):
            cleared = await self.client.post(
                "/guilds/settings",
                data={
                    "csrf": csrf,
                    "guild_id": "1",
                    "setting": setting,
                    setting: value,
                },
            )
            self.assertEqual(cleared.status, 200)
        self.assertIsNone(self.repository.party_channels[1])
        self.assertIsNone(self.repository.channels[1])
        self.assertIsNone(self.repository.event_channels[1])
        self.assertIn(1, self.repository.guild_ids)
        self.assertTrue(self.repository.forbidden_filters[1])

    async def test_unusable_existing_channel_does_not_block_another_setting(self):
        _, csrf = await self._login()
        self.bot.guilds = [_AnnouncementGuild({}, guild_id=1)]
        self.repository.party_channels[1] = 999

        response = await self.client.post(
            "/guilds/settings",
            data={
                "csrf": csrf,
                "guild_id": "1",
                "setting": "forbidden_filter_enabled",
                "forbidden_filter_enabled": "0",
                "party_channel_id": "999",
            },
        )

        self.assertEqual(response.status, 200)
        self.assertFalse(self.repository.forbidden_filters[1])
        self.assertEqual(self.repository.party_channels[1], 999)

    async def test_party_channel_requires_all_bot_permissions(self):
        _, csrf = await self._login()
        for missing_permission in (
            "view_channel",
            "send_messages",
            "read_message_history",
            "embed_links",
        ):
            with self.subTest(missing_permission=missing_permission):
                blocked = _SetupChannel(
                    77, "party", **{missing_permission: False}
                )
                self.bot.guilds = [
                    _AnnouncementGuild({77: blocked}, guild_id=1)
                ]
                self.repository.party_channels.clear()

                response = await self.client.post(
                    "/guilds/settings",
                    data={
                        "csrf": csrf,
                        "guild_id": "1",
                        "setting": "party_channel_id",
                        "party_channel_id": "77",
                    },
                )

                self.assertIn("권한이 부족합니다", await response.text())
                self.assertNotIn(1, self.repository.party_channels)

    async def test_party_save_repairs_same_channel_and_reports_missing_or_timeout(self):
        _, csrf = await self._login()
        channel = _AnnouncementChannel()
        guild = _AnnouncementGuild({77: channel}, guild_id=1)
        self.bot.guilds = [guild]
        self.repository.party_channels[1] = 77
        ensure_panels = AsyncMock()
        self.bot.cogs["PartyCog"] = SimpleNamespace(
            ensure_panels=ensure_panels
        )
        payload = {
            "csrf": csrf,
            "guild_id": "1",
            "setting": "party_channel_id",
            "party_channel_id": "77",
        }

        repaired = await self.client.post("/guilds/settings", data=payload)
        self.assertIn("설정을 저장했습니다", await repaired.text())
        ensure_panels.assert_awaited_once_with(guild)

        self.bot.cogs.pop("PartyCog")
        missing = await self.client.post("/guilds/settings", data=payload)
        self.assertIn("파티 모듈이 로드되지 않아", await missing.text())

        async def hang(_):
            await asyncio.sleep(3600)

        self.bot.cogs["PartyCog"] = SimpleNamespace(ensure_panels=hang)
        with patch.object(
            web_admin_cog, "PANEL_REFRESH_TIMEOUT_SECONDS", 0.01
        ), patch.object(web_admin_cog.logger, "exception"):
            timed_out = await self.client.post("/guilds/settings", data=payload)
        self.assertIn("작업 시간이 초과되었습니다", await timed_out.text())

    async def test_non_ascii_snowflakes_are_rejected_without_500(self):
        _, csrf = await self._login()
        self.bot.guilds = [_AnnouncementGuild({}, guild_id=1)]
        for payload in (
            {
                "csrf": csrf,
                "guild_id": "²",
                "setting": "forbidden_filter_enabled",
                "forbidden_filter_enabled": "1",
            },
            {
                "csrf": csrf,
                "guild_id": "1",
                "setting": "party_channel_id",
                "party_channel_id": "²",
            },
        ):
            with self.subTest(payload=payload):
                response = await self.client.post(
                    "/guilds/settings", data=payload
                )
                self.assertEqual(response.status, 200)
                self.assertIn("올바르지 않습니다", await response.text())

    async def test_session_expiry_relogin_revocation_and_stale_cookie(self):
        with patch.object(web_admin_cog.time, "monotonic", return_value=100.0):
            first_id, _ = await self._login()
        with patch.object(web_admin_cog.time, "monotonic", return_value=101.0):
            second_id, _ = await self._login()
        self.assertNotEqual(first_id, second_id)
        self.assertNotIn(first_id, self.cog.admin_sessions)
        self.assertEqual(list(self.cog.admin_sessions), [second_id])

        self.client.session.cookie_jar.update_cookies(
            {web_admin_cog.SESSION_COOKIE: first_id}, self.client.make_url("/")
        )
        stale = await self.client.get("/", allow_redirects=False)
        self.assertEqual((stale.status, stale.headers["Location"]), (302, "/login"))

        self.client.session.cookie_jar.update_cookies(
            {web_admin_cog.SESSION_COOKIE: second_id}, self.client.make_url("/")
        )
        with patch.object(
            web_admin_cog.time,
            "monotonic",
            return_value=101.0 + web_admin_cog.SESSION_TTL_SECONDS,
        ):
            expired = await self.client.get("/", allow_redirects=False)
        self.assertEqual((expired.status, expired.headers["Location"]), (302, "/login"))
        self.assertEqual(self.cog.admin_sessions, {})

    async def test_settings_validation_reload_notices_and_html_escape(self):
        session_id, csrf = await self._login()
        original = (self.settings_dir / "games.json").read_bytes()
        invalid = await self.client.post(
            "/settings/games.json",
            data={"csrf": csrf, "document": "{broken"},
        )
        self.assertEqual(invalid.status, 200)
        self.assertEqual((self.settings_dir / "games.json").read_bytes(), original)
        denied = await self.client.post(
            "/settings/not-allowed.json",
            data={"csrf": csrf, "document": "{}"},
        )
        self.assertEqual(denied.status, 404)

        reload_cog = SimpleNamespace(reload_forbidden_words=AsyncMock())
        self.bot.cogs["ForbiddenFilterCog"] = reload_cog
        saved = await self.client.post(
            "/settings/forbidden_words.json",
            data={"csrf": csrf, "document": '["new"]'},
        )
        self.assertIn("다시 불러왔습니다", await saved.text())
        reload_cog.reload_forbidden_words.assert_awaited_once_with()

        persona = await self.client.post(
            "/settings/persona.json",
            data={
                "csrf": csrf,
                "document": json.dumps({"system_prompt": "p", "greeting": "g"}),
            },
        )
        self.assertIn("새 AI 세션부터", await persona.text())
        games = await self.client.post(
            "/settings/games.json",
            data={"csrf": csrf, "document": '{"Game":{"max_players":2}}'},
        )
        self.assertIn("봇을 재시작", await games.text())

        (self.settings_dir / "persona.json").write_text(
            json.dumps(
                {"system_prompt": "</textarea><script>x</script>", "greeting": "hi"}
            ),
            encoding="utf-8",
        )
        index = await self.client.get("/")
        page = await index.text()
        self.assertNotIn("</textarea><script>", page)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", page)
        self.assertIn(session_id, self.cog.admin_sessions)

    async def test_forbidden_file_write_and_reload_are_serialized(self):
        _, csrf = await self._login()
        first_reload_started = asyncio.Event()
        release_first_reload = asyncio.Event()
        reload_count = 0

        async def reload_forbidden_words():
            nonlocal reload_count
            reload_count += 1
            if reload_count == 1:
                first_reload_started.set()
                await release_first_reload.wait()

        self.bot.cogs["ForbiddenFilterCog"] = SimpleNamespace(
            reload_forbidden_words=reload_forbidden_words
        )
        first = asyncio.create_task(
            self.client.post(
                "/settings/forbidden_words.json",
                data={"csrf": csrf, "document": '["first"]'},
            )
        )
        await asyncio.wait_for(first_reload_started.wait(), timeout=1)
        second = asyncio.create_task(
            self.client.post(
                "/settings/forbidden_words.json",
                data={"csrf": csrf, "document": '["second"]'},
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(
            json.loads(
                (self.settings_dir / "forbidden_words.json").read_text(
                    encoding="utf-8"
                )
            ),
            ["first"],
        )

        release_first_reload.set()
        responses = await asyncio.gather(first, second)
        self.assertTrue(all(response.status == 200 for response in responses))
        self.assertEqual(reload_count, 2)
        self.assertEqual(
            json.loads(
                (self.settings_dir / "forbidden_words.json").read_text(
                    encoding="utf-8"
                )
            ),
            ["second"],
        )

    def test_template_replaces_original_placeholders_once(self):
        page = self.cog._render_template(
            "admin_index.html",
            lang="ko",
            csrf="csrf",
            notice="",
            persona_prompt="{{games}}",
            persona_greeting="{{games}}",
            forbidden_words='["{{games}}"]',
            games='{"Game":{"max_players":2}}',
        )
        self.assertEqual(page.count("{{games}}"), 3)
        self.assertIn("{&quot;Game&quot;:{&quot;max_players&quot;:2}}", page)

    async def test_content_length_and_actual_body_bytes_are_bounded(self):
        response = await self.client.post(
            "/login",
            data=b"x" * (web_admin_cog.MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status, 413)

        async def slow_chunks():
            yield b"token=operator-secret"
            await asyncio.sleep(0)
            yield b"&padding=" + b"x" * web_admin_cog.MAX_BODY_BYTES

        chunked = await self.client.post(
            "/login",
            data=slow_chunks(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(chunked.status, 413)

    async def test_form_content_type_strict_decoding_and_generic_500_headers(self):
        _, csrf = await self._login()
        wrong_type = await self.client.post(
            "/announce",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(wrong_type.status, 415)
        malformed = await self.client.post(
            "/announce",
            data=b"csrf=%ZZ&title=t&body=b",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(malformed.status, 400)

        with patch.object(self.cog, "_index_response", side_effect=RuntimeError("boom")), patch.object(
            web_admin_cog.logger, "exception"
        ):
            failed = await self.client.get("/")
        self.assertEqual(failed.status, 500)
        for key, value in web_admin_cog.SECURITY_HEADERS.items():
            self.assertEqual(failed.headers[key], value)
        self.assertNotIn(csrf, await failed.text())

    async def test_unreadable_settings_directory_reports_reason_not_blank_boxes(self):
        """빈 textarea만 보여주면 파일 없음과 못 읽음이 구분되지 않는다."""
        await self._login()
        readable = await self.client.get("/")
        self.assertIn("prompt", await readable.text())

        # group 쓰기 가능한 SETTINGS_DIR은 config가 거부한다. 그 사유가 화면에
        # 올라와야 운영자가 chmod 하나로 끝낼 수 있다.
        # 소유자 권한은 그대로라 tearDown의 tempdir 정리는 영향받지 않는다.
        self.settings_dir.chmod(0o775)
        degraded = await self.client.get("/")
        page = await degraded.text()
        self.assertEqual(degraded.status, 200)
        self.assertIn("설정을 읽지 못했습니다", page)
        for name in config.SETTINGS_FILES:
            self.assertIn(name, page)

    async def test_percent_encoded_transport_padding_does_not_expand_field_limits(self):
        """전송 한도와 실제 설정 필드 한도는 서로 독립적이다."""
        _, csrf = await self._login()
        padding = "가" * (config.MAX_SETTINGS_BYTES // 6)
        self.assertGreater(len(padding) * 9, config.MAX_SETTINGS_BYTES)
        response = await self.client.post(
            "/settings/persona.json",
            data={
                "csrf": csrf,
                "system_prompt": "prompt",
                "greeting": "안녕",
                "padding": padding,
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIn("새 AI 세션부터 적용", await response.text())
        saved = json.loads(
            (self.settings_dir / "persona.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["system_prompt"], "prompt")

    async def test_persona_form_round_trips_quotes_and_newlines_without_json_escaping(self):
        """persona는 값만 입력받는다. JSON 이스케이프는 운영자 몫이 아니다."""
        _, csrf = await self._login()
        prompt = '따옴표 "회색둥이 씨"와 역슬래시 \\ 그리고\n줄바꿈이 들어간 프롬프트'
        response = await self.client.post(
            "/settings/persona.json",
            data={"csrf": csrf, "system_prompt": prompt, "greeting": "안녕하세요~"},
        )
        self.assertEqual(response.status, 200)
        saved = json.loads(
            (self.settings_dir / "persona.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved, {"system_prompt": prompt, "greeting": "안녕하세요~"})

        # 저장한 값이 다음 화면에 그대로 돌아오고, HTML로 새지 않아야 한다.
        page = await (await self.client.get("/")).text()
        self.assertIn("따옴표 &quot;회색둥이 씨&quot;", page)
        self.assertNotIn('"회색둥이 씨"', page)

    async def test_persona_rejects_blank_fields_and_preserves_previous_file(self):
        _, csrf = await self._login()
        original = (self.settings_dir / "persona.json").read_bytes()
        response = await self.client.post(
            "/settings/persona.json",
            data={"csrf": csrf, "system_prompt": "prompt", "greeting": ""},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("저장 실패", await response.text())
        self.assertEqual((self.settings_dir / "persona.json").read_bytes(), original)

    async def test_unparseable_persona_file_reports_reason_instead_of_defaults(self):
        """깨진 파일을 코드 기본값으로 덮어 보여주면 운영자가 손실을 눈치채지 못한다."""
        await self._login()
        (self.settings_dir / "persona.json").write_text("{not json", encoding="utf-8")
        page = await (await self.client.get("/")).text()
        self.assertIn("persona.json", page)
        self.assertIn("설정을 읽지 못했습니다", page)
        self.assertNotIn("놀빛 정원", page)


class _AnnouncementChannel:
    type = discord.ChannelType.text

    def __init__(self, error=None, allowed=True, name="채널"):
        self.error = error
        self.allowed = allowed
        self.name = name
        self.embeds = []
        self.files = []

    def permissions_for(self, member):
        return SimpleNamespace(
            view_channel=self.allowed,
            send_messages=self.allowed,
            read_message_history=self.allowed,
            embed_links=self.allowed,
            attach_files=self.allowed,
        )

    async def send(self, *, embed, file=None):
        if self.error:
            raise self.error
        self.embeds.append(embed)
        self.files.append(file)


class _AnnouncementGuild:
    def __init__(self, channels, guild_id=0, name="길드"):
        self.channels = channels
        self.id = guild_id
        self.name = name
        self.me = object()
        for channel_id, channel in channels.items():
            channel.id = channel_id
        self.text_channels = list(channels.values())

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class WebAdminAnnouncementDirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_announcement_result_lists_each_skip_and_failure_cause(self):
        sent = _AnnouncementChannel()
        forbidden = _AnnouncementChannel(allowed=False)
        broken = _AnnouncementChannel(error=RuntimeError("private detail"))
        repository = _WebSettingsRepository(
            guild_ids=[1, 2, 3, 4, 5],
            channels={1: 11, 2: 22, 3: 33, 4: 44, 5: 55},
        )
        bot = _WebBot()
        bot.guilds = [
            _AnnouncementGuild({11: sent}, 1, "성공 길드"),
            _AnnouncementGuild({22: forbidden}, 2, "권한 길드"),
            _AnnouncementGuild({}, 3, "삭제 길드"),
            _AnnouncementGuild({44: broken}, 4, "실패 길드"),
        ]
        cog = web_admin_cog.WebAdminCog(bot, repository)
        session = web_admin_cog.AdminSession(csrf="csrf", expires_at=1)
        response = web_admin_cog.web.Response(text="ok")

        with patch.object(
            cog,
            "_read_announcement_form",
            AsyncMock(
                return_value=(
                    {"csrf": "csrf", "title": "제목", "body": "본문"},
                    None,
                )
            ),
        ), patch.object(cog, "_require_session"), patch.object(
            cog,
            "_require_csrf",
            return_value=("session", session),
        ), patch.object(
            cog,
            "_index_response",
            AsyncMock(return_value=response),
        ) as render, patch.object(web_admin_cog.logger, "exception"):
            result = await cog.announce_post(object())

        self.assertIs(result, response)
        notice = render.await_args.args[2]
        self.assertIn("성공 1, 건너뜀 3, 실패 1", notice)
        self.assertIn("권한이 부족해 건너뜀", notice)
        self.assertIn("설정된 공지 채널이 삭제되어 건너뜀", notice)
        self.assertIn("현재 길드에 접속되어 있지 않아 건너뜀", notice)
        self.assertIn("예상하지 못한 오류가 발생했습니다 (RuntimeError)", notice)
        self.assertNotIn("private detail", notice)


class WebAdminImageValidationTests(unittest.TestCase):
    def test_supported_image_signatures_get_fixed_attachment_names(self):
        cases = (
            ("image/png", b"\x89PNG\r\n\x1a\n", "announcement.png"),
            ("image/jpeg", b"\xff\xd8\xff", "announcement.jpg"),
            ("image/gif", b"GIF89a", "announcement.gif"),
            ("image/webp", b"RIFF\x04\x00\x00\x00WEBP", "announcement.webp"),
        )
        for content_type, payload, expected in cases:
            with self.subTest(content_type=content_type):
                upload = web_admin_cog.AnnouncementUpload(content_type, payload)
                self.assertEqual(
                    web_admin_cog.WebAdminCog._announcement_image_name(upload),
                    expected,
                )

    def test_mismatched_or_unsupported_image_is_rejected(self):
        for upload in (
            web_admin_cog.AnnouncementUpload("image/png", b"not png"),
            web_admin_cog.AnnouncementUpload("image/svg+xml", b"<svg>"),
        ):
            with self.assertRaises(ValueError):
                web_admin_cog.WebAdminCog._announcement_image_name(upload)


class WebAdminAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = WebAdminHTTPTests.asyncSetUp
    asyncTearDown = WebAdminHTTPTests.asyncTearDown
    _login = WebAdminHTTPTests._login

    async def test_announcement_uses_the_guild_announcement_channel_not_party_channel(self):
        _, csrf = await self._login()
        party = _AnnouncementChannel()
        announcement = _AnnouncementChannel()
        self.repository.guild_ids = [1]
        self.repository.party_channels = {1: 11}
        self.repository.channels = {1: 22}
        self.bot.guilds = [_AnnouncementGuild({11: party, 22: announcement}, 1)]

        response = await self.client.post(
            "/announce",
            data={"csrf": csrf, "title": "Title", "body": "Body"},
        )

        self.assertIn("성공 1", await response.text())
        self.assertEqual(party.embeds, [])
        self.assertEqual(len(announcement.embeds), 1)

    async def test_announcement_accepts_one_png_attachment(self):
        _, csrf = await self._login()
        channel = _AnnouncementChannel()
        self.repository.guild_ids = [1]
        self.repository.channels = {1: 22}
        self.bot.guilds = [_AnnouncementGuild({22: channel}, 1)]
        data = FormData()
        data.add_field("csrf", csrf)
        data.add_field("title", "Title")
        data.add_field("body", "Body")
        data.add_field(
            "image",
            b"\x89PNG\r\n\x1a\nimage",
            filename="notice.png",
            content_type="image/png",
        )

        response = await self.client.post("/announce", data=data)

        self.assertIn("성공 1", await response.text())
        self.assertEqual(channel.embeds[0].image.url, "attachment://announcement.png")
        self.assertEqual(channel.files[0].filename, "announcement.png")

    async def test_only_opted_in_accessible_channels_receive_isolated_announcement(self):
        session_id, csrf = await self._login()
        sent = _AnnouncementChannel()
        forbidden = _AnnouncementChannel(allowed=False)
        broken = _AnnouncementChannel(error=RuntimeError("discord unavailable"))
        self.repository.guild_ids = [1, 2, 3, 4, 5]
        self.repository.channels = {1: 11, 2: 22, 3: 33, 4: 44, 5: 55}
        self.bot.guilds = [
            _AnnouncementGuild({11: sent}, 1),
            _AnnouncementGuild({22: forbidden}, 2),
            _AnnouncementGuild({}, 3),
            _AnnouncementGuild({44: broken}, 4),
            # guild 5 is inaccessible
            _AnnouncementGuild({999: _AnnouncementChannel()}, 999),  # opted out
        ]
        with patch.object(web_admin_cog.logger, "exception") as logged:
            response = await self.client.post(
                "/announce",
                data={"csrf": csrf, "title": "Title", "body": "Body"},
            )
        page = await response.text()
        self.assertIn("성공 1, 건너뜀 3, 실패 1", page)
        self.assertIn("권한이 부족해 건너뜀", page)
        self.assertIn("설정된 공지 채널이 삭제되어 건너뜀", page)
        self.assertIn("현재 길드에 접속되어 있지 않아 건너뜀", page)
        self.assertIn("예상하지 못한 오류가 발생했습니다 (RuntimeError)", page)
        self.assertEqual(len(sent.embeds), 1)
        self.assertEqual((sent.embeds[0].title, sent.embeds[0].description), ("Title", "Body"))
        logged.assert_called_once()
        self.assertEqual(logged.call_args.args[-1], 4)
        self.assertIn(session_id, self.cog.admin_sessions)

    async def test_hanging_guild_times_out_without_blocking_settings_saves(self):
        """공지가 한 길드에 매달려도 관리 화면 전체가 멈추면 안 된다."""
        _, csrf = await self._login()
        sending = asyncio.Event()

        class _HangingChannel(_AnnouncementChannel):
            async def send(self, *, embed):
                sending.set()
                await asyncio.sleep(3600)

        delivered = _AnnouncementChannel()
        self.repository.guild_ids = [1, 2]
        self.repository.channels = {1: 11, 2: 22}
        self.bot.guilds = [
            _AnnouncementGuild({11: _HangingChannel()}, 1),
            _AnnouncementGuild({22: delivered}, 2),
        ]
        with patch.object(
            web_admin_cog, "ANNOUNCEMENT_SEND_TIMEOUT_SECONDS", 0.2
        ), patch.object(
            web_admin_cog.logger, "exception"
        ):
            announcing = asyncio.create_task(
                self.client.post(
                    "/announce", data={"csrf": csrf, "title": "Title", "body": "Body"}
                )
            )
            await asyncio.wait_for(sending.wait(), timeout=1)
            saved = await self.client.post(
                "/settings/games.json",
                data={
                    "csrf": csrf,
                    "document": json.dumps({"Game": {"max_players": 3, "roles": []}}),
                },
            )
            response = await asyncio.wait_for(announcing, timeout=5)

        self.assertEqual(saved.status, 200)
        page = await response.text()
        self.assertIn("성공 1, 건너뜀 0, 실패 1", page)
        self.assertIn("작업 시간이 초과되었습니다", page)
        self.assertEqual(len(delivered.embeds), 1)

    async def test_selected_guild_receives_announcement_alone_with_chosen_colour(self):
        """대상을 고르면 그 길드에만, 고른 색으로 간다."""
        _, csrf = await self._login()
        picked = _AnnouncementChannel()
        other = _AnnouncementChannel()
        self.repository.guild_ids = [1, 2]
        self.repository.channels = {1: 11, 2: 22}
        self.bot.guilds = [
            _AnnouncementGuild({11: picked}, 1),
            _AnnouncementGuild({22: other}, 2),
        ]
        response = await self.client.post(
            "/announce",
            data={
                "csrf": csrf,
                "guild_id": "2",
                "title": "Title",
                "body": "Body",
                "color": "#a1b2c3",
            },
        )
        self.assertIn("성공 1, 건너뜀 0, 실패 0", await response.text())
        self.assertEqual(picked.embeds, [])
        self.assertEqual(len(other.embeds), 1)
        self.assertEqual(other.embeds[0].colour, discord.Colour(0xA1B2C3))

    async def test_announcement_to_opted_out_or_malformed_guild_sends_nothing(self):
        """드롭다운에 없는 길드를 form에 박아 넣어도 옵트인 경계를 넘지 못한다."""
        _, csrf = await self._login()
        opted_in = _AnnouncementChannel()
        opted_out = _AnnouncementChannel()
        self.repository.guild_ids = [1]
        self.repository.channels = {1: 11, 999: 999}
        self.bot.guilds = [
            _AnnouncementGuild({11: opted_in}, 1),
            _AnnouncementGuild({999: opted_out}, 999),
        ]
        for guild_id, expected in (
            ("999", "공지를 허용하지 않은 길드입니다."),
            ("nope", "공지 대상 길드가 올바르지 않습니다."),
            ("²", "공지 대상 길드가 올바르지 않습니다."),
        ):
            with self.subTest(guild_id=guild_id):
                response = await self.client.post(
                    "/announce",
                    data={
                        "csrf": csrf,
                        "guild_id": guild_id,
                        "title": "Title",
                        "body": "Body",
                    },
                )
                self.assertIn(expected, await response.text())
        self.assertEqual(opted_out.embeds, [])
        self.assertEqual(opted_in.embeds, [])

    async def test_malformed_colour_is_rejected_before_any_guild_is_contacted(self):
        _, csrf = await self._login()
        channel = _AnnouncementChannel()
        self.repository.guild_ids = [1]
        self.repository.channels = {1: 11}
        self.bot.guilds = [_AnnouncementGuild({11: channel}, 1)]
        response = await self.client.post(
            "/announce",
            data={
                "csrf": csrf,
                "title": "Title",
                "body": "Body",
                "color": "red; drop",
            },
        )
        self.assertIn("공지 색상은 #RRGGBB 형식이어야 합니다.", await response.text())
        self.assertEqual(channel.embeds, [])

    async def test_index_lists_guild_settings_and_offers_only_opted_in_targets(self):
        """운영자가 공지 대상과 패널 채널을 화면에서 확인할 수 있어야 한다."""
        await self._login()
        self.repository.guild_ids = [1]
        self.repository.channels = {1: 11, 2: 22}
        party = _AnnouncementChannel(allowed=False, name="🎮-파티")
        self.bot.guilds = [
            _AnnouncementGuild({11: party}, 1, "공지 켠 길드"),
            _AnnouncementGuild({}, 2, "<공지 끈 길드>"),
        ]
        page = await (await self.client.get("/")).text()

        self.assertIn("#🎮-파티 (권한 부족)", page)
        # 길드 2는 party_channel_id 22가 있지만 채널이 사라졌다.
        self.assertIn("삭제됨 (22)", page)
        self.assertIn('<option value="1">공지 켠 길드</option>', page)
        # 이벤트 채널 폼은 모든 길드에 있으므로 공지 대상 option만 확인한다.
        self.assertNotIn('<option value="2">', page)
        # 길드 이름은 운영자가 통제하지 않는 외부 문자열이다.
        self.assertNotIn("<공지 끈 길드>", page)
        self.assertIn("&lt;공지 끈 길드&gt;", page)

    async def test_unreadable_guild_settings_surface_instead_of_breaking_the_page(self):
        await self._login()
        self.repository.guild_ids = [1]
        self.repository.channels = {1: RuntimeError("db unavailable")}
        self.bot.guilds = [_AnnouncementGuild({}, 1)]
        with patch.object(web_admin_cog.logger, "exception") as logged:
            response = await self.client.get("/")
        page = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("길드 설정을 읽지 못했습니다", page)
        logged.assert_called_once()

    async def test_announcement_rejects_empty_and_discord_overlimit_text(self):
        _, csrf = await self._login()
        for title, body in (
            ("", "body"),
            ("t" * 257, "body"),
            ("title", ""),
            ("title", "b" * 4_097),
        ):
            with self.subTest(title_length=len(title), body_length=len(body)):
                response = await self.client.post(
                    "/announce", data={"csrf": csrf, "title": title, "body": body}
                )
                self.assertEqual(response.status, 200)
        self.assertEqual(self.repository.guild_ids, [])
