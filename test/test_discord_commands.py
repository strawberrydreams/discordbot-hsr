import asyncio
import contextlib
import dataclasses
import gc
import hashlib
import json
import os
import pathlib
import secrets
import tempfile
import threading
import unittest
import warnings
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import discord
import httpx
import openai
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from discord.ext import commands

import module.config as config
import module.guildsettings_cog as guildsettings_cog
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
import module.music_cog as music_cog
from module.music_cog import (
    MusicCog,
    MusicPanelView,
    MusicPlayer,
    MusicRemoveView,
    MusicSnapshot,
    MusicTrack,
    extract_track,
    music_panel,
    resolve_stream_url,
)
import module.playwith_cog as playwith_cog
import module.forbiddenfilter_cog as forbiddenfilter_cog
import module.backup as backup
import module.webadmin_cog as webadmin_cog



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


def _spec_faker(missing):
    """nacl/yt_dlp의 설치 여부만 조작하고 나머지 모듈 조회는 그대로 둔다."""
    real = music_cog.find_spec

    def fake(name, *args, **kwargs):
        if name in ("nacl", "yt_dlp"):
            return None if name in missing else object()
        return real(name, *args, **kwargs)

    return fake


_ALL_AI_KEYS = {"OPENAI_API_KEY": "a", "GOOGLE_API_KEY": "b"}
_NO_AI_KEYS = {"OPENAI_API_KEY": None, "GOOGLE_API_KEY": None}
_CORE_EXTENSIONS = (
    "module.guildsettings_cog",
    "module.eventnotice_cog",
    "module.playwith_cog",
    "module.forbiddenfilter_cog",
    "module.attendance_cog",
    "module.finance_cog",
)


class MusicDependencyGateTest(unittest.TestCase):
    """선택적 의존성이 빠지면 음악 확장 하나만 사라져야 한다."""

    def _load(self, missing=(), env=None):
        buffer = StringIO()
        with patch.object(music_cog, "find_spec", _spec_faker(set(missing))), \
             patch.object(bot_main, "ENV_VALUES", dict(env or _ALL_AI_KEYS)), \
             contextlib.redirect_stdout(buffer):
            names = bot_main.available_extensions()
        return names, buffer.getvalue()

    def test_extension_entries_carry_env_names_and_dependency_check(self):
        for entry in bot_main.EXTENSIONS:
            self.assertEqual(len(entry), 3, entry)
            module_name, required, dependency_check = entry
            self.assertIsInstance(module_name, str)
            self.assertIsInstance(required, tuple)
            self.assertTrue(dependency_check is None or callable(dependency_check))

    def test_music_loads_when_both_packages_are_installed(self):
        names, _ = self._load()
        self.assertIn("module.music_cog", names)

    def test_music_alone_is_skipped_without_pynacl(self):
        names, log = self._load(missing=["nacl"])
        self.assertNotIn("module.music_cog", names)
        for extension in _CORE_EXTENSIONS:
            self.assertIn(extension, names)
        self.assertIn("module.hyacine_chat_cog", names)
        self.assertIn("module.hyacine_image_cog", names)
        self.assertIn("PyNaCl", log)
        self.assertNotIn("yt-dlp", log)
        self.assertIn("pip install", log)

    def test_music_alone_is_skipped_without_yt_dlp(self):
        names, log = self._load(missing=["yt_dlp"])
        self.assertNotIn("module.music_cog", names)
        for extension in _CORE_EXTENSIONS:
            self.assertIn(extension, names)
        self.assertIn("yt-dlp", log)
        self.assertIn("pip install", log)

    def test_missing_packages_are_all_named_in_the_skip_reason(self):
        _, log = self._load(missing=["nacl", "yt_dlp"])
        self.assertIn("PyNaCl", log)
        self.assertIn("yt-dlp", log)
        self.assertIn("pip install PyNaCl yt-dlp", log)

    def test_core_extensions_survive_when_every_optional_feature_is_absent(self):
        names, _ = self._load(missing=["nacl", "yt_dlp"], env=_NO_AI_KEYS)
        self.assertEqual(names, list(_CORE_EXTENSIONS))

    def test_dependency_error_is_none_when_packages_exist(self):
        with patch.object(music_cog, "find_spec", _spec_faker(set())):
            self.assertIsNone(music_cog.music_dependency_error())


class _RecordingYTDL:
    """yt_dlp.YoutubeDL 대역. 어떤 thread에서 불렸는지까지 기록한다."""

    def __init__(self, info=None, error=None):
        self.info = info
        self.error = error
        self.urls = []
        self.threads = []
        self.download_flags = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        self.urls.append(url)
        self.download_flags.append(download)
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error
        return self.info


class MusicExtractionTests(unittest.IsolatedAsyncioTestCase):
    def _patch_ytdl(self, info=None, error=None):
        ytdl = _RecordingYTDL(info, error)
        patcher = patch.object(music_cog, "_youtube_dl", lambda: ytdl)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ytdl

    def test_ytdl_options_disable_download_playlist_and_search(self):
        options = music_cog._YTDL_OPTIONS
        self.assertIs(options["noplaylist"], True)
        self.assertIs(options["skip_download"], True)
        self.assertIs(options["extract_flat"], False)
        # 기본 검색이 켜져 있으면 "아무 문자열"이 곡으로 승격된다.
        self.assertEqual(options["default_search"], "error")

    async def test_extraction_runs_off_the_event_loop(self):
        ytdl = self._patch_ytdl(
            {"title": "노래", "webpage_url": "https://example.com/watch?v=1"}
        )
        await extract_track("https://example.com/watch?v=1", requester_id=7)
        self.assertEqual(len(ytdl.threads), 1)
        self.assertIsNot(ytdl.threads[0], threading.main_thread())
        self.assertEqual(ytdl.download_flags, [False])

    async def test_only_single_http_urls_are_accepted(self):
        ytdl = self._patch_ytdl({"title": "노래", "webpage_url": "https://e.com/1"})
        for rejected in (
            "file:///etc/passwd",
            "ftp://example.com/song.mp3",
            "javascript:alert(1)",
            "example.com/watch",
            "노래 제목 검색어",
            "https:///no-host",
            "",
        ):
            with self.subTest(url=rejected):
                with self.assertRaises(ValueError):
                    await extract_track(rejected, requester_id=7)
        self.assertEqual(ytdl.urls, [])  # 검증 전에 yt-dlp를 부르지 않는다

    async def test_playlists_are_rejected(self):
        self._patch_ytdl({"_type": "playlist", "title": "목록", "entries": []})
        with self.assertRaises(ValueError):
            await extract_track("https://example.com/list", requester_id=7)

    async def test_live_streams_are_rejected(self):
        for info in (
            {"title": "생방송", "webpage_url": "https://e.com/1", "is_live": True},
            {
                "title": "예정",
                "webpage_url": "https://e.com/1",
                "live_status": "is_upcoming",
            },
        ):
            with self.subTest(info=info):
                self._patch_ytdl(info)
                with self.assertRaises(ValueError):
                    await extract_track("https://example.com/1", requester_id=7)

    async def test_title_must_be_a_non_empty_string(self):
        for title in (None, 12, "", "   "):
            with self.subTest(title=title):
                self._patch_ytdl({"title": title, "webpage_url": "https://e.com/1"})
                with self.assertRaises(ValueError):
                    await extract_track("https://example.com/1", requester_id=7)

    async def test_long_title_is_bounded(self):
        self._patch_ytdl({"title": "가" * 400, "webpage_url": "https://e.com/1"})
        track = await extract_track("https://example.com/1", requester_id=7)
        self.assertEqual(len(track.title), music_cog.MAX_TITLE_LENGTH)

    async def test_canonical_webpage_url_replaces_the_input_url(self):
        self._patch_ytdl(
            {"title": "노래", "webpage_url": "https://example.com/watch?v=abc"}
        )
        track = await extract_track(
            "https://example.com/short/abc?utm=1", requester_id=99
        )
        self.assertEqual(track.webpage_url, "https://example.com/watch?v=abc")
        self.assertEqual(track.requester_id, 99)

    async def test_unusable_canonical_url_is_rejected(self):
        for webpage_url in (
            "file:///tmp/song.mp3",
            12,
            "https://example.com/" + "a" * music_cog.MAX_URL_LENGTH,
        ):
            with self.subTest(webpage_url=webpage_url):
                self._patch_ytdl({"title": "노래", "webpage_url": webpage_url})
                with self.assertRaises(ValueError):
                    await extract_track("https://example.com/1", requester_id=7)

    def test_track_is_immutable_and_stores_no_stream_url(self):
        track = MusicTrack("노래", "https://example.com/1", 7)
        self.assertEqual(
            set(MusicTrack.__dataclass_fields__),
            {"title", "webpage_url", "requester_id"},
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            track.title = "다른 노래"

    async def test_stream_url_is_resolved_again_off_the_event_loop(self):
        ytdl = self._patch_ytdl(
            {
                "title": "노래",
                "webpage_url": "https://example.com/watch?v=1",
                "url": "https://cdn.example.com/stream?expires=1",
            }
        )
        track = MusicTrack("노래", "https://example.com/watch?v=1", 7)
        stream = await resolve_stream_url(track)
        self.assertEqual(stream, "https://cdn.example.com/stream?expires=1")
        self.assertEqual(ytdl.urls, ["https://example.com/watch?v=1"])
        self.assertIsNot(ytdl.threads[0], threading.main_thread())

    async def test_unusable_stream_url_is_rejected(self):
        for url in (None, "", "file:///tmp/song.mp3"):
            with self.subTest(url=url):
                self._patch_ytdl(
                    {"title": "노래", "webpage_url": "https://e.com/1", "url": url}
                )
                with self.assertRaises(ValueError):
                    await resolve_stream_url(MusicTrack("노래", "https://e.com/1", 7))


BOT_USER_ID = 999


class _FakeVoiceMember:
    def __init__(self, bot=False, user_id=1, guild_id=TEST_GUILD_ID):
        self.bot = bot
        self.id = user_id
        self.guild = SimpleNamespace(id=guild_id)


class _FakeVoiceChannel:
    def __init__(self, members=()):
        self.members = list(members)


class _FakeVoiceState:
    def __init__(self, channel=None):
        self.channel = channel


class _FakeVoiceClient:
    """discord.VoiceClient 대역.

    실제 VoiceClient의 두 가지 성질을 그대로 흉내낸다.
    - `stop()`과 `disconnect()`가 after callback을 부른다(`disconnect()`는 내부에서
      `stop()`을 부른다). MusicPlayer.stop()이 별도 `stop()` 호출 없이도 유령 재생을
      남기지 않는지는 이 성질에 기대고 있다.
    - `play()`는 이미 재생 중이거나 연결이 없으면 `ClientException`을 던진다.
    """

    def __init__(self, channel=None, play_error=None):
        self.channel = channel
        self.play_error = play_error
        self.sources = []
        self.after = None
        self.disconnects = 0
        self._playing = False
        self._paused = False

    def play(self, source, *, after=None):
        if self.play_error is not None:
            raise self.play_error
        if self._playing:
            raise discord.ClientException("Already playing audio.")
        self.sources.append(source)
        self.after = after
        self._playing = True
        self._paused = False

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        after, self.after = self.after, None
        self._playing = False
        self._paused = False
        if after is not None:
            after(None)

    async def disconnect(self, *, force=False):
        self.disconnects += 1
        self.stop()


class _FakeSource:
    def __init__(self, url, **options):
        self.url = url
        self.options = options
        self.thread = threading.current_thread()
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


def _music_track(number):
    return MusicTrack(f"곡{number}", f"https://example.com/{number}", 100 + number)


class _PatchedPlaybackTests(unittest.IsolatedAsyncioTestCase):
    """스트림 해석과 ffmpeg source를 대역으로 바꾼 공통 바탕."""

    async def asyncSetUp(self):
        self.sources = []          # play() 성공 여부와 무관하게 생성된 모든 source
        self.resolve_errors = {}   # 곡 제목 → 해석 시 던질 예외

        async def fake_resolve(track):
            error = self.resolve_errors.get(track.title)
            if error is not None:
                raise error
            return f"https://cdn.example.com/{track.title}"

        def fake_source(url, **options):
            source = _FakeSource(url, **options)
            self.sources.append(source)
            return source

        for patcher in (
            patch.object(music_cog, "resolve_stream_url", fake_resolve),
            patch("discord.FFmpegPCMAudio", fake_source),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _wait_until(self, predicate, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def _settle(self):
        """thread→loop로 예약된 뒷정리 task가 다 돌도록 잠시 양보한다."""
        await asyncio.sleep(0.05)


class MusicPlayerStateTests(_PatchedPlaybackTests):
    async def test_first_enqueue_starts_playback_exactly_once(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        first, second = _music_track(1), _music_track(2)

        await player.enqueue(voice, first)
        self.assertEqual(len(voice.sources), 1)
        self.assertEqual(player.current, first)
        self.assertEqual(player.snapshot().queue, ())

        await player.enqueue(voice, second)
        self.assertEqual(len(voice.sources), 1)  # 재생 중이면 다시 시작하지 않는다
        self.assertEqual(player.current, first)
        self.assertEqual(player.snapshot().queue, (second,))

    async def test_ffmpeg_source_reconnects_and_drops_video(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        await player.enqueue(voice, _music_track(1))
        options = voice.sources[0].options
        self.assertIn("-reconnect 1", options["before_options"])
        self.assertIn("-reconnect_delay_max", options["before_options"])
        self.assertEqual(options["options"], "-vn")

    async def test_playback_callback_hands_the_next_track_to_the_event_loop(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        first, second = _music_track(1), _music_track(2)
        await player.enqueue(voice, first)
        await player.enqueue(voice, second)

        # discord.py는 재생 완료 callback을 별도 thread에서 부른다.
        worker = threading.Thread(target=voice.stop)
        worker.start()
        started = await self._wait_until(lambda: len(voice.sources) == 2)
        worker.join(timeout=2)

        self.assertTrue(started, "다음 곡이 시작되지 않았다")
        self.assertEqual(player.current, second)
        self.assertEqual(player.snapshot().queue, ())
        # 다음 곡 준비는 worker thread가 아니라 event loop에서 이뤄져야 한다.
        self.assertIs(voice.sources[1].thread, threading.main_thread())
        self.assertIsNot(voice.sources[1].thread, worker)

    async def test_skip_stops_the_current_source_and_advances(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        first, second = _music_track(1), _music_track(2)
        await player.enqueue(voice, first)
        await player.enqueue(voice, second)

        skipped = await player.skip()
        self.assertEqual(skipped, first)
        self.assertTrue(await self._wait_until(lambda: player.current == second))
        self.assertEqual(len(voice.sources), 2)
        self.assertEqual(voice.sources[1].url, "https://cdn.example.com/곡2")
        self.assertEqual(player.snapshot().queue, ())

    async def test_skipping_the_last_track_leaves_the_player_idle(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        await player.enqueue(voice, _music_track(1))

        await player.skip()
        self.assertTrue(await self._wait_until(lambda: player.current is None))
        self.assertEqual(len(voice.sources), 1)
        self.assertFalse(voice.is_playing())

    async def test_pause_and_resume_track_the_voice_client_state(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        await player.enqueue(voice, _music_track(1))

        self.assertTrue(await player.toggle_pause())
        self.assertTrue(voice.is_paused())
        self.assertFalse(voice.is_playing())
        self.assertTrue(player.snapshot().paused)

        self.assertFalse(await player.toggle_pause())
        self.assertFalse(voice.is_paused())
        self.assertTrue(voice.is_playing())
        self.assertFalse(player.snapshot().paused)

    async def test_stop_clears_the_queue_and_disconnects(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        await player.enqueue(voice, _music_track(1))
        await player.enqueue(voice, _music_track(2))

        await player.stop()
        # 실제 disconnect()는 내부에서 stop()을 불러 after callback을 깨운다.
        # 그 뒤늦은 _advance가 유령 재생을 만들지 않아야 한다.
        await self._settle()
        snapshot = player.snapshot()
        self.assertIsNone(snapshot.current)
        self.assertEqual(snapshot.queue, ())
        self.assertFalse(snapshot.connected)
        self.assertEqual(voice.disconnects, 1)
        self.assertEqual(len(voice.sources), 1)
        self.assertIsNone(player.voice_client)

    async def test_remove_uses_one_based_queue_numbers(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        playing, second, third = _music_track(1), _music_track(2), _music_track(3)
        await player.enqueue(voice, playing)
        await player.enqueue(voice, second)
        await player.enqueue(voice, third)

        self.assertIsNone(await player.remove(0))       # 0-based 번호는 없다
        self.assertIsNone(await player.remove(3))       # 대기열은 2곡뿐이다
        self.assertIsNone(await player.remove(-1))
        self.assertIsNone(await player.remove(True))    # bool은 번호가 아니다
        self.assertEqual(player.snapshot().queue, (second, third))

        # 1번은 재생 중인 곡이 아니라 대기열 첫 곡이다.
        self.assertEqual(await player.remove(1), second)
        self.assertEqual(player.snapshot().queue, (third,))
        self.assertEqual(player.current, playing)

    async def test_empty_voice_channel_goes_idle_and_disconnects(self):
        player = MusicPlayer(TEST_GUILD_ID)
        listener = _FakeVoiceMember()
        channel = _FakeVoiceChannel([_FakeVoiceMember(bot=True), listener])
        voice = _FakeVoiceClient(channel)
        await player.enqueue(voice, _music_track(1))
        await player.enqueue(voice, _music_track(2))

        self.assertFalse(await player.disconnect_if_alone())
        self.assertEqual(voice.disconnects, 0)

        channel.members.remove(listener)
        self.assertTrue(await player.disconnect_if_alone())
        await self._settle()
        snapshot = player.snapshot()
        self.assertIsNone(snapshot.current)
        self.assertEqual(snapshot.queue, ())
        self.assertFalse(snapshot.connected)
        self.assertEqual(voice.disconnects, 1)
        self.assertEqual(len(voice.sources), 1)

    async def test_players_are_per_guild_and_all_stop_on_unload(self):
        cog = MusicCog(bot=None)
        first = cog.get_player(1)
        self.assertIs(cog.get_player(1), first)
        second = cog.get_player(2)
        self.assertIsNot(second, first)

        voices = []
        for player in (first, second):
            voice = _FakeVoiceClient()
            voices.append(voice)
            await player.enqueue(voice, _music_track(1))

        await cog.cog_unload()
        await self._settle()
        self.assertEqual(cog.players, {})
        self.assertTrue(all(voice.disconnects == 1 for voice in voices))
        self.assertTrue(all(player.current is None for player in (first, second)))

    async def test_controls_are_not_blocked_by_a_slow_resolution(self):
        """해석은 lock 밖에서 돈다. 그렇지 않으면 Task 6 버튼이 3초 안에 응답하지 못한다."""
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        gate = asyncio.Event()
        resolving = asyncio.Event()

        async def slow_resolve(track):
            resolving.set()
            await gate.wait()
            return f"https://cdn.example.com/{track.title}"

        with patch.object(music_cog, "resolve_stream_url", slow_resolve):
            enqueueing = asyncio.create_task(player.enqueue(voice, _music_track(1)))
            await asyncio.wait_for(resolving.wait(), timeout=1)
            # 해석이 아직 진행 중인데도 제어는 즉시 돌아와야 한다.
            await asyncio.wait_for(player.stop(), timeout=1)
            gate.set()
            await asyncio.wait_for(enqueueing, timeout=1)

        self.assertEqual(voice.sources, [])  # 끊긴 뒤에는 재생하지 않는다
        self.assertIsNone(player.current)
        self.assertEqual(voice.disconnects, 1)
        self.assertTrue(all(source.cleaned for source in self.sources))

    async def test_skip_during_resolution_discards_that_result_and_starts_the_next(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        gate = asyncio.Event()
        resolving = asyncio.Event()

        async def slow_first(track):
            if track.title == "곡1":
                resolving.set()
                await gate.wait()
            return f"https://cdn.example.com/{track.title}"

        await player.queue_track(voice, _music_track(1))
        await player.queue_track(voice, _music_track(2))
        with patch.object(music_cog, "resolve_stream_url", slow_first):
            starting = asyncio.create_task(player.start_if_idle())
            await asyncio.wait_for(resolving.wait(), timeout=1)
            snapshot = player.snapshot()
            self.assertIsNone(snapshot.current)
            self.assertEqual(snapshot.resolving, _music_track(1))
            self.assertEqual(await player.skip(), _music_track(1))
            gate.set()
            await asyncio.wait_for(starting, timeout=1)

        self.assertEqual(player.current, _music_track(2))
        self.assertEqual(len(voice.sources), 1)
        self.assertTrue(voice.sources[0].url.endswith("곡2"))
        self.assertTrue(self.sources[0].cleaned)

    async def test_dead_link_is_skipped_and_the_queue_keeps_playing(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        playing, dead, alive = _music_track(1), _music_track(2), _music_track(3)
        self.resolve_errors[dead.title] = ValueError("사라진 영상입니다.")
        for track in (playing, dead, alive):
            await player.enqueue(voice, track)

        with self.assertLogs(music_cog.logger, level="ERROR"):
            voice.stop()  # 첫 곡 종료 → 죽은 링크를 건너뛰고 다음 곡으로
            self.assertTrue(await self._wait_until(lambda: player.current == alive))
        self.assertEqual([source.url for source in voice.sources][-1],
                         "https://cdn.example.com/곡3")
        self.assertEqual(len(voice.sources), 2)
        self.assertEqual(player.snapshot().queue, ())

    async def test_only_dead_links_leave_the_player_idle(self):
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient()
        dead = _music_track(1)
        self.resolve_errors[dead.title] = ValueError("사라진 영상입니다.")

        with self.assertLogs(music_cog.logger, level="ERROR"):
            await player.enqueue(voice, dead)

        self.assertIsNone(player.current)
        self.assertEqual(voice.sources, [])
        self.assertEqual(player.snapshot().queue, ())

    async def test_failed_play_reaps_the_ffmpeg_process(self):
        # 해석 도중 연결이 끊기면 play()가 던진다. 이때 이미 떠 있는 ffmpeg를
        # 거둬 가지 않으면 대기열 길이만큼 고아 프로세스가 남는다.
        player = MusicPlayer(TEST_GUILD_ID)
        voice = _FakeVoiceClient(
            play_error=discord.ClientException("Not connected to voice.")
        )
        with self.assertLogs(music_cog.logger, level="ERROR"):
            await player.enqueue(voice, _music_track(1))
            await player.enqueue(voice, _music_track(2))

        self.assertEqual(len(self.sources), 2)
        self.assertTrue(all(source.cleaned for source in self.sources))
        self.assertEqual(voice.sources, [])
        self.assertIsNone(player.current)

    async def test_enqueue_disconnects_a_voice_client_it_replaces(self):
        player = MusicPlayer(TEST_GUILD_ID)
        old, new = _FakeVoiceClient(), _FakeVoiceClient()
        await player.enqueue(old, _music_track(1))
        await player.enqueue(new, _music_track(2))
        await self._settle()

        self.assertEqual(old.disconnects, 1)
        self.assertIs(player.voice_client, new)
        self.assertEqual(len(new.sources), 1)
        self.assertEqual(player.current, _music_track(2))


class MusicVoiceEventTests(_PatchedPlaybackTests):
    """on_voice_state_update — Task 5에서 봇이 실제로 실행하는 유일한 경로."""

    def _cog(self):
        return MusicCog(bot=SimpleNamespace(user=SimpleNamespace(id=BOT_USER_ID)))

    async def _connected(self, cog, channel):
        player = cog.get_player(TEST_GUILD_ID)
        voice = _FakeVoiceClient(channel)
        await player.enqueue(voice, _music_track(1))
        await player.enqueue(voice, _music_track(2))
        return player, voice

    async def test_last_human_leaving_disconnects_the_player(self):
        cog = self._cog()
        listener = _FakeVoiceMember(user_id=1)
        bot_member = _FakeVoiceMember(bot=True, user_id=BOT_USER_ID)
        channel = _FakeVoiceChannel([bot_member, listener])
        player, voice = await self._connected(cog, channel)

        channel.members.remove(listener)
        await cog.on_voice_state_update(
            listener, _FakeVoiceState(channel), _FakeVoiceState(None)
        )
        await self._settle()
        self.assertEqual(voice.disconnects, 1)
        self.assertFalse(player.snapshot().connected)
        self.assertEqual(player.snapshot().queue, ())

    async def test_a_human_leaving_a_still_occupied_channel_changes_nothing(self):
        cog = self._cog()
        staying = _FakeVoiceMember(user_id=2)
        leaving = _FakeVoiceMember(user_id=1)
        channel = _FakeVoiceChannel(
            [_FakeVoiceMember(bot=True, user_id=BOT_USER_ID), staying, leaving]
        )
        player, voice = await self._connected(cog, channel)

        channel.members.remove(leaving)
        await cog.on_voice_state_update(
            leaving, _FakeVoiceState(channel), _FakeVoiceState(None)
        )
        self.assertEqual(voice.disconnects, 0)
        self.assertTrue(player.snapshot().connected)

    async def test_mute_toggles_do_not_touch_the_player(self):
        cog = self._cog()
        channel = _FakeVoiceChannel([_FakeVoiceMember(bot=True, user_id=BOT_USER_ID)])
        player, voice = await self._connected(cog, channel)

        member = _FakeVoiceMember(user_id=1)
        # 같은 채널 안에서의 상태 변화(음소거 등)는 채널이 비어 있어도 무시한다.
        await cog.on_voice_state_update(
            member, _FakeVoiceState(channel), _FakeVoiceState(channel)
        )
        self.assertEqual(voice.disconnects, 0)
        self.assertTrue(player.snapshot().connected)

    async def test_forced_disconnect_of_the_bot_clears_stale_state(self):
        cog = self._cog()
        channel = _FakeVoiceChannel(
            [_FakeVoiceMember(bot=True, user_id=BOT_USER_ID), _FakeVoiceMember(user_id=1)]
        )
        player, voice = await self._connected(cog, channel)

        bot_member = _FakeVoiceMember(bot=True, user_id=BOT_USER_ID)
        await cog.on_voice_state_update(
            bot_member, _FakeVoiceState(channel), _FakeVoiceState(None)
        )
        snapshot = player.snapshot()
        self.assertFalse(snapshot.connected)  # connected=True는 패널이 렌더할 거짓말이다
        self.assertIsNone(snapshot.current)
        self.assertEqual(snapshot.queue, ())
        self.assertIsNone(player.voice_client)
        self.assertEqual(voice.disconnects, 0)  # 이미 끊긴 연결을 다시 끊지 않는다

    async def test_moving_the_bot_to_an_occupied_channel_keeps_playing(self):
        cog = self._cog()
        channel = _FakeVoiceChannel(
            [_FakeVoiceMember(bot=True, user_id=BOT_USER_ID), _FakeVoiceMember(user_id=1)]
        )
        player, voice = await self._connected(cog, channel)

        bot_member = _FakeVoiceMember(bot=True, user_id=BOT_USER_ID)
        destination = _FakeVoiceChannel(
            [bot_member, _FakeVoiceMember(user_id=2)]
        )
        await cog.on_voice_state_update(
            bot_member, _FakeVoiceState(channel), _FakeVoiceState(destination)
        )
        self.assertTrue(player.snapshot().connected)
        self.assertEqual(player.current, _music_track(1))
        self.assertEqual(voice.disconnects, 0)

    async def test_moving_the_bot_to_an_empty_channel_disconnects_and_evicts(self):
        cog = self._cog()
        channel = _FakeVoiceChannel(
            [_FakeVoiceMember(bot=True, user_id=BOT_USER_ID), _FakeVoiceMember(user_id=1)]
        )
        player, voice = await self._connected(cog, channel)
        bot_member = _FakeVoiceMember(bot=True, user_id=BOT_USER_ID)
        destination = _FakeVoiceChannel([bot_member])

        await cog.on_voice_state_update(
            bot_member, _FakeVoiceState(channel), _FakeVoiceState(destination)
        )
        await self._settle()

        self.assertEqual(voice.disconnects, 1)
        self.assertFalse(player.snapshot().connected)
        self.assertNotIn(TEST_GUILD_ID, cog.players)

    async def test_voice_events_from_guilds_without_a_player_are_ignored(self):
        cog = self._cog()
        member = _FakeVoiceMember(user_id=1, guild_id=TEST_GUILD_ID + 1)
        await cog.on_voice_state_update(
            member, _FakeVoiceState(_FakeVoiceChannel()), _FakeVoiceState(None)
        )
        self.assertEqual(cog.players, {})


class _MusicPanelMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _MusicPanelChannel:
    def __init__(self, channel_id=70):
        self.id = channel_id
        self.messages = {}
        self.fetch_error = None
        self.sent = []

    async def fetch_message(self, message_id):
        if self.fetch_error is not None:
            raise self.fetch_error
        message = self.messages.get(message_id)
        if message is None:
            raise discord.NotFound(_FakeResponse(), "missing")
        return message

    async def send(self, **kwargs):
        message = _MusicPanelMessage(100 + len(self.sent))
        self.messages[message.id] = message
        self.sent.append((message, kwargs))
        return message


class _MusicGuild:
    def __init__(self, channel=None):
        self.id = TEST_GUILD_ID
        self.channel = channel or _MusicPanelChannel()
        self.me = SimpleNamespace(id=BOT_USER_ID)
        self.voice_client = None

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None


class _MusicBot:
    def __init__(self, guild):
        self.guild = guild
        self.guilds = [guild]
        self.user = SimpleNamespace(id=BOT_USER_ID)
        self.views = []

    def add_view(self, view):
        self.views.append(view)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


class _MusicInteraction:
    def __init__(
        self,
        guild,
        user_id=123,
        *,
        manager=False,
        voice_channel=None,
        message_id=10,
    ):
        self.guild = guild
        self.guild_id = guild.id
        self.message = SimpleNamespace(id=message_id) if message_id is not None else None
        self.user = SimpleNamespace(
            id=user_id,
            voice=SimpleNamespace(channel=voice_channel),
            guild_permissions=SimpleNamespace(manage_guild=manager),
        )
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()


class MusicPanelTests(_PatchedPlaybackTests):
    def _cog(self, directory):
        settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
        guild = _MusicGuild()
        settings.set_music_channel(guild.id, guild.channel.id)
        settings.set_music_panel_msg(guild.id, 10)
        bot = _MusicBot(guild)
        cog = MusicCog(bot, settings)
        self.addCleanup(drop_panel_locks, guild.id)
        return cog, guild, settings, bot

    async def test_embed_shows_state_requesters_and_only_first_ten_queue_items(self):
        current = MusicTrack("현재 곡", "https://example.com/current", 7)
        queue = tuple(_music_track(number) for number in range(1, 13))
        snapshot = MusicSnapshot(current, queue, paused=True, connected=True)

        embed = music_panel(snapshot)

        self.assertEqual(embed.fields[0].value, "⏸️ 일시정지")
        self.assertIn("현재 곡", embed.fields[1].value)
        self.assertIn("<@7>", embed.fields[1].value)
        self.assertIn("10. 곡10", embed.fields[2].value)
        self.assertNotIn("곡11", embed.fields[2].value)
        self.assertIn("외 2곡", embed.fields[2].value)

    async def test_resolving_snapshot_is_not_rendered_as_idle(self):
        track = _music_track(1)
        embed = music_panel(
            MusicSnapshot(None, (), paused=False, connected=True, resolving=track, starting=True)
        )
        self.assertEqual(embed.fields[0].value, "⏳ 재생 준비 중")
        self.assertIn(track.title, embed.fields[1].value)

    async def test_panel_edits_recreates_on_not_found_and_preserves_id_on_forbidden(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ):
            cog, guild, settings, _ = self._cog(directory)
            original = _MusicPanelMessage(10)
            guild.channel.messages[10] = original
            settings.set_music_panel_msg(guild.id, 10)

            await cog.ensure_panel(guild)
            self.assertTrue(original.edits)
            self.assertEqual(settings.get_music_panel_msg(guild.id), 10)

            guild.channel.messages.pop(10)
            await cog.ensure_panel(guild)
            replacement_id = settings.get_music_panel_msg(guild.id)
            self.assertNotEqual(replacement_id, 10)

            guild.channel.fetch_error = discord.Forbidden(_FakeResponse(403), "no")
            with self.assertLogs(music_cog.logger, level="WARNING") as logged:
                await cog.ensure_panel(guild)
            self.assertEqual(settings.get_music_panel_msg(guild.id), replacement_id)
            self.assertIn("403", "\n".join(logged.output))

    def test_persistent_buttons_and_remove_select_obey_discord_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            cog, _, _, bot = self._cog(directory)
            self.assertIsInstance(bot.views[0], MusicPanelView)
            self.assertTrue(bot.views[0].is_persistent())
            self.assertEqual(
                {item.custom_id for item in bot.views[0].children},
                {"music:add", "music:skip", "music:pause", "music:stop", "music:remove"},
            )
            remove = MusicRemoveView(
                cog, tuple(_music_track(number) for number in range(1, 31))
            )
            self.assertEqual(len(remove.children[0].options), 25)
            self.assertTrue(all(len(option.label) <= 100 for option in remove.children[0].options))
            modal = music_cog.MusicURLModal(cog)
            self.assertEqual(len(modal.children), 1)
            self.assertEqual(modal.children[0].max_length, music_cog.MAX_URL_LENGTH)

    async def test_replaced_panel_denies_old_controls_and_accepts_the_current_panel(self):
        class VoiceChannel:
            def __init__(self):
                self.connects = 0

            def permissions_for(self, member):
                return SimpleNamespace(connect=True, speak=True)

            async def connect(self):
                self.connects += 1

        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ), patch.object(
            music_cog, "extract_track", return_value=_music_track(3)
        ) as extract:
            cog, guild, settings, _ = self._cog(directory)
            player = cog.get_player(guild.id)
            player.current = _music_track(1)
            player.queue.append(_music_track(2))
            before = player.snapshot()
            settings.set_music_panel_msg(guild.id, 20)

            stale = [
                _MusicInteraction(guild, manager=True, message_id=10)
                for _ in range(5)
            ]
            for child, interaction in zip(cog.view.children, stale):
                await child.callback(interaction)

            voice_channel = VoiceChannel()
            stale_modal = _MusicInteraction(
                guild, voice_channel=voice_channel, message_id=None
            )
            await cog.add_url(
                stale_modal,
                "https://example.com/song",
                panel_message_id=10,
            )

            current = _MusicInteraction(guild, message_id=20)
            await cog.view.children[0].callback(current)

        self.assertEqual(player.snapshot(), before)
        self.assertEqual(voice_channel.connects, 0)
        self.assertEqual(extract.await_count, 1)
        self.assertTrue(all("최신" in item.followup.messages[0][0][0] for item in stale[1:4]))
        self.assertIn("최신", stale[0].response.messages[0][0][0])
        self.assertIn("최신", stale[4].response.messages[0][0][0])
        self.assertIn("최신", stale_modal.followup.messages[0][0][0])
        self.assertIsInstance(current.response.modal, music_cog.MusicURLModal)
        self.assertEqual(current.response.modal.panel_message_id, 20)

    async def test_panel_id_is_rechecked_after_waiting_for_music_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            cog, guild, settings, _ = self._cog(directory)
            player = cog.get_player(guild.id)
            player.current = _music_track(1)
            before = player.snapshot()
            interaction = _MusicInteraction(guild, user_id=1, message_id=10)
            deferred = asyncio.Event()
            original_defer = interaction.response.defer

            async def recording_defer(**kwargs):
                await original_defer(**kwargs)
                deferred.set()

            interaction.response.defer = recording_defer
            lock = panel_lock(guild.id, "music")
            await lock.acquire()
            task = asyncio.create_task(cog.skip(interaction))
            await deferred.wait()
            settings.set_music_panel_msg(guild.id, 20)
            lock.release()
            await task

        self.assertEqual(player.snapshot(), before)
        self.assertIn("최신", interaction.followup.messages[0][0][0])

    async def test_permission_denials_do_not_change_player_state(self):
        with tempfile.TemporaryDirectory() as directory:
            cog, guild, _, _ = self._cog(directory)
            player = cog.get_player(guild.id)
            player.current = MusicTrack("남의 곡", "https://example.com/1", 999)
            before = player.snapshot()

            skip = _MusicInteraction(guild, user_id=123)
            await cog.skip(skip)
            pause = _MusicInteraction(guild, user_id=123)
            await cog.pause(pause)
            stop = _MusicInteraction(guild, user_id=123)
            await cog.stop(stop)

        self.assertEqual(player.snapshot(), before)
        for interaction in (skip, pause, stop):
            messages = interaction.followup.messages or interaction.response.messages
            self.assertTrue(messages[0][1]["ephemeral"])

    async def test_invalid_url_and_other_voice_channel_do_not_mutate_or_move_player(self):
        class VoiceChannel:
            def __init__(self):
                self.connects = 0

            def permissions_for(self, member):
                return SimpleNamespace(connect=True, speak=True)

            async def connect(self):
                self.connects += 1

        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ), patch.object(music_cog, "extract_track") as extract:
            cog, guild, _, _ = self._cog(directory)
            requested = VoiceChannel()
            connected_elsewhere = SimpleNamespace(channel=VoiceChannel())
            guild.voice_client = connected_elsewhere
            interaction = _MusicInteraction(guild, voice_channel=requested)
            await cog.add_url(interaction, "https://example.com/song")

            guild.voice_client = None
            invalid = _MusicInteraction(guild, voice_channel=requested)
            await cog.add_url(invalid, "not-a-url")

        extract.assert_not_awaited()
        self.assertEqual(cog.players, {})
        self.assertEqual(requested.connects, 0)
        self.assertTrue(interaction.response.messages[0][1]["ephemeral"])
        self.assertTrue(invalid.response.messages[0][1]["ephemeral"])

    async def test_add_requires_user_voice_and_bot_connect_and_speak_permissions(self):
        class VoiceChannel:
            def __init__(self, connect, speak):
                self.permissions = SimpleNamespace(connect=connect, speak=speak)

            def permissions_for(self, member):
                return self.permissions

        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ), patch.object(music_cog, "extract_track") as extract:
            cog, guild, _, _ = self._cog(directory)
            interactions = [
                _MusicInteraction(guild, voice_channel=None),
                _MusicInteraction(guild, voice_channel=VoiceChannel(False, True)),
                _MusicInteraction(guild, voice_channel=VoiceChannel(True, False)),
            ]
            for interaction in interactions:
                await cog.add_url(interaction, "https://example.com/song")

        extract.assert_not_awaited()
        self.assertEqual(cog.players, {})
        self.assertTrue(
            all(interaction.response.messages[0][1]["ephemeral"] for interaction in interactions)
        )

    async def test_add_revalidates_voice_and_permissions_after_slow_extraction(self):
        class VoiceChannel:
            def __init__(self):
                self.permissions = SimpleNamespace(connect=True, speak=True)
                self.connects = 0

            def permissions_for(self, member):
                return self.permissions

            async def connect(self):
                self.connects += 1

        async def suspend_then_change(cog, guild, change):
            started = asyncio.Event()
            release = asyncio.Event()
            channel = VoiceChannel()
            interaction = _MusicInteraction(guild, voice_channel=channel)

            async def slow_extract(url, requester_id):
                started.set()
                await release.wait()
                return MusicTrack("새 곡", url, requester_id)

            with patch.object(music_cog, "extract_track", side_effect=slow_extract):
                adding = asyncio.create_task(
                    cog.add_url(interaction, "https://example.com/song")
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                change(interaction, channel)
                release.set()
                await asyncio.wait_for(adding, timeout=1)
            return interaction, channel

        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ):
            cog, guild, _, _ = self._cog(directory)
            left, left_channel = await suspend_then_change(
                cog, guild, lambda interaction, channel: setattr(
                    interaction.user.voice, "channel", None
                )
            )
            revoked, revoked_channel = await suspend_then_change(
                cog, guild, lambda interaction, channel: setattr(
                    channel.permissions, "speak", False
                )
            )

        self.assertEqual(left_channel.connects, 0)
        self.assertEqual(revoked_channel.connects, 0)
        self.assertEqual(cog.players, {})
        self.assertIn("바뀌", left.followup.messages[0][0][0])
        self.assertIn("권한", revoked.followup.messages[0][0][0])

    async def test_valid_modal_url_is_extracted_connected_and_queued(self):
        track = MusicTrack("새 곡", "https://example.com/song", 123)

        class VoiceChannel:
            def __init__(self, guild):
                self.guild = guild

            def permissions_for(self, member):
                return SimpleNamespace(connect=True, speak=True)

            async def connect(self):
                voice = _FakeVoiceClient(self)
                self.guild.voice_client = voice
                return voice

        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ), patch.object(
            music_cog, "extract_track", return_value=track
        ) as extract, patch.object(
            MusicPlayer, "start_if_idle", return_value=None
        ) as start:
            cog, guild, settings, _ = self._cog(directory)
            channel = VoiceChannel(guild)
            interaction = _MusicInteraction(guild, voice_channel=channel)
            await cog.add_url(interaction, "https://example.com/song")
            self.assertIsNotNone(settings.get_music_panel_msg(guild.id))

        extract.assert_awaited_once_with("https://example.com/song", 123)
        start.assert_awaited_once_with()
        self.assertEqual(cog.get_player(guild.id).snapshot().queue, (track,))
        self.assertTrue(interaction.followup.messages[0][1]["ephemeral"])

    async def test_initial_voice_play_failure_reports_error_and_preserves_queue(self):
        track = MusicTrack("실패 곡", "https://example.com/song", 123)

        class VoiceChannel:
            def __init__(self, guild):
                self.guild = guild

            def permissions_for(self, member):
                return SimpleNamespace(connect=True, speak=True)

            async def connect(self):
                voice = _FakeVoiceClient(
                    self, play_error=discord.ClientException("voice unavailable")
                )
                self.guild.voice_client = voice
                return voice

        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ), patch.object(
            music_cog, "extract_track", return_value=track
        ):
            cog, guild, _, _ = self._cog(directory)
            interaction = _MusicInteraction(
                guild, voice_channel=VoiceChannel(guild)
            )
            with self.assertLogs(music_cog.logger, level="ERROR"):
                await cog.add_url(interaction, "https://example.com/song")

        snapshot = cog.get_player(guild.id).snapshot()
        self.assertEqual(snapshot.queue, (track,))
        self.assertIsNone(snapshot.current)
        self.assertIn("시작하지 못", snapshot.error)
        self.assertTrue(all(source.cleaned for source in self.sources))
        self.assertIn("대기열은 보존", interaction.followup.messages[0][0][0])
        self.assertNotIn("✅", interaction.followup.messages[0][0][0])

    async def test_automatic_next_track_voice_failure_is_visible_on_panel(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ):
            cog, guild, settings, _ = self._cog(directory)
            voice_channel = _FakeVoiceChannel([_FakeVoiceMember(user_id=1)])
            voice = _FakeVoiceClient(voice_channel)
            player = cog.get_player(guild.id)
            await player.enqueue(voice, _music_track(1))
            await player.enqueue(voice, _music_track(2))
            voice.play_error = discord.ClientException("voice unavailable")

            with self.assertLogs(music_cog.logger, level="ERROR"):
                voice.stop()
                self.assertTrue(
                    await self._wait_until(lambda: player.snapshot().error is not None)
                )
            message = guild.channel.messages[settings.get_music_panel_msg(guild.id)]

        embed = message.edits[-1]["embed"]
        self.assertIn("재생 오류", embed.fields[0].value)
        self.assertEqual(player.snapshot().queue, (_music_track(2),))

    async def test_remove_rechecks_position_and_requester_under_panel_lock(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ):
            cog, guild, _, _ = self._cog(directory)
            player = cog.get_player(guild.id)
            first, second = _music_track(1), _music_track(2)
            player.queue.extend((first, second))
            denied = _MusicInteraction(guild, user_id=999)
            await cog.remove_selected(denied, 1, first)
            stale = _MusicInteraction(guild, user_id=_music_track(1).requester_id)
            await cog.remove_selected(stale, 3, first)
            allowed = _MusicInteraction(guild, user_id=_music_track(1).requester_id)
            await cog.remove_selected(allowed, 1, first)

        self.assertEqual(player.snapshot().queue, (_music_track(2),))
        self.assertIn("요청자", denied.followup.messages[0][0][0])
        self.assertIn("바뀌", stale.followup.messages[0][0][0])
        self.assertIn("제거", allowed.followup.messages[0][0][0])

    async def test_remove_select_rejects_a_shifted_but_still_valid_snapshot_item(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ):
            cog, guild, _, _ = self._cog(directory)
            player = cog.get_player(guild.id)
            first, selected, shifted = _music_track(1), _music_track(2), _music_track(3)
            player.queue.extend((first, selected, shifted))
            select = MusicRemoveView(cog, player.snapshot().queue).children[0]
            player.queue.popleft()
            select._values = ["2"]
            interaction = _MusicInteraction(guild, manager=True)

            await select.callback(interaction)

        self.assertEqual(player.snapshot().queue, (selected, shifted))
        self.assertIn("바뀌", interaction.followup.messages[0][0][0])

    async def test_voice_api_stop_failure_is_ephemeral_logged_and_preserves_metadata(self):
        class FailingVoice(_FakeVoiceClient):
            async def disconnect(self, *, force=False):
                raise discord.ClientException("voice unavailable")

        with tempfile.TemporaryDirectory() as directory:
            cog, guild, _, _ = self._cog(directory)
            player = cog.get_player(guild.id)
            player.voice_client = FailingVoice()
            player.current = _music_track(1)
            player.queue.append(_music_track(2))
            before = player.snapshot()
            interaction = _MusicInteraction(guild, manager=True)

            with self.assertLogs(music_cog.logger, level="ERROR"):
                await cog.stop(interaction)

        self.assertEqual(player.snapshot(), before)
        self.assertIs(cog.players[guild.id], player)
        self.assertTrue(interaction.followup.messages[0][1]["ephemeral"])

    async def test_empty_channel_renders_idle_and_evicts_player(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value=None
        ):
            cog, guild, settings, _ = self._cog(directory)
            bot_member = _FakeVoiceMember(bot=True, user_id=BOT_USER_ID)
            leaving = _FakeVoiceMember(user_id=1)
            channel = _FakeVoiceChannel([bot_member, leaving])
            player = cog.get_player(guild.id)
            voice = _FakeVoiceClient(channel)
            await player.enqueue(voice, _music_track(1))
            channel.members.remove(leaving)
            advance_finished = asyncio.Event()
            original_advance = player._advance

            async def observed_advance():
                try:
                    await original_advance()
                finally:
                    advance_finished.set()

            with patch.object(player, "_advance", side_effect=observed_advance):
                await cog.on_voice_state_update(
                    leaving, _FakeVoiceState(channel), _FakeVoiceState(None)
                )
                await asyncio.wait_for(advance_finished.wait(), timeout=1)
            panel_message_id = settings.get_music_panel_msg(guild.id)
            self.assertIsNotNone(panel_message_id)
            message = guild.channel.messages[panel_message_id]

        self.assertNotIn(guild.id, cog.players)
        embed = message.edits[-1]["embed"]
        self.assertEqual(embed.fields[0].value, "⏹️ 대기 중")

    async def test_startup_restores_each_configured_panel_once(self):
        with tempfile.TemporaryDirectory() as directory:
            cog, guild, _, _ = self._cog(directory)
            with patch.object(cog, "ensure_panel") as ensure:
                await cog.on_ready()
                await cog.on_ready()

        ensure.assert_awaited_once_with(guild)


class RecordingResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False
        self.modal = None

    async def send_message(self, *args, **kwargs):
        if self.messages:
            raise RuntimeError("interaction already has an initial response")
        self.messages.append((args, kwargs))

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def defer(self, **kwargs):
        self.deferred = True

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
        self.addCleanup(drop_panel_locks, guild.id)
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
    def __init__(self, play_cog=None, music_cog=None):
        self.views = []
        self.play_cog = play_cog
        self.music_cog = music_cog

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        if name == "PlayWithCog":
            return self.play_cog
        if name == "MusicCog":
            return self.music_cog
        return None


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
    async def test_channel_settings_reject_unsupported_ambient_channels(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            guildsettings_cog.discord, "TextChannel", _SetupChannel
        ):
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            cog = GuildSettingsCog(_SetupBot(), settings)
            guild = _SetupGuild()
            thread = SimpleNamespace(id=77, mention="<#77>")

            for command in (GuildSettingsCog._party_channel, GuildSettingsCog._music_channel):
                interaction = SimpleNamespace(
                    guild=guild,
                    guild_id=guild.id,
                    channel=thread,
                    response=RecordingResponse(),
                    followup=RecordingFollowup(),
                )
                await command.callback(cog, interaction, None)
                self.assertIn("텍스트 채널", interaction.response.messages[0][0][0])
                self.assertTrue(interaction.response.messages[0][1]["ephemeral"])

            self.assertIsNone(settings.get_party_channel(guild.id))
            self.assertIsNone(settings.get_music_channel(guild.id))

            explicit = _SetupChannel(88, "explicit")
            for command in (GuildSettingsCog._party_channel, GuildSettingsCog._music_channel):
                interaction = SimpleNamespace(
                    guild=guild,
                    guild_id=guild.id,
                    channel=thread,
                    response=RecordingResponse(),
                    followup=RecordingFollowup(),
                )
                await command.callback(cog, interaction, explicit)
                self.assertIn("지정했습니다", interaction.followup.messages[0][0][0])

            self.assertEqual(settings.get_party_channel(guild.id), explicit.id)
            self.assertEqual(settings.get_music_channel(guild.id), explicit.id)

    async def test_setup_completion_ensures_party_and_music_panels(self):
        party_calls = []
        music_calls = []

        async def ensure_panels(guild):
            party_calls.append(guild)

        async def ensure_panel(guild):
            music_calls.append(guild)

        play_cog = SimpleNamespace(ensure_panels=ensure_panels)
        music_panel_cog = SimpleNamespace(ensure_panel=ensure_panel)
        with tempfile.TemporaryDirectory() as directory:
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            guild = _SetupGuild()
            cog = GuildSettingsCog(_SetupBot(play_cog, music_panel_cog), settings)
            await cog._ensure_bot_channels(guild)

        self.assertEqual(party_calls, [guild])
        self.assertEqual(music_calls, [guild])

    async def test_setup_and_settings_show_optional_music_dependency_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            music_cog, "music_dependency_error", return_value="yt-dlp 미설치"
        ), patch(
            "module.guildsettings_cog.music_dependency_error",
            return_value="yt-dlp 미설치",
        ):
            settings = SQLiteGuildSettingsRepository(pathlib.Path(directory) / "settings.db")
            cog = GuildSettingsCog(_SetupBot(), settings)
            guild = _SetupGuild()
            interaction = SimpleNamespace(
                guild=guild,
                guild_id=guild.id,
                response=RecordingResponse(),
                followup=RecordingFollowup(),
            )
            await GuildSettingsCog._start.callback(cog, interaction)

            show = SimpleNamespace(
                guild_id=guild.id,
                response=RecordingResponse(),
            )
            await GuildSettingsCog._show.callback(cog, show)

        self.assertIn("음악 기능 비활성", interaction.followup.messages[0][0][0])
        music_field = show.response.messages[0][1]["embed"].fields[1]
        self.assertIn("yt-dlp 미설치", music_field.value)

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

    async def test_reload_prepares_off_loop_then_publishes_state(self):
        pattern = forbiddenfilter_cog._build_pattern(["새금지어"])
        with patch.object(
            forbiddenfilter_cog.asyncio,
            "to_thread",
            AsyncMock(return_value=(["새금지어"], pattern)),
        ) as to_thread, patch("module.forbiddenfilter_cog.print"):
            loaded = await self.cog.reload_prohibited_words()
        to_thread.assert_awaited_once_with(forbiddenfilter_cog._load_prohibited_pattern)
        self.assertEqual(loaded, ["새금지어"])
        self.assertEqual(self.cog._banned, ["새금지어"])
        self.assertIs(self.cog._banned_pattern, pattern)

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


class _WebSettingsRepository:
    def __init__(self, guild_ids=(), channels=None):
        self.guild_ids = list(guild_ids)
        self.channels = dict(channels or {})

    def list_announcement_guild_ids(self):
        return list(self.guild_ids)

    def get_party_channel(self, guild_id):
        value = self.channels.get(guild_id)
        if isinstance(value, Exception):
            raise value
        return value


class _WebBot:
    def __init__(self):
        self.guilds = {}
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guilds.get(guild_id)

    def get_cog(self, name):
        return self.cogs.get(name)


class WebAdminExtensionTests(unittest.IsolatedAsyncioTestCase):
    def test_web_admin_is_the_only_extension_skipped_without_admin_token(self):
        env = {
            "ADMIN_TOKEN": None,
            "OPENAI_API_KEY": "openai",
            "GOOGLE_API_KEY": "google",
        }
        with patch.object(bot_main, "ENV_VALUES", env):
            names = bot_main.available_extensions()
        self.assertNotIn("module.webadmin_cog", names)
        self.assertIn("module.guildsettings_cog", names)
        self.assertIn("module.hyacine_chat_cog", names)

    def test_web_admin_loads_only_with_admin_token(self):
        env = {
            "ADMIN_TOKEN": "secret",
            "OPENAI_API_KEY": None,
            "GOOGLE_API_KEY": None,
        }
        with patch.object(bot_main, "ENV_VALUES", env):
            names = bot_main.available_extensions()
        self.assertIn("module.webadmin_cog", names)
        self.assertNotIn("module.hyacine_chat_cog", names)

    async def test_fixed_loopback_bind_and_idempotent_cleanup(self):
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        cog = webadmin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.object(webadmin_cog.web, "AppRunner", return_value=runner), patch.object(
            webadmin_cog.web, "TCPSite", return_value=site
        ) as site_factory:
            await cog.start()
            site_factory.assert_called_once_with(runner, "127.0.0.1", 8080)
            await cog.cog_unload()
            await cog.close()  # bot close after extension unload
        runner.cleanup.assert_awaited_once()

    async def test_bot_close_cleans_runner_before_isolated_superclass_close(self):
        events = []
        runner = SimpleNamespace(
            cleanup=AsyncMock(side_effect=lambda: events.append("runner"))
        )
        cog = webadmin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        cog.runner = runner
        bot = object.__new__(bot_main.MyBot)
        superclass_close = AsyncMock(side_effect=lambda: events.append("super"))
        with patch.object(bot_main.MyBot, "get_cog", return_value=cog), patch.object(
            commands.Bot, "close", superclass_close
        ):
            await bot.close()
        runner.cleanup.assert_awaited_once_with()
        superclass_close.assert_awaited_once_with()
        self.assertEqual(events, ["runner", "super"])

    async def test_cancelled_start_cleans_unowned_runner(self):
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock(side_effect=asyncio.CancelledError))
        cog = webadmin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.object(webadmin_cog.web, "AppRunner", return_value=runner), patch.object(
            webadmin_cog.web, "TCPSite", return_value=site
        ):
            with self.assertRaises(asyncio.CancelledError):
                await cog.start()
        runner.cleanup.assert_awaited_once_with()

    async def test_cancelled_runner_setup_is_cleaned(self):
        runner = SimpleNamespace(
            setup=AsyncMock(side_effect=asyncio.CancelledError), cleanup=AsyncMock()
        )
        cog = webadmin_cog.WebAdminCog(_WebBot(), _WebSettingsRepository())
        with patch.object(webadmin_cog.web, "AppRunner", return_value=runner):
            with self.assertRaises(asyncio.CancelledError):
                await cog.start()
        runner.cleanup.assert_awaited_once_with()

    async def test_cancelled_cog_registration_cleans_started_runner(self):
        cog = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        bot = SimpleNamespace(add_cog=AsyncMock(side_effect=asyncio.CancelledError))
        with patch.object(webadmin_cog, "WebAdminCog", return_value=cog):
            with self.assertRaises(asyncio.CancelledError):
                await webadmin_cog.setup(bot)
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
            self.assertEqual(forbiddenfilter_cog.load_forbidden_words(), ["1", "word"])
        self.assertEqual(
            forbiddenfilter_cog.canonicalize_forbidden_words([1, {}, ""]),
            ["1", "{}"],
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
        self.assertEqual(webadmin_cog.WebAdminCog._settings_text("persona.json"), "")
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
        self.cog = webadmin_cog.WebAdminCog(self.bot, self.repository)
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
        session_id = response.cookies[webadmin_cog.SESSION_COOKIE].value
        return session_id, self.cog.sessions[session_id].csrf

    async def test_auth_redirect_headers_cookie_and_non_reflection(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual((response.status, response.headers["Location"]), (302, "/login"))
        for key, value in webadmin_cog.SECURITY_HEADERS.items():
            self.assertEqual(response.headers[key], value)

        response = await self.client.post("/announce", data={})
        self.assertEqual(response.status, 401)
        oversized = await self.client.post(
            "/announce",
            data=b"x" * (webadmin_cog.MAX_BODY_BYTES + 1),
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
            webadmin_cog.secrets,
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
        )[webadmin_cog.SESSION_COOKIE]
        self.assertNotEqual(cookie.value, "operator-secret")

        index = await self.client.get("/")
        page = await index.text()
        self.assertNotIn("operator-secret", page)

    async def test_cookie_attributes_csrf_logout_and_restart_memory(self):
        response = await self.client.post(
            "/login", data={"token": "operator-secret"}, allow_redirects=False
        )
        morsel = response.cookies[webadmin_cog.SESSION_COOKIE]
        self.assertTrue(morsel["httponly"])
        self.assertEqual(morsel["samesite"], "Strict")
        self.assertEqual(morsel["path"], "/")
        self.assertEqual(morsel["max-age"], str(webadmin_cog.SESSION_TTL_SECONDS))
        session_id = morsel.value
        csrf = self.cog.sessions[session_id].csrf

        rejected = await self.client.post(
            "/settings/forbidden_words.json", data={"document": "[]"}
        )
        self.assertEqual(rejected.status, 403)
        rejected = await self.client.post(
            "/logout", data={"csrf": "wrong"}, allow_redirects=False
        )
        self.assertEqual(rejected.status, 403)
        self.assertIn(session_id, self.cog.sessions)

        logout = await self.client.post(
            "/logout", data={"csrf": csrf}, allow_redirects=False
        )
        self.assertEqual(logout.status, 303)
        self.assertNotIn(session_id, self.cog.sessions)
        self.assertEqual(
            logout.cookies[webadmin_cog.SESSION_COOKIE]["max-age"], "0"
        )
        restarted = webadmin_cog.WebAdminCog(self.bot, self.repository)
        self.assertEqual(restarted.sessions, {})

    async def test_session_expiry_relogin_revocation_and_stale_cookie(self):
        with patch.object(webadmin_cog.time, "monotonic", return_value=100.0):
            first_id, _ = await self._login()
        with patch.object(webadmin_cog.time, "monotonic", return_value=101.0):
            second_id, _ = await self._login()
        self.assertNotEqual(first_id, second_id)
        self.assertNotIn(first_id, self.cog.sessions)
        self.assertEqual(list(self.cog.sessions), [second_id])

        self.client.session.cookie_jar.update_cookies(
            {webadmin_cog.SESSION_COOKIE: first_id}, self.client.make_url("/")
        )
        stale = await self.client.get("/", allow_redirects=False)
        self.assertEqual((stale.status, stale.headers["Location"]), (302, "/login"))

        self.client.session.cookie_jar.update_cookies(
            {webadmin_cog.SESSION_COOKIE: second_id}, self.client.make_url("/")
        )
        with patch.object(
            webadmin_cog.time,
            "monotonic",
            return_value=101.0 + webadmin_cog.SESSION_TTL_SECONDS,
        ):
            expired = await self.client.get("/", allow_redirects=False)
        self.assertEqual((expired.status, expired.headers["Location"]), (302, "/login"))
        self.assertEqual(self.cog.sessions, {})

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

        reload_cog = SimpleNamespace(reload_prohibited_words=AsyncMock())
        self.bot.cogs["ForbiddenFilterCog"] = reload_cog
        saved = await self.client.post(
            "/settings/forbidden_words.json",
            data={"csrf": csrf, "document": '["new"]'},
        )
        self.assertIn("다시 불러왔습니다", await saved.text())
        reload_cog.reload_prohibited_words.assert_awaited_once_with()

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
        self.assertIn(session_id, self.cog.sessions)

    def test_template_replaces_original_placeholders_once(self):
        page = self.cog._template(
            "admin_index.html",
            csrf="csrf",
            notice="",
            persona='{"system_prompt":"{{games}}"}',
            forbidden_words='["{{games}}"]',
            games='{"Game":{"max_players":2}}',
        )
        self.assertEqual(page.count("{{games}}"), 2)
        self.assertIn("{&quot;Game&quot;:{&quot;max_players&quot;:2}}", page)

    async def test_content_length_and_actual_body_bytes_are_bounded(self):
        response = await self.client.post(
            "/login",
            data=b"x" * (webadmin_cog.MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status, 413)

        async def slow_chunks():
            yield b"token=operator-secret"
            await asyncio.sleep(0)
            yield b"&padding=" + b"x" * webadmin_cog.MAX_BODY_BYTES

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
            webadmin_cog.logger, "exception"
        ):
            failed = await self.client.get("/")
        self.assertEqual(failed.status, 500)
        for key, value in webadmin_cog.SECURITY_HEADERS.items():
            self.assertEqual(failed.headers[key], value)
        self.assertNotIn(csrf, await failed.text())


class _AnnouncementChannel:
    type = discord.ChannelType.text

    def __init__(self, error=None, allowed=True):
        self.error = error
        self.allowed = allowed
        self.embeds = []

    def permissions_for(self, member):
        return SimpleNamespace(
            view_channel=self.allowed,
            send_messages=self.allowed,
            embed_links=self.allowed,
        )

    async def send(self, *, embed):
        if self.error:
            raise self.error
        self.embeds.append(embed)


class _AnnouncementGuild:
    def __init__(self, channels):
        self.channels = channels
        self.me = object()

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class WebAdminAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = WebAdminHTTPTests.asyncSetUp
    asyncTearDown = WebAdminHTTPTests.asyncTearDown
    _login = WebAdminHTTPTests._login

    async def test_only_opted_in_accessible_channels_receive_isolated_announcement(self):
        session_id, csrf = await self._login()
        sent = _AnnouncementChannel()
        forbidden = _AnnouncementChannel(allowed=False)
        broken = _AnnouncementChannel(error=RuntimeError("discord unavailable"))
        self.repository.guild_ids = [1, 2, 3, 4, 5]
        self.repository.channels = {1: 11, 2: 22, 3: 33, 4: 44, 5: 55}
        self.bot.guilds = {
            1: _AnnouncementGuild({11: sent}),
            2: _AnnouncementGuild({22: forbidden}),
            3: _AnnouncementGuild({}),
            4: _AnnouncementGuild({44: broken}),
            # guild 5 is inaccessible
            999: _AnnouncementGuild({999: _AnnouncementChannel()}),  # opted out
        }
        with patch.object(webadmin_cog.logger, "exception") as logged:
            response = await self.client.post(
                "/announce",
                data={"csrf": csrf, "title": "Title", "body": "Body"},
            )
        page = await response.text()
        self.assertIn("성공 1, 건너뜀 3, 실패 1", page)
        self.assertEqual(len(sent.embeds), 1)
        self.assertEqual((sent.embeds[0].title, sent.embeds[0].description), ("Title", "Body"))
        logged.assert_called_once()
        self.assertEqual(logged.call_args.args[-1], 4)
        self.assertIn(session_id, self.cog.sessions)

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
