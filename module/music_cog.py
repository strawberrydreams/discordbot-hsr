"""Minimal music playback core.

패널·버튼 UI는 Task 6이 붙인다. 여기에는 재생 엔진만 둔다.

yt-dlp와 PyNaCl은 선택 의존성이다. 이 모듈은 두 패키지가 없어도 import되어야
한다(`music_dependency_error()`가 skip 사유를 만들려면 먼저 import돼야 한다).
그래서 무거운 import는 전부 함수 안에서 한다.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Optional
from urllib.parse import urlsplit

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)

# (import 이름, pip 배포 이름)
MUSIC_PACKAGES = (("nacl", "PyNaCl"), ("yt_dlp", "yt-dlp"))

MAX_TITLE_LENGTH = 100          # Discord select option label 한도
MAX_URL_LENGTH = 2_000          # Discord 메시지 본문 한도
MAX_STREAM_URL_LENGTH = 8_192   # CDN 서명 URL은 길다. 패널에 노출되지 않는다.

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
    queue: tuple
    paused: bool
    connected: bool


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
    _reject_unsupported(info)
    return _checked_http_url(info.get("url"), "스트림 URL", MAX_STREAM_URL_LENGTH)


class MusicPlayer:
    """길드 하나의 재생 상태.

    별도 task framework를 두지 않는다. deque가 대기열, asyncio.Lock이 상태 전이의
    직렬화, FFmpeg 완료 callback → loop 예약이 다음 곡 진행을 맡는다.
    """

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: deque = deque()
        self.current: Optional[MusicTrack] = None
        self.voice_client = None
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def snapshot(self) -> MusicSnapshot:
        voice_client = self.voice_client
        return MusicSnapshot(
            current=self.current,
            queue=tuple(self.queue),
            paused=bool(voice_client is not None and voice_client.is_paused()),
            connected=voice_client is not None,
        )

    async def enqueue(self, voice_client, track: MusicTrack) -> None:
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            self.voice_client = voice_client
            self.queue.append(track)
            if self.current is None and not (
                voice_client.is_playing() or voice_client.is_paused()
            ):
                await self._play_next()

    async def skip(self) -> Optional[MusicTrack]:
        """현재 곡을 끊는다. 다음 곡은 완료 callback이 이어서 건다."""
        async with self._lock:
            skipped = self.current
            voice_client = self.voice_client
            if voice_client is not None and (
                voice_client.is_playing() or voice_client.is_paused()
            ):
                voice_client.stop()
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
            self.queue.clear()
            self.current = None
            await self._disconnect()

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

    async def disconnect_if_alone(self) -> bool:
        """사람이 아무도 없는 채널에 남아 있으면 idle로 정리하고 나간다."""
        async with self._lock:
            channel = getattr(self.voice_client, "channel", None)
            if channel is None:
                return False
            if any(not member.bot for member in getattr(channel, "members", ())):
                return False
            self.queue.clear()
            self.current = None
            await self._disconnect()
            return True

    # ── 내부 (self._lock을 잡은 채로만 호출한다) ────────────────────────── #

    async def _play_next(self) -> None:
        # 스트림 해석(네트워크) 동안 player lock을 쥔 채로 있다. 그 사이 같은 길드의
        # 다른 버튼은 잠깐 기다린다. 곡 시작 중복을 막는 대가로 받아들인 지연이다.
        self.current = None
        while self.queue:
            track = self.queue.popleft()
            voice_client = self.voice_client
            if voice_client is None:
                return
            try:
                stream_url = await resolve_stream_url(track)
                source = discord.FFmpegPCMAudio(
                    stream_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
                voice_client.play(source, after=self._schedule_advance)
            except Exception:
                logger.exception(
                    "곡 재생을 시작하지 못했습니다: guild=%s url=%s",
                    self.guild_id,
                    track.webpage_url,
                )
                continue
            self.current = track
            return

    async def _disconnect(self) -> None:
        voice_client, self.voice_client = self.voice_client, None
        if voice_client is None:
            return
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            logger.exception("voice 연결 해제에 실패했습니다: guild=%s", self.guild_id)

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
            async with self._lock:
                if self.voice_client is not None:
                    await self._play_next()
        except Exception:
            logger.exception("다음 곡으로 넘어가지 못했습니다: guild=%s", self.guild_id)


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}

    def get_player(self, guild_id: int) -> MusicPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = MusicPlayer(guild_id)
            self.players[guild_id] = player
        return player

    async def cog_unload(self):
        for player in list(self.players.values()):
            await player.stop()
        self.players.clear()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        channel = getattr(before, "channel", None)
        if channel is None or channel is getattr(after, "channel", None):
            return
        guild = getattr(member, "guild", None)
        player = self.players.get(guild.id) if guild is not None else None
        if player is not None:
            await player.disconnect_if_alone()


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
