import gc
import pathlib
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import discord

from module.attendance_cog import AttendanceCog
from module.database import SQLiteAttendanceRepository, SQLitePartyRepository
from module.eventnotice_cog import EventNoticeCog
from module.hyacine_chat_cog import HyacineChatCog
from module.playwith_cog import PlayWithCog
import module.playwith_cog as playwith_cog


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
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeUser:
    id = 123
    mention = "<@123>"
    display_name = "테스트 유저"
    joined_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    color = discord.Color.default()
    avatar = None


class FakeGuild:
    def __init__(self):
        self.fetch_scheduled_events_calls = 0

    async def fetch_scheduled_events(self):
        self.fetch_scheduled_events_calls += 1
        return []

    def get_member(self, user_id):
        return FakeUser()


class FakeInteraction:
    def __init__(self, channel_id, guild=None):
        self.channel_id = channel_id
        self.user = FakeUser()
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.guild = guild


class RecordingAttendance:
    def __init__(self, refund_error=None):
        self.deductions = []
        self.refunds = []
        self.refund_error = refund_error

    def deduct_points(self, user_id, amount):
        self.deductions.append((user_id, amount))
        return True

    def add_points(self, user_id, amount):
        if self.refund_error:
            raise self.refund_error
        self.refunds.append((user_id, amount))


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
                (basic, "기본 대화", None, "gpt-5.6-terra", "none", 0),
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
