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
    queue: tuple[MusicTrack, ...]
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
    # 대기열에 있는 동안 영상이 생방송으로 바뀌거나 재생목록으로 대체될 수 있어
    # 추가 시점에 이어 여기서 한 번 더 확인한다.
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
        # lock 밖에서 도는 곡 시작 작업이 하나뿐임을 보장하는 표식.
        self._starting = False

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
            if self.voice_client is not None and self.voice_client is not voice_client:
                # 다른 채널의 연결을 말없이 버리면 ffmpeg가 계속 먹이는 유령 연결이 남는다.
                self.queue.clear()
                self.current = None
                await self._disconnect()
            self.voice_client = voice_client
            self.queue.append(track)
            idle = self.current is None and not (
                voice_client.is_playing() or voice_client.is_paused()
            )
        if idle:
            await self._start_next()

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

    async def forget_connection(self) -> None:
        """밖에서 끊긴 연결의 잔재를 버린다.

        운영자가 봇을 강제로 내보내면 disconnect할 대상이 이미 없다. 그대로 두면
        죽은 voice client와 재생 목록이 영원히 남고 snapshot이 거짓말을 한다.
        """
        async with self._lock:
            self.queue.clear()
            self.current = None
            self.voice_client = None

    # ── 내부 ──────────────────────────────────────────────────────────── #

    async def _start_next(self) -> None:
        """대기열의 다음 곡을 건다.

        스트림 해석은 15초까지 걸리는 네트워크 호출이라 lock 밖에서 한다. lock을
        쥔 채로 기다리면 그동안 눌린 stop/skip 버튼이 Discord의 3초 응답 기한을
        넘긴다. 중복 시작은 lock 대신 `_starting` 표식 하나로 막는다.
        """
        if self._starting:
            return
        self._starting = True
        try:
            while True:
                async with self._lock:
                    voice_client = self.voice_client
                    # 이미 소리가 나고 있으면 뒤늦게 도착한 완료 callback이다.
                    busy = voice_client is not None and (
                        voice_client.is_playing() or voice_client.is_paused()
                    )
                    if not busy:
                        self.current = None
                    track = (
                        self.queue.popleft()
                        if not busy and self.queue and voice_client is not None
                        else None
                    )
                    if track is None:
                        # 걸 곡이 없다는 판단은 lock 안에서 확정한다. 표식을 여기서
                        # 내려야 방금 enqueue된 곡이 시작되지 못한 채 남지 않는다.
                        self._starting = False
                        return

                source = None
                try:
                    source = discord.FFmpegPCMAudio(
                        await resolve_stream_url(track),
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS,
                    )
                    async with self._lock:
                        if self.voice_client is not voice_client:
                            raise RuntimeError("재생 준비 중 voice 연결이 바뀌었습니다.")
                        voice_client.play(source, after=self._schedule_advance)
                        self.current = track
                    return
                except Exception:
                    # FFmpegPCMAudio는 생성 시점에 프로세스를 띄운다. play()가 실패하면
                    # discord.py가 거둬 갈 기회가 없어 고아 ffmpeg가 남는다.
                    if source is not None:
                        source.cleanup()
                    logger.exception(
                        "곡 재생을 시작하지 못했습니다: guild=%s url=%s",
                        self.guild_id,
                        track.webpage_url,
                    )
        finally:
            self._starting = False

    async def _disconnect(self) -> None:
        voice_client, self.voice_client = self.voice_client, None
        if voice_client is None:
            return
        try:
            # 게이트웨이가 멎으면 disconnect가 영영 안 돌아온다. 종료 경로가 매달리면 안 된다.
            await asyncio.wait_for(voice_client.disconnect(force=True), timeout=10)
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
            await self._start_next()
            if self.current is None:
                # 대기열이 말랐다. 남은 사람이 없으면 여기서 나간다. voice event가
                # 오지 않는 경우(게이트웨이 공백 등)에도 혼자 남지 않게 하는 유일한 보루다.
                await self.disconnect_if_alone()
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
        players = list(self.players.values())
        self.players.clear()
        await asyncio.gather(*(player.stop() for player in players))

    def _is_self(self, member) -> bool:
        bot_user = getattr(self.bot, "user", None)
        return bot_user is not None and getattr(member, "id", None) == bot_user.id

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
        if self._is_self(member):
            # 채널 이동은 voice client가 알아서 따라간다. 완전히 쫓겨난 경우만 정리한다.
            if after_channel is None:
                await player.forget_connection()
            return
        await player.disconnect_if_alone()


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
