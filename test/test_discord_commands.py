import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import discord

from module.attendance_cog import AttendanceCog
from module.database import SQLiteAttendanceRepository, SQLitePartyRepository
from module.eventnotice_cog import EventNoticeCog
from module.playwith_cog import PlayWithCog
import module.playwith_cog as playwith_cog


class RecordingResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    def is_done(self):
        return bool(self.messages)


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


class FakeInteraction:
    def __init__(self, channel_id, guild=None):
        self.channel_id = channel_id
        self.user = FakeUser()
        self.response = RecordingResponse()
        self.guild = guild


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
