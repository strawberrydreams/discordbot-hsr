"""Persistent music panel and non-blocking playback core.

yt-dlp와 PyNaCl은 선택 의존성이다. 이 모듈은 두 패키지가 없어도 import되어야
한다(`music_dependency_error()`가 skip 사유를 만들려면 먼저 import돼야 한다).
그래서 무거운 import는 전부 함수 안에서 한다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Optional
from urllib.parse import urlsplit

import discord
from discord.ext import commands

from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)
from module.panel import panel_lock, upsert_panel


logger = logging.getLogger(__name__)

# (import 이름, pip 배포 이름)
MUSIC_PACKAGES = (("nacl", "PyNaCl"), ("yt_dlp", "yt-dlp"))

MAX_TITLE_LENGTH = 100          # Discord select option label 한도
MAX_URL_LENGTH = 2_000          # Discord 메시지 본문 한도
MAX_STREAM_URL_LENGTH = 8_192   # CDN 서명 URL은 길다. 패널에 노출되지 않는다.
MAX_QUEUE_DISPLAY = 10
MAX_REMOVE_OPTIONS = 25

# 스트림은 만료되는 CDN URL이라 끊기기 쉽다. 재연결 없이는 곡 중간에 조용히 죽는다.
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"

_YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,      # 재생목록 URL이라도 단일 항목만 받는다
    "skip_download": True,   # 디스크에 아무것도 남기지 않는다
    "default_search": "error",  # 검색어를 URL로 승격하지 않는다
    "extract_flat": False,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
}


def music_dependency_error() -> Optional[str]:
    """음악 기능을 쓸 수 없으면 skip 사유를, 쓸 수 있으면 None을 돌려준다."""
    missing = []
    for module_name, package in MUSIC_PACKAGES:
        try:
            found = find_spec(module_name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(package)
    if not missing:
        return None
    return (
        f"{', '.join(missing)} 미설치 — "
        f"pip install {' '.join(missing)} 후 다시 시작하세요"
    )


@dataclass(frozen=True, slots=True)
class MusicTrack:
    """재생 목록 항목.

    스트림 URL은 담지 않는다. 만료되는 값이라 대기열에서 기다리는 동안 죽는다.
    재생 직전에 `resolve_stream_url()`로 다시 해석한다.
    """

    title: str
    webpage_url: str
    requester_id: int


@dataclass(frozen=True, slots=True)
class MusicSnapshot:
    """패널이 렌더할 재생 상태의 정지 화면."""

    current: Optional[MusicTrack]
    queue: tuple[MusicTrack, ...]
    paused: bool
    connected: bool
    resolving: Optional[MusicTrack] = None
    starting: bool = False
    error: Optional[str] = None


def _checked_http_url(value: object, label: str, limit: int = MAX_URL_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}이(가) 문자열이 아닙니다.")
    if not 0 < len(value) <= limit:
        raise ValueError(f"{label}의 길이가 1~{limit}자가 아닙니다.")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{label}은(는) http(s) 주소여야 합니다.")
    return value


def _youtube_dl():
    """yt-dlp 핸들. 의존성이 없는 환경에서도 이 모듈이 import되도록 늦게 부른다."""
    import yt_dlp

    return yt_dlp.YoutubeDL(dict(_YTDL_OPTIONS))


def _extract_info(url: str) -> dict:
    with _youtube_dl() as ytdl:
        info = ytdl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError("정보를 가져오지 못했습니다.")
    return info


def _reject_unsupported(info: dict) -> None:
    if info.get("_type") == "playlist" or "entries" in info:
        raise ValueError("재생목록은 지원하지 않습니다. 개별 곡 URL을 넣어 주세요.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("실시간 방송은 지원하지 않습니다.")


async def extract_track(url: str, requester_id: int) -> MusicTrack:
    """단일 HTTP(S) URL 하나를 MusicTrack으로 만든다.

    yt-dlp는 네트워크를 타는 블로킹 호출이라 event loop에서 직접 부르면 봇 전체가
    멈춘다. 반드시 worker thread로 보낸다.
    """
    url = _checked_http_url(url, "URL")
    info = await asyncio.to_thread(_extract_info, url)
    _reject_unsupported(info)

    title = info.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("제목을 가져오지 못했습니다.")
    webpage_url = _checked_http_url(
        info.get("webpage_url") or url, "webpage URL"
    )
    return MusicTrack(
        title=title.strip()[:MAX_TITLE_LENGTH],
        webpage_url=webpage_url,
        requester_id=requester_id,
    )


async def resolve_stream_url(track: MusicTrack) -> str:
    """재생 직전에 만료되지 않은 스트림 URL을 얻는다."""
    info = await asyncio.to_thread(_extract_info, track.webpage_url)
    # 대기열에 있는 동안 영상이 생방송으로 바뀌거나 재생목록으로 대체될 수 있어
    # 추가 시점에 이어 여기서 한 번 더 확인한다.
    _reject_unsupported(info)
    return _checked_http_url(info.get("url"), "스트림 URL", MAX_STREAM_URL_LENGTH)


StateCallback = Callable[[], Awaitable[None]]


class MusicPlayer:
    """길드 하나의 재생 상태.

    별도 task framework를 두지 않는다. deque가 대기열, asyncio.Lock이 상태 전이의
    직렬화, FFmpeg 완료 callback → loop 예약이 다음 곡 진행을 맡는다.
    """

    def __init__(
        self,
        guild_id: int,
        state_callback: Optional[StateCallback] = None,
        state_lock: Optional[asyncio.Lock] = None,
    ):
        self.guild_id = guild_id
        self.queue: deque = deque()
        self.current: Optional[MusicTrack] = None
        self.resolving: Optional[MusicTrack] = None
        self.last_error: Optional[str] = None
        self.voice_client = None
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._state_callback = state_callback
        self._state_lock = state_lock
        # lock 밖에서 도는 곡 시작 작업이 하나뿐임을 보장하는 표식.
        self._starting = False

    def snapshot(self) -> MusicSnapshot:
        voice_client = self.voice_client
        return MusicSnapshot(
            current=self.current,
            queue=tuple(self.queue),
            paused=bool(voice_client is not None and voice_client.is_paused()),
            connected=voice_client is not None,
            resolving=self.resolving,
            starting=self._starting,
            error=self.last_error,
        )

    async def enqueue(self, voice_client, track: MusicTrack) -> None:
        idle = await self.queue_track(voice_client, track)
        if idle:
            await self.start_if_idle()

    async def queue_track(self, voice_client, track: MusicTrack) -> bool:
        """대기열에 추가하고 시작 작업이 필요한지 돌려준다.

        패널 callback은 이 메서드만 panel lock 안에서 부른 뒤 렌더하고,
        시간이 걸리는 stream resolve는 lock 밖에서 시작한다.
        """
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            if self.voice_client is not None and self.voice_client is not voice_client:
                # 다른 채널의 연결을 말없이 버리면 ffmpeg가 계속 먹이는 유령 연결이 남는다.
                await self._disconnect()
                self.queue.clear()
                self.current = None
                self.resolving = None
            self.voice_client = voice_client
            self.queue.append(track)
            return self.current is None and self.resolving is None and not self._starting and not (
                voice_client.is_playing() or voice_client.is_paused()
            )

    async def start_if_idle(self) -> Optional[str]:
        return await self._start_next()

    async def skip(self) -> Optional[MusicTrack]:
        """현재 곡을 끊는다. 다음 곡은 완료 callback이 이어서 건다."""
        async with self._lock:
            skipped = self.current or self.resolving
            voice_client = self.voice_client
            if voice_client is not None and (
                voice_client.is_playing() or voice_client.is_paused()
            ):
                voice_client.stop()
                self.current = None
            elif self.resolving is not None:
                # resolve thread는 취소할 수 없지만, 결과를 버리도록 표식은 즉시 내린다.
                self.resolving = None
            return skipped

    async def toggle_pause(self) -> bool:
        """일시정지 상태를 뒤집고 그 결과를 돌려준다."""
        async with self._lock:
            voice_client = self.voice_client
            if voice_client is None:
                return False
            if voice_client.is_paused():
                voice_client.resume()
            elif voice_client.is_playing():
                voice_client.pause()
            return voice_client.is_paused()

    async def stop(self) -> None:
        async with self._lock:
            await self._disconnect()
            self.queue.clear()
            self.current = None
            self.resolving = None
            self.last_error = None

    async def remove(self, position: int) -> Optional[MusicTrack]:
        """대기열에서 한 곡을 뺀다. position은 UI에 보이는 1-based 번호다."""
        async with self._lock:
            if not isinstance(position, int) or isinstance(position, bool):
                return None
            if not 1 <= position <= len(self.queue):
                return None
            track = self.queue[position - 1]
            del self.queue[position - 1]
            return track

    async def disconnect_if_alone(self, channel=None) -> bool:
        """사람이 아무도 없는 채널에 남아 있으면 idle로 정리하고 나간다."""
        async with self._lock:
            if channel is None:
                channel = getattr(self.voice_client, "channel", None)
            if channel is None:
                return False
            if any(not member.bot for member in getattr(channel, "members", ())):
                return False
            await self._disconnect()
            self.queue.clear()
            self.current = None
            self.resolving = None
            self.last_error = None
            return True

    async def forget_connection(self) -> None:
        """밖에서 끊긴 연결의 잔재를 버린다.

        운영자가 봇을 강제로 내보내면 disconnect할 대상이 이미 없다. 그대로 두면
        죽은 voice client와 재생 목록이 영원히 남고 snapshot이 거짓말을 한다.
        """
        async with self._lock:
            self.queue.clear()
            self.current = None
            self.resolving = None
            self.last_error = None
            self.voice_client = None

    # ── 내부 ──────────────────────────────────────────────────────────── #

    async def _start_next(self) -> Optional[str]:
        """대기열의 다음 곡을 건다.

        스트림 해석은 15초까지 걸리는 네트워크 호출이라 lock 밖에서 한다. lock을
        쥔 채로 기다리면 그동안 눌린 stop/skip 버튼이 Discord의 3초 응답 기한을
        넘긴다. 중복 시작은 lock 대신 `_starting` 표식 하나로 막는다.
        """
        async with self._state_guard():
            async with self._lock:
                if self._starting:
                    return
                self._starting = True

        while True:
            async with self._state_guard():
                async with self._lock:
                    voice_client = self.voice_client
                    busy = voice_client is not None and (
                        voice_client.is_playing() or voice_client.is_paused()
                    )
                    if busy:
                        self._starting = False
                        return
                    self.current = None
                    if voice_client is None or not self.queue:
                        self._starting = False
                        track = None
                    else:
                        track = self.queue.popleft()
                        self.resolving = track
                        self.last_error = None
                await self._notify()
                if track is None:
                    return

            source = None
            try:
                source = discord.FFmpegPCMAudio(
                    await resolve_stream_url(track),
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
            except Exception:
                async with self._state_guard():
                    async with self._lock:
                        if self.resolving is track:
                            self.resolving = None
                    await self._notify()
                if source is not None:
                    source.cleanup()
                logger.exception(
                    "곡 재생을 준비하지 못했습니다: guild=%s url=%s",
                    self.guild_id,
                    track.webpage_url,
                )
                continue

            async with self._state_guard():
                async with self._lock:
                    # stop/skip은 블로킹 resolve를 취소할 수 없다. 결과가 돌아왔을 때
                    # resolving 표식이 달라졌다면 정상 취소이므로 오류 로그 없이 버린다.
                    if self.resolving is not track or self.voice_client is not voice_client:
                        cancelled = True
                    else:
                        cancelled = False
                        try:
                            voice_client.play(source, after=self._schedule_advance)
                        except discord.DiscordException:
                            # voice API 실패는 사용자가 넣은 대기열을 잃게 해서는 안 된다.
                            self.queue.appendleft(track)
                            self.resolving = None
                            self._starting = False
                            self.last_error = "재생 오류 — 음성 재생을 시작하지 못했습니다."
                            source.cleanup()
                            logger.exception(
                                "voice 재생 시작에 실패했습니다: guild=%s", self.guild_id
                            )
                        else:
                            self.current = track
                            self.resolving = None
                            self._starting = False
                if not cancelled:
                    await self._notify()
            if cancelled:
                source.cleanup()
                continue
            return self.last_error

    async def _disconnect(self) -> None:
        voice_client = self.voice_client
        if voice_client is None:
            return
        # 게이트웨이가 멎으면 disconnect가 영영 안 돌아온다. 종료 경로가 매달리면 안 된다.
        await asyncio.wait_for(voice_client.disconnect(force=True), timeout=10)
        self.voice_client = None

    async def _notify(self) -> None:
        if self._state_callback is None:
            return
        try:
            await self._state_callback()
        except Exception:
            logger.exception("음악 패널 상태 알림에 실패했습니다: guild=%s", self.guild_id)

    def _state_guard(self):
        return self._state_lock if self._state_lock is not None else nullcontext()

    def _schedule_advance(self, error: Optional[Exception]) -> None:
        """FFmpeg 완료 callback. discord.py가 별도 thread에서 부른다."""
        if error is not None:
            logger.warning(
                "재생이 오류로 끝났습니다: guild=%s (%s)", self.guild_id, error
            )
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._advance(), loop)

    async def _advance(self) -> None:
        try:
            await self._start_next()
            if self.current is None:
                # 대기열이 말랐다. 남은 사람이 없으면 여기서 나간다. voice event가
                # 오지 않는 경우(게이트웨이 공백 등)에도 혼자 남지 않게 하는 유일한 보루다.
                async with self._state_guard():
                    if await self.disconnect_if_alone():
                        await self._notify()
        except Exception:
            logger.exception("다음 곡으로 넘어가지 못했습니다: guild=%s", self.guild_id)


def _bounded(value: str, limit: int = 1024) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def music_panel(snapshot: MusicSnapshot) -> discord.Embed:
    embed = discord.Embed(title="🎵 음악", color=discord.Color.blurple())
    if snapshot.error is not None:
        state = f"❌ {snapshot.error}"
        track = None
    elif snapshot.resolving is not None:
        state = "⏳ 재생 준비 중"
        track = snapshot.resolving
    elif snapshot.current is not None:
        state = "⏸️ 일시정지" if snapshot.paused else "▶️ 재생 중"
        track = snapshot.current
    else:
        state = "⏹️ 대기 중"
        track = None
    embed.add_field(name="상태", value=state, inline=False)
    embed.add_field(
        name="현재 곡",
        value=(
            f"{_bounded(track.title, 900)}\n요청자: <@{track.requester_id}>"
            if track is not None
            else "없음"
        ),
        inline=False,
    )
    lines = [
        f"{number}. {_bounded(track.title, 65)} — <@{track.requester_id}>"
        for number, track in enumerate(snapshot.queue[:MAX_QUEUE_DISPLAY], 1)
    ]
    if len(snapshot.queue) > MAX_QUEUE_DISPLAY:
        lines.append(f"… 외 {len(snapshot.queue) - MAX_QUEUE_DISPLAY}곡")
    embed.add_field(
        name="대기열",
        value=_bounded("\n".join(lines)) if lines else "비어 있음",
        inline=False,
    )
    return embed


async def _ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class MusicURLModal(discord.ui.Modal, title="음악 URL 추가"):
    url = discord.ui.TextInput(
        label="URL",
        placeholder="https://...",
        required=True,
        max_length=MAX_URL_LENGTH,
    )

    def __init__(self, cog: "MusicCog", panel_message_id: Optional[int] = None):
        super().__init__()
        self.cog = cog
        self.panel_message_id = panel_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.add_url(
            interaction, str(self.url.value), panel_message_id=self.panel_message_id
        )


class MusicRemoveSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "MusicCog",
        tracks: tuple[MusicTrack, ...],
        panel_message_id: Optional[int] = None,
    ):
        self.cog = cog
        self.tracks = tracks[:MAX_REMOVE_OPTIONS]
        self.panel_message_id = panel_message_id
        options = [
            discord.SelectOption(
                label=_bounded(track.title, MAX_TITLE_LENGTH),
                description=f"요청자: {track.requester_id}",
                value=str(position),
            )
            for position, track in enumerate(self.tracks, 1)
        ]
        super().__init__(
            placeholder="대기열에서 제거할 곡을 선택하세요",
            custom_id="music:remove-select",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        position = int(self.values[0])
        await self.cog.remove_selected(
            interaction,
            position,
            self.tracks[position - 1],
            panel_message_id=self.panel_message_id,
        )


class MusicRemoveView(discord.ui.View):
    def __init__(
        self,
        cog: "MusicCog",
        tracks: tuple[MusicTrack, ...],
        panel_message_id: Optional[int] = None,
    ):
        super().__init__(timeout=60)
        self.add_item(MusicRemoveSelect(cog, tracks, panel_message_id))


class MusicPanelView(discord.ui.View):
    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="추가", style=discord.ButtonStyle.primary, custom_id="music:add"
    )
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        panel_message_id = self.cog.interaction_panel_message_id(interaction)
        if not await self.cog.is_current_panel(interaction, panel_message_id):
            return
        await interaction.response.send_modal(MusicURLModal(self.cog, panel_message_id))

    @discord.ui.button(
        label="건너뛰기", style=discord.ButtonStyle.secondary, custom_id="music:skip"
    )
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.skip(interaction)

    @discord.ui.button(
        label="일시정지", style=discord.ButtonStyle.secondary, custom_id="music:pause"
    )
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.pause(interaction)

    @discord.ui.button(
        label="정지", style=discord.ButtonStyle.danger, custom_id="music:stop"
    )
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.stop(interaction)

    @discord.ui.button(
        label="대기열 제거", style=discord.ButtonStyle.secondary, custom_id="music:remove"
    )
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_remove(interaction)


class MusicCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, settings: Optional[GuildSettingsRepository] = None
    ):
        self.bot = bot
        self.settings = settings or create_guild_settings_repository()
        self.players: dict[int, MusicPlayer] = {}
        self.view = MusicPanelView(self)
        self._panels_restored = False
        self._restore_lock = asyncio.Lock()
        add_view = getattr(bot, "add_view", None)
        if add_view is not None:
            add_view(self.view)

    def get_player(self, guild_id: int) -> MusicPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = MusicPlayer(
                guild_id,
                state_callback=lambda: self._player_changed(guild_id),
                state_lock=panel_lock(guild_id, "music"),
            )
            self.players[guild_id] = player
        return player

    @staticmethod
    def interaction_panel_message_id(interaction: discord.Interaction) -> Optional[int]:
        return getattr(getattr(interaction, "message", None), "id", None)

    async def is_current_panel(
        self,
        interaction: discord.Interaction,
        panel_message_id: Optional[int] = None,
    ) -> bool:
        guild_id = interaction.guild_id
        if guild_id is None:
            await _ephemeral(interaction, "❌ 서버 안에서만 사용할 수 있습니다.")
            return False
        if panel_message_id is None:
            panel_message_id = self.interaction_panel_message_id(interaction)
        current_id = await run_db(self.settings.get_music_panel_msg, guild_id)
        if panel_message_id is None or panel_message_id != current_id:
            await _ephemeral(interaction, "❌ 최신 음악 패널에서 다시 시도해 주세요.")
            return False
        return True

    async def _player_changed(self, guild_id: int) -> None:
        player = self.players.get(guild_id)
        if player is not None:
            snapshot = player.snapshot()
            if (
                not snapshot.connected
                and snapshot.current is None
                and snapshot.resolving is None
                and not snapshot.queue
            ):
                self.players.pop(guild_id, None)
        await self.render_panel(guild_id)

    async def ensure_panel(self, guild: discord.Guild) -> None:
        if music_dependency_error() is not None:
            return
        channel_id = await run_db(self.settings.get_music_channel, guild.id)
        if channel_id is None or guild.get_channel(channel_id) is None:
            return
        async with panel_lock(guild.id, "music"):
            await self.render_panel(guild.id)

    async def render_panel(self, guild_id: int) -> None:
        if self.bot is None or music_dependency_error() is not None:
            return
        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(guild_id) if get_guild is not None else None
        if guild is None:
            return
        channel_id = await run_db(self.settings.get_music_channel, guild_id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        message_id = await run_db(self.settings.get_music_panel_msg, guild_id)
        player = self.players.get(guild_id)
        snapshot = player.snapshot() if player is not None else MusicSnapshot(None, (), False, False)
        try:
            message = await upsert_panel(
                channel, message_id, embed=music_panel(snapshot), view=self.view
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "음악 패널을 갱신하지 못했습니다: guild=%s", guild_id, exc_info=True
            )
            return
        if message_id != message.id:
            await run_db(self.settings.set_music_panel_msg, guild_id, message.id)

    async def add_url(
        self,
        interaction: discord.Interaction,
        url: str,
        *,
        panel_message_id: Optional[int] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await _ephemeral(interaction, "❌ 서버 안에서만 사용할 수 있습니다.")
            return
        dependency_error = music_dependency_error()
        if dependency_error is not None:
            await _ephemeral(interaction, f"❌ 음악 기능 비활성: {dependency_error}")
            return
        voice_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if voice_channel is None:
            await _ephemeral(interaction, "❌ 먼저 음성 채널에 들어가 주세요.")
            return
        permissions = voice_channel.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            await _ephemeral(interaction, "❌ 이 음성 채널의 연결·말하기 권한이 필요합니다.")
            return
        voice_client = getattr(guild, "voice_client", None)
        if voice_client is not None and voice_client.channel is not voice_channel:
            await _ephemeral(interaction, "❌ 봇이 다른 음성 채널에서 사용 중입니다.")
            return
        try:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("URL은 http(s) 주소여야 합니다.")
        except (TypeError, ValueError) as exc:
            await _ephemeral(interaction, f"❌ URL 오류: {exc}")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            track = await extract_track(url, interaction.user.id)
        except Exception as exc:
            logger.info("음악 URL 추출 실패: guild=%s (%s)", guild.id, exc)
            await _ephemeral(interaction, f"❌ URL 오류: {exc}")
            return

        start = False
        try:
            async with panel_lock(guild.id, "music"):
                if not await self.is_current_panel(interaction, panel_message_id):
                    return
                current_channel = getattr(
                    getattr(interaction.user, "voice", None), "channel", None
                )
                if current_channel is None or current_channel is not voice_channel:
                    await _ephemeral(
                        interaction, "❌ 음성 채널이 바뀌었습니다. 다시 시도해 주세요."
                    )
                    return
                permissions = current_channel.permissions_for(guild.me)
                if not permissions.connect or not permissions.speak:
                    await _ephemeral(
                        interaction, "❌ 이 음성 채널의 연결·말하기 권한이 필요합니다."
                    )
                    return
                voice_client = getattr(guild, "voice_client", None)
                if voice_client is not None and voice_client.channel is not voice_channel:
                    await _ephemeral(interaction, "❌ 봇이 다른 음성 채널에서 사용 중입니다.")
                    return
                if voice_client is None:
                    voice_client = await voice_channel.connect()
                # 큐에 넣은 player와 시작시킬 player가 반드시 같은 객체여야 한다.
                # get_player()를 두 번 부르면 그 사이 _player_changed()가 idle
                # player를 정리했을 때 방금 넣은 곡이 사라진다.
                player = self.get_player(guild.id)
                start = await player.queue_track(voice_client, track)
                await self.render_panel(guild.id)
        except discord.DiscordException:
            logger.exception("음성 채널 연결에 실패했습니다: guild=%s", guild.id)
            await _ephemeral(interaction, "❌ 음성 채널에 연결하지 못했습니다.")
            return
        if start:
            error = await player.start_if_idle()
            if error is not None:
                await _ephemeral(
                    interaction,
                    f"❌ {error} 대기열은 보존했습니다.",
                )
                return
        await _ephemeral(interaction, f"✅ 대기열에 추가했습니다: {track.title}")

    @staticmethod
    def _manager(interaction: discord.Interaction) -> bool:
        return bool(interaction.user.guild_permissions.manage_guild)

    async def skip(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await _ephemeral(interaction, "❌ 서버 안에서만 사용할 수 있습니다.")
            return
        await interaction.response.defer(ephemeral=True)
        async with panel_lock(guild.id, "music"):
            if not await self.is_current_panel(interaction):
                return
            player = self.players.get(guild.id)
            current = None if player is None else (player.current or player.resolving)
            if current is None:
                await _ephemeral(interaction, "❌ 건너뛸 곡이 없습니다.")
                return
            if current.requester_id != interaction.user.id and not self._manager(interaction):
                await _ephemeral(interaction, "❌ 요청자 또는 서버 관리자만 건너뛸 수 있습니다.")
                return
            try:
                skipped = await player.skip()
            except discord.DiscordException:
                logger.exception("음악 건너뛰기에 실패했습니다: guild=%s", guild.id)
                await _ephemeral(interaction, "❌ 음성 재생을 제어하지 못했습니다.")
                return
            await self.render_panel(guild.id)
        await _ephemeral(interaction, f"⏭️ 건너뛰었습니다: {skipped.title}")

    async def pause(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self._manager(interaction):
            await _ephemeral(interaction, "❌ 서버 관리자만 일시정지할 수 있습니다.")
            return
        await interaction.response.defer(ephemeral=True)
        async with panel_lock(interaction.guild_id, "music"):
            if not await self.is_current_panel(interaction):
                return
            player = self.players.get(interaction.guild_id)
            if player is None or player.current is None:
                await _ephemeral(interaction, "❌ 재생 중인 곡이 없습니다.")
                return
            try:
                paused = await player.toggle_pause()
            except discord.DiscordException:
                logger.exception("음악 일시정지 전환 실패: guild=%s", interaction.guild_id)
                await _ephemeral(interaction, "❌ 음성 재생을 제어하지 못했습니다.")
                return
            await self.render_panel(interaction.guild_id)
        await _ephemeral(interaction, "⏸️ 일시정지했습니다." if paused else "▶️ 재개했습니다.")

    async def stop(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self._manager(interaction):
            await _ephemeral(interaction, "❌ 서버 관리자만 정지할 수 있습니다.")
            return
        await interaction.response.defer(ephemeral=True)
        async with panel_lock(interaction.guild_id, "music"):
            if not await self.is_current_panel(interaction):
                return
            player = self.players.get(interaction.guild_id)
            if player is None:
                await _ephemeral(interaction, "❌ 정지할 재생이 없습니다.")
                return
            try:
                await player.stop()
            except (discord.DiscordException, asyncio.TimeoutError):
                logger.exception("음악 정지에 실패했습니다: guild=%s", interaction.guild_id)
                await _ephemeral(interaction, "❌ 음성 연결을 종료하지 못했습니다.")
                return
            self.players.pop(interaction.guild_id, None)
            await self.render_panel(interaction.guild_id)
        await _ephemeral(interaction, "⏹️ 재생을 정지했습니다.")

    async def show_remove(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await _ephemeral(interaction, "❌ 서버 안에서만 사용할 수 있습니다.")
            return
        panel_message_id = self.interaction_panel_message_id(interaction)
        async with panel_lock(interaction.guild_id, "music"):
            if not await self.is_current_panel(interaction, panel_message_id):
                return
            player = self.players.get(interaction.guild_id)
            queue = () if player is None else player.snapshot().queue[:MAX_REMOVE_OPTIONS]
            if not queue:
                await _ephemeral(interaction, "❌ 대기열이 비어 있습니다.")
                return
        await interaction.response.send_message(
            "제거할 곡을 선택하세요.",
            view=MusicRemoveView(self, queue, panel_message_id),
            ephemeral=True,
        )

    async def remove_selected(
        self,
        interaction: discord.Interaction,
        position: int,
        expected_track: MusicTrack,
        *,
        panel_message_id: Optional[int] = None,
    ) -> None:
        if interaction.guild is None:
            await _ephemeral(interaction, "❌ 서버 안에서만 사용할 수 있습니다.")
            return
        await interaction.response.defer(ephemeral=True)
        async with panel_lock(interaction.guild_id, "music"):
            if not await self.is_current_panel(interaction, panel_message_id):
                return
            player = self.players.get(interaction.guild_id)
            queue = () if player is None else player.snapshot().queue
            if (
                not 1 <= position <= min(len(queue), MAX_REMOVE_OPTIONS)
                or queue[position - 1] is not expected_track
            ):
                await _ephemeral(interaction, "❌ 대기열이 바뀌었습니다. 다시 선택해 주세요.")
                return
            target = queue[position - 1]
            if target.requester_id != interaction.user.id and not self._manager(interaction):
                await _ephemeral(interaction, "❌ 요청자 또는 서버 관리자만 제거할 수 있습니다.")
                return
            removed = await player.remove(position)
            if removed is None:
                await _ephemeral(interaction, "❌ 대기열이 바뀌었습니다. 다시 선택해 주세요.")
                return
            await self.render_panel(interaction.guild_id)
        await _ephemeral(interaction, f"🗑️ 대기열에서 제거했습니다: {removed.title}")

    async def cog_unload(self):
        players = list(self.players.values())
        self.players.clear()
        results = await asyncio.gather(
            *(player.stop() for player in players), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error("종료 중 음악 player 정리 실패: %r", result)

    def _is_self(self, member) -> bool:
        bot_user = getattr(self.bot, "user", None)
        return bot_user is not None and getattr(member, "id", None) == bot_user.id

    @commands.Cog.listener()
    async def on_ready(self):
        if self._panels_restored or self.bot is None:
            return
        async with self._restore_lock:
            if self._panels_restored:
                return
            failed = False
            for guild in self.bot.guilds:
                try:
                    await self.ensure_panel(guild)
                except Exception:
                    failed = True
                    logger.exception("음악 패널 startup 복구 실패: guild=%s", guild.id)
            self._panels_restored = not failed

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        player = self.players.pop(guild.id, None)
        if player is not None:
            try:
                await player.stop()
            except Exception:
                logger.exception("길드 제거 중 음악 player 정리 실패: guild=%s", guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        if before_channel is None or before_channel is after_channel:
            return
        guild = getattr(member, "guild", None)
        player = self.players.get(guild.id) if guild is not None else None
        if player is None:
            return
        async with panel_lock(guild.id, "music"):
            if self._is_self(member):
                if after_channel is None:
                    await player.forget_connection()
                    self.players.pop(guild.id, None)
                    await self.render_panel(guild.id)
                    return
                try:
                    stopped = await player.disconnect_if_alone(after_channel)
                except (discord.DiscordException, asyncio.TimeoutError):
                    logger.exception(
                        "이동된 빈 음성 채널 정리에 실패했습니다: guild=%s", guild.id
                    )
                    return
                if stopped:
                    self.players.pop(guild.id, None)
                    await self.render_panel(guild.id)
                return
            try:
                stopped = await player.disconnect_if_alone()
            except (discord.DiscordException, asyncio.TimeoutError):
                logger.exception("빈 음성 채널 정리에 실패했습니다: guild=%s", guild.id)
                return
            if stopped:
                self.players.pop(guild.id, None)
                await self.render_panel(guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
