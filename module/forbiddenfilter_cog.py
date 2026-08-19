from __future__ import annotations
import asyncio
import discord
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from discord.ext import commands

from module.config import load_settings_json
from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)

logger = logging.getLogger(__name__)

# 옵션: 자모 입력(ㅍ ㅔ ㄴ ...)을 완성형으로 결합할지
COMBINE_JAMO = True

# 채널 메시지 버킷이 5개/5초다. 금지어 연타에 응답을 그대로 내보내면 레이트리밋에
# 걸려 다른 기능까지 막힌다. 창 안의 추가 적발은 응답만 생략하고 카운트는 남긴다.
RESPONSE_COOLDOWN_SECONDS = 10.0

DEFAULT_TEMPLATE = "🛑🛑 {mention} 삐삑~~ 나쁜 단어 **{word}** 금지! 금지! 🛑🛑"
MAX_TERMS_PER_LIST = 1_000
MAX_TERM_CHARS = 100
MAX_RESPONSE_CHARS = 2_000
MAX_MENTION_CHARS = 24

# ─────────── 유틸 ─────────── #
ZERO_WIDTH = {
    "\u200b",  # ZWSP
    "\u200c",  # ZWNJ
    "\u200d",  # ZWJ
    "\u2060",  # Word Joiner
    "\ufeff",  # BOM
}

def _strip_zero_width(s: str) -> str:
    return "".join(ch for ch in s if ch not in ZERO_WIDTH)

def _strip_separators_symbols(s: str) -> str:
    """
    공백/구두점/기호/제어문자 제거 -> 글자/숫자만 남김.
    """
    out = []
    for ch in s:
        cat = unicodedata.category(ch)  # 'L*' 글자, 'N*' 숫자, 'P*' 구두점, 'S*' 기호, 'Z*' 구분자...
        if cat.startswith("L") or cat.startswith("N"):
            out.append(ch)
    return "".join(out)

def _strip_to_core_chars(s: str) -> str:
    """
    공격적인 정규화: 한글 완성형(가-힣)과 영문(a-z)만 남기고 모두 제거.
    숫자, 자모, 특수문자 등을 모두 노이즈로 간주하여 제거함.
    예: '아1니' -> '아니', '아ㅑ니' -> '아니'
    """
    # 가-힣: \uac00-\ud7a3
    # a-z: \u0061-\u007a
    return re.sub(r"[^가-힣a-z]", "", s)

def _normalize_term(s: str) -> str:
    """
    금지어와 허용어를 메시지와 같은 규칙으로 정규화한다.
    """
    return _normalize_message_for_match(s)

def _normalize_message_for_match(s: str) -> str:
    """
    메시지 정규화: 전각/조합 통합 -> 소문자 -> 제로폭 제거 -> 구분자/기호 제거 -> (옵션) NFC 결합
    """
    s = unicodedata.normalize("NFKC", s).lower()
    s = _strip_zero_width(s)
    s = _strip_separators_symbols(s)
    if COMBINE_JAMO:
        s = unicodedata.normalize("NFC", s)
    return s

def _build_pattern(terms: List[str]) -> re.Pattern:
    """
    정규화된 금지어 리스트를 하나의 OR 패턴으로 컴파일 (부분일치)
    """
    if not terms:
        return re.compile(r"^\b$")  # 아무것도 매치되지 않는 더미
    escaped = [re.escape(t) for t in terms if t]
    return re.compile("|".join(escaped))


@dataclass(frozen=True)
class ForbiddenPolicy:
    """금지어 문서 하나가 정하는 것 전부 — 단어, 응답 문구, 예외."""

    words: List[str]
    template: str
    allow: List[str]


EMPTY_POLICY = ForbiddenPolicy(words=[], template=DEFAULT_TEMPLATE, allow=[])


def _normalized_terms(
    raw: object, *, field: str, strict: bool
) -> List[str]:
    if not isinstance(raw, list):
        return []
    if len(raw) > MAX_TERMS_PER_LIST:
        message = f"forbidden_words.json의 {field}는 최대 {MAX_TERMS_PER_LIST:,}개여야 합니다."
        if strict:
            raise ValueError(message)
        print(f"⚠️ {message} 앞 항목만 사용합니다.")
        raw = raw[:MAX_TERMS_PER_LIST]
    normalized = []
    seen = set()
    for term in raw:
        value = _normalize_term(str(term))
        if len(value) > MAX_TERM_CHARS:
            message = (
                f"forbidden_words.json의 {field} 항목은 "
                f"{MAX_TERM_CHARS}자 이하여야 합니다."
            )
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 해당 항목을 건너뜁니다.")
            continue
        if value and value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def _template_fits_discord(template: str) -> bool:
    rendered = template.replace("{mention}", "m" * MAX_MENTION_CHARS).replace(
        "{word}", "w" * MAX_TERM_CHARS
    )
    return len(rendered) <= MAX_RESPONSE_CHARS


def canonicalize_forbidden_policy(
    data: object, *, strict: bool = False
) -> ForbiddenPolicy:
    """배열(구형)과 객체(신형) 두 형태를 모두 받는다.

    기존 운영자의 파일은 배열이다. 그대로 계속 동작해야 하므로 마이그레이션을
    요구하지 않고 두 형태를 함께 받는다.
    """
    if isinstance(data, list):
        return ForbiddenPolicy(
            words=_normalized_terms(data, field="words", strict=strict),
            template=DEFAULT_TEMPLATE,
            allow=[],
        )
    if isinstance(data, dict):
        words = data.get("words")
        if not isinstance(words, list):
            message = "forbidden_words.json 객체에는 배열 words가 있어야 합니다."
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 필터를 비활성합니다.")
            return EMPTY_POLICY
        template = data.get("template", DEFAULT_TEMPLATE)
        if not isinstance(template, str) or not template.strip():
            message = "forbidden_words.json의 template은 비어 있지 않은 문자열이어야 합니다."
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 기본 문구를 씁니다.")
            template = DEFAULT_TEMPLATE
        elif not _template_fits_discord(template):
            message = "forbidden_words.json의 template 응답은 2,000자 이하여야 합니다."
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 기본 문구를 씁니다.")
            template = DEFAULT_TEMPLATE
        allow = data.get("allow", [])
        if not isinstance(allow, list):
            message = "forbidden_words.json의 allow는 배열이어야 합니다."
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 허용 목록을 비웁니다.")
            allow = []
        return ForbiddenPolicy(
            words=_normalized_terms(words, field="words", strict=strict),
            template=template,
            allow=_normalized_terms(allow, field="allow", strict=strict),
        )
    message = "forbidden_words.json 최상단은 배열 또는 객체여야 합니다."
    if strict:
        raise ValueError(message)
    print(f"⚠️ {message} 필터를 비활성합니다.")
    return EMPTY_POLICY


def canonicalize_forbidden_words(data: object, *, strict: bool = False) -> List[str]:
    return canonicalize_forbidden_policy(data, strict=strict).words


def canonicalize_forbidden_document(data: object, *, strict: bool = False) -> object:
    """저장할 문서를 정규화한다. 들어온 형태(배열/객체)를 그대로 돌려준다.

    형태를 바꿔 쓰면 웹 관리에서 저장할 때마다 template과 allow가 사라진다.
    """
    policy = canonicalize_forbidden_policy(data, strict=strict)
    if isinstance(data, dict):
        return {
            "words": policy.words,
            "template": policy.template,
            "allow": policy.allow,
        }
    return policy.words


def load_forbidden_policy() -> ForbiddenPolicy:
    """금지어 설정을 읽는다. 없거나 비었으면 빈 정책 — 필터만 꺼진다.

    예외를 던지면 setup_hook의 load_extension에서 터져 봇 전체가 죽는다.
    금지어는 선택 기능이므로 부팅을 막을 이유가 없다.
    """
    data = load_settings_json("forbidden_words.json", default=[])
    return canonicalize_forbidden_policy(data)


def load_forbidden_words() -> List[str]:
    return load_forbidden_policy().words


def _load_prohibited_pattern() -> tuple[ForbiddenPolicy, re.Pattern, Optional[re.Pattern]]:
    policy = load_forbidden_policy()
    allow_pattern = _build_pattern(policy.allow) if policy.allow else None
    return policy, _build_pattern(policy.words), allow_pattern


class ForbiddenFilterCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        settings: Optional[GuildSettingsRepository] = None,
    ):
        self.bot = bot
        self.settings = settings or create_guild_settings_repository()
        self._policy: ForbiddenPolicy = EMPTY_POLICY
        self._banned_pattern: Optional[re.Pattern] = None
        self._allow_pattern: Optional[re.Pattern] = None
        # 길드 설정은 메시지마다 조회하게 되므로 프로세스에 캐시한다.
        # /설정 금지어가 invalidate_guild로 무효화한다.
        self._enabled_cache: Dict[int, bool] = {}
        # (guild_id, channel_id) -> 마지막 응답 시각. 재시작 시 초기화돼도 무해하다.
        self._last_response: Dict[Tuple[int, int], float] = {}
        self.load_prohibited_words()

    @property
    def _banned(self) -> List[str]:
        return self._policy.words

    def load_prohibited_words(self) -> List[str]:
        """JSON을 읽어 내부 캐시에 저장하고 패턴을 갱신합니다."""
        self._publish_prohibited_words(*_load_prohibited_pattern())
        return self._policy.words

    def _publish_prohibited_words(
        self,
        policy: ForbiddenPolicy,
        pattern: re.Pattern,
        allow_pattern: Optional[re.Pattern],
    ) -> None:
        self._policy = policy
        self._banned_pattern = pattern
        self._allow_pattern = allow_pattern
        print(f"📥 금지어 {len(policy.words)}개 로드 (허용 {len(policy.allow)}개)")

    async def reload_prohibited_words(self) -> List[str]:
        """파일 읽기·정규화·정규식 컴파일은 worker에서, 상태 교체는 loop에서 한다."""
        loaded = await asyncio.to_thread(_load_prohibited_pattern)
        self._publish_prohibited_words(*loaded)
        return self._policy.words

    def invalidate_guild(self, guild_id: int) -> None:
        """/설정 금지어가 값을 바꿨을 때 캐시를 버린다."""
        self._enabled_cache.pop(guild_id, None)

    async def _is_enabled(self, guild_id: int) -> bool:
        enabled = self._enabled_cache.get(guild_id)
        if enabled is None:
            enabled = await run_db(
                self.settings.get_forbidden_filter_enabled, guild_id
            )
            self._enabled_cache[guild_id] = enabled
        return enabled

    def _search(self, pattern: re.Pattern, text: str) -> Optional[re.Match]:
        """허용 목록 구간 안에서 걸린 적발은 건너뛴다.

        ponytail: 허용어끼리 겹치면 finditer가 뒤엣것을 놓친다. 오탐 예외
        목록이라 겹칠 일이 드물고, 필요해지면 그때 스캔을 바꾸면 된다.
        """
        allowed = (
            [m.span() for m in self._allow_pattern.finditer(text)]
            if self._allow_pattern is not None
            else []
        )
        for match in pattern.finditer(text):
            if not any(
                start <= match.start() and match.end() <= end
                for start, end in allowed
            ):
                return match
        return None

    def _find_match(self, content: str) -> Optional[str]:
        """금지어를 찾으면 정규화된 형태로 반환한다."""
        if not self._policy.words or self._banned_pattern is None:
            return None

        # 1차 검사: 기본 정규화 (공백/구두점 제거, 숫자/자모 유지) -> "18", "ㅋㅋㅋ" 등을 잡음
        text_norm = _normalize_message_for_match(content)
        match = self._search(self._banned_pattern, text_norm)

        # 2차 검사: 공격적 정규화 (숫자/자모 제거) -> "아1니", "아ㅑ니" 등을 잡음
        # 1차에서 걸리지 않았을 때만 수행 (중복 적발 방지)
        if not match:
            text_aggressive = _strip_to_core_chars(text_norm)
            match = self._search(self._banned_pattern, text_aggressive)

        return match.group() if match else None

    def _claim_response_slot(self, guild_id: int, channel_id: int) -> bool:
        """쿨다운 창 밖이면 슬롯을 잡고 True. 창 안이면 응답하지 않는다."""
        now = time.monotonic()
        last = self._last_response.get((guild_id, channel_id))
        if last is not None and now - last < RESPONSE_COOLDOWN_SECONDS:
            return False
        self._last_response[(guild_id, channel_id)] = now
        return True

    def _format_response(self, message: discord.Message, bad_word: str) -> str:
        """template의 placeholder만 치환한다.

        str.format을 쓰면 문서에 적힌 임의 속성 경로가 평가된다. 웹 관리에서
        운영자가 넣는 문자열이므로 치환 대상을 두 개로 못박는다.
        """
        return self._policy.template.replace(
            "{mention}", message.author.mention
        ).replace("{word}", bad_word)

    async def _inspect(self, message: discord.Message):
        # forbidden_count는 길드별로 집계된다. DM에는 귀속시킬 길드가 없으므로 건너뛴다.
        if message.guild is None:
            return
        if message.author.bot:
            return
        if not await self._is_enabled(message.guild.id):
            return

        bad_word = self._find_match(message.content)
        if not bad_word:
            return

        # 쿨다운은 응답만 묶는다. 카운트와 전송 실패도 서로 영향을 주지 않는다.
        if self._claim_response_slot(message.guild.id, message.channel.id):
            try:
                await message.channel.send(self._format_response(message, bad_word))
            except Exception:
                logger.exception(
                    "Failed to send forbidden response for guild_id=%s channel_id=%s",
                    message.guild.id,
                    message.channel.id,
                )

        usage_cog = self.bot.get_cog("UsageCog")
        if usage_cog:
            try:
                await usage_cog.increment_forbidden_count(
                    message.guild.id, message.author.id
                )
            except Exception:
                logger.exception(
                    "Failed to count forbidden word for guild_id=%s user_id=%s",
                    message.guild.id,
                    message.author.id,
                )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """길드를 떠나면 그 길드의 프로세스 상태를 남기지 않는다."""
        self._enabled_cache.pop(guild.id, None)
        for key in [key for key in self._last_response if key[0] == guild.id]:
            del self._last_response[key]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._inspect(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """깨끗한 메시지를 올린 뒤 수정해 금지어를 넣는 우회를 막는다."""
        if before.content == after.content:
            return
        if self._find_match(before.content):
            return  # 수정 전에 이미 적발됨
        await self._inspect(after)

async def setup(bot: commands.Bot):
    await bot.add_cog(ForbiddenFilterCog(bot))
