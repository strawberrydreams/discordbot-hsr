"""Enka Network 조회를 게임별로 흡수하는 어댑터.

게임마다 클라이언트·모델·UID 형식·진열장 메뉴 경로가 전부 다르다. 명령이
그 차이를 알면 게임이 늘어날 때마다 명령이 부풀어 오르므로, 어댑터가
'조회 → 공통 표시 모델' 변환까지 책임진다. 명령은 ADAPTERS만 안다.

discord를 import하지 않는다. 네트워크 경로를 봇 없이 시험할 수 있어야 한다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import enka

# 응답 캐시 수명. Enka도 ttl을 주지만 게임마다 수 분 단위라 보수적으로 잡는다.
CACHE_TTL_SECONDS = 300.0
# 캐시는 프로세스 메모리다. 별도 캐시 서버는 쓰지 않는다.
CACHE_MAX_ENTRIES = 256
# 연속 실패한 게임은 잠시 쉬게 둔다. 죽은 API를 매 명령마다 두드리지 않는다.
BACKOFF_FAILURE_THRESHOLD = 3
BACKOFF_SECONDS = 60.0


class ShowcaseLookupError(Exception):
    """사용자에게 그대로 보여줄 수 있는 조회 실패."""


@dataclass(frozen=True)
class ShowcaseCharacter:
    name: str
    level: int
    art_url: str


@dataclass(frozen=True)
class Showcase:
    nickname: str
    level: int
    characters: List[ShowcaseCharacter]


@dataclass(frozen=True)
class GameAdapter:
    key: str  # DB에 저장되는 값. 바꾸면 기존 등록이 끊긴다.
    label: str  # 한국어 표시명
    uid_lengths: Tuple[int, ...]
    showcase_help: str  # 진열장이 비었을 때의 게임별 안내
    _client_factory: Callable[[], Any]
    _showcase_converter: Callable[[Any], Showcase]

    def validate_uid(self, uid: str) -> str:
        """형식만 본다. 실존 확인은 조회가 한다."""
        cleaned = uid.strip()
        if (
            not cleaned.isascii()
            or not cleaned.isdecimal()
            or len(cleaned) not in self.uid_lengths
        ):
            allowed_lengths = "·".join(str(length) for length in self.uid_lengths)
            raise ShowcaseLookupError(
                f"{self.label} UID는 ASCII 숫자 {allowed_lengths}자리여야 합니다."
            )
        return cleaned

    def create_client(self):
        return self._client_factory()

    def convert_showcase(self, response: Any) -> Showcase:
        return self._showcase_converter(response)


def _standard_showcase(response: Any) -> Showcase:
    return Showcase(
        nickname=response.player.nickname,
        level=response.player.level,
        characters=[
            ShowcaseCharacter(
                name=character.name,
                level=character.level,
                art_url=character.icon.gacha,
            )
            for character in response.characters
        ],
    )


def _zzz_showcase(response: Any) -> Showcase:
    # ZZZ는 캐릭터를 agent라 부르고 아트 URL 계산도 다르다. 차이를 여기서 흡수한다.
    return Showcase(
        nickname=response.player.nickname,
        level=response.player.level,
        characters=[
            ShowcaseCharacter(
                name=agent.name,
                level=agent.level,
                art_url=agent.icon.image,
            )
            for agent in response.agents
        ],
    )


ADAPTERS: Dict[str, GameAdapter] = {
    adapter.key: adapter
    for adapter in (
        GameAdapter(
            key="hsr",
            label="붕괴: 스타레일",
            uid_lengths=(9,),
            showcase_help=(
                "게임 안에서 **전화 → 프로필 → 진열 캐릭터**를 설정하면 "
                "여기에 표시됩니다."
            ),
            _client_factory=lambda: enka.HSRClient(enka.hsr.Language.KOREAN),
            _showcase_converter=_standard_showcase,
        ),
        GameAdapter(
            key="gi",
            label="원신",
            uid_lengths=(9, 10),
            showcase_help=(
                "게임 안에서 **파티 편성 → 캐릭터 전시**를 설정하고 "
                "**상세 정보 표시**를 켜면 여기에 표시됩니다."
            ),
            _client_factory=lambda: enka.GenshinClient(enka.gi.Language.KOREAN),
            _showcase_converter=_standard_showcase,
        ),
        GameAdapter(
            key="zzz",
            label="젠레스 존 제로",
            uid_lengths=(8, 9, 10),
            showcase_help=(
                "게임 안에서 **프로필 → 명함 편집 → 에이전트 진열**을 설정하면 "
                "여기에 표시됩니다."
            ),
            _client_factory=lambda: enka.ZZZClient(enka.zzz.Language.KOREAN),
            _showcase_converter=_zzz_showcase,
        ),
    )
}


@dataclass
class _BackoffState:
    consecutive_failures: int = 0
    blocked_until: float = 0.0


class ShowcaseService:
    """게임별 enka 클라이언트를 하나씩 살려 두고 조회 결과를 캐시한다.

    클라이언트를 명령마다 새로 만들면 매번 에셋 JSON을 다시 읽는다. 반대로
    봇 기동 시 만들면 네트워크 없는 환경에서 부팅이 막힌다. 첫 사용 시점에
    만들고 계속 쓴다.
    """

    def __init__(self, adapters: Optional[Dict[str, GameAdapter]] = None):
        self._adapters = adapters if adapters is not None else ADAPTERS
        self._clients: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._cache: Dict[Tuple[str, str], Tuple[float, Showcase]] = {}
        self._backoff_by_game: Dict[str, _BackoffState] = {}

    def get_adapter(self, game: str) -> GameAdapter:
        try:
            return self._adapters[game]
        except KeyError:
            raise ShowcaseLookupError("지원하지 않는 게임입니다.") from None

    async def _get_or_create_client(self, adapter: GameAdapter):
        lock = self._locks.setdefault(adapter.key, asyncio.Lock())
        async with lock:
            client = self._clients.get(adapter.key)
            if client is None:
                client = adapter.create_client()
                # start()가 에셋을 내려받는다. 실패하면 클라이언트를 남기지 않는다.
                await client.start()
                self._clients[adapter.key] = client
            return client

    def _check_backoff(self, game: str) -> None:
        backoff = self._backoff_by_game.get(game)
        if backoff is not None and time.monotonic() < backoff.blocked_until:
            raise ShowcaseLookupError(
                "조회가 연달아 실패해 잠시 쉬는 중입니다. 1분 뒤에 다시 시도해 주세요."
            )

    def _record_failure(self, game: str) -> None:
        backoff = self._backoff_by_game.setdefault(game, _BackoffState())
        backoff.consecutive_failures += 1
        if backoff.consecutive_failures >= BACKOFF_FAILURE_THRESHOLD:
            backoff.blocked_until = time.monotonic() + BACKOFF_SECONDS
            backoff.consecutive_failures = 0

    def _get_cached_showcase(self, game: str, uid: str) -> Optional[Showcase]:
        cache_entry = self._cache.get((game, uid))
        if cache_entry is None:
            return None
        expires_at, showcase = cache_entry
        if time.monotonic() >= expires_at:
            del self._cache[(game, uid)]
            return None
        return showcase

    def _cache_showcase(self, game: str, uid: str, showcase: Showcase) -> None:
        # ponytail: 삽입 순서로 가장 오래된 것부터 버린다. LRU가 필요할 규모가 아니다.
        while len(self._cache) >= CACHE_MAX_ENTRIES:
            del self._cache[next(iter(self._cache))]
        self._cache[(game, uid)] = (time.monotonic() + CACHE_TTL_SECONDS, showcase)

    async def fetch_showcase(
        self, game: str, uid: str, *, use_cache: bool = True
    ) -> Showcase:
        adapter = self.get_adapter(game)
        if use_cache:
            cached = self._get_cached_showcase(game, uid)
            if cached is not None:
                return cached
        self._check_backoff(game)

        try:
            client = await self._get_or_create_client(adapter)
            response = await client.fetch_showcase(uid)
        except enka.errors.PlayerDoesNotExistError:
            # 오타는 실패가 아니다. 백오프를 태우지 않는다.
            raise ShowcaseLookupError(
                f"{adapter.label}에 UID `{uid}` 계정이 없습니다. 다시 확인해 주세요."
            ) from None
        except enka.errors.WrongUIDFormatError:
            raise ShowcaseLookupError(f"{adapter.label} UID 형식이 아닙니다.") from None
        except enka.errors.GameMaintenanceError:
            self._record_failure(game)
            raise ShowcaseLookupError("게임이 점검 중입니다. 잠시 뒤에 다시 시도해 주세요.") from None
        except enka.errors.RateLimitedError:
            self._record_failure(game)
            raise ShowcaseLookupError("Enka 요청이 몰려 있습니다. 잠시 뒤에 다시 시도해 주세요.") from None
        except (enka.errors.EnkaAPIError, asyncio.TimeoutError, OSError):
            self._record_failure(game)
            raise ShowcaseLookupError(
                "Enka Network에 연결하지 못했습니다. 잠시 뒤에 다시 시도해 주세요."
            ) from None

        self._backoff_by_game.pop(game, None)
        showcase = adapter.convert_showcase(response)
        self._cache_showcase(game, uid, showcase)
        return showcase

    async def close(self) -> None:
        clients_to_close, self._clients = self._clients, {}
        for client in clients_to_close.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 종료 경로에서 더 할 수 있는 게 없다
                pass
