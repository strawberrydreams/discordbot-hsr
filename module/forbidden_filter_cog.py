from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from module.config import load_settings_json
from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)
from module.usage_cog import UsageCog

logger = logging.getLogger(__name__)

# 옵션: 자모 입력(ㅍ ㅔ ㄴ ...)을 완성형으로 결합할지
COMBINE_JAMO = True

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

def _strip_zero_width(text: str) -> str:
    return "".join(
        character for character in text if character not in ZERO_WIDTH
    )

def _strip_separators_symbols(text: str) -> str:
    """
    공백/구두점/기호/제어문자 제거 -> 글자/숫자만 남김.
    """
    normalized_characters = []
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("L") or category.startswith("N"):
            normalized_characters.append(character)
    return "".join(normalized_characters)

def _strip_to_core_chars(text: str) -> str:
    """
    공격적인 정규화: 한글 완성형(가-힣)과 영문(a-z)만 남기고 모두 제거.
    숫자, 자모, 특수문자 등을 모두 노이즈로 간주하여 제거함.
    예: '아1니' -> '아니', '아ㅑ니' -> '아니'
    """
    # 가-힣: \uac00-\ud7a3
    # a-z: \u0061-\u007a
    return re.sub(r"[^가-힣a-z]", "", text)

def _normalize_term(term: str) -> str:
    """
    금지어와 허용어를 메시지와 같은 규칙으로 정규화한다.
    """
    return _normalize_message_for_match(term)

def _normalize_message_for_match(text: str) -> str:
    """
    메시지 정규화: 전각/조합 통합 -> 소문자 -> 제로폭 제거 -> 구분자/기호 제거 -> (옵션) NFC 결합
    """
    normalized_text = unicodedata.normalize("NFKC", text).lower()
    normalized_text = _strip_zero_width(normalized_text)
    normalized_text = _strip_separators_symbols(normalized_text)
    if COMBINE_JAMO:
        normalized_text = unicodedata.normalize("NFC", normalized_text)
    return normalized_text

def _build_pattern(terms: List[str]) -> re.Pattern:
    """
    정규화된 금지어 리스트를 하나의 OR 패턴으로 컴파일 (부분일치)
    """
    if not terms:
        return re.compile(r"^\b$")  # 아무것도 매치되지 않는 더미
    escaped_terms = [re.escape(term) for term in terms if term]
    return re.compile("|".join(escaped_terms))


@dataclass(frozen=True)
class ForbiddenPolicy:
    """금지어 문서 하나가 정하는 것 전부 — 단어, 응답 문구, 예외."""

    words: List[str]
    template: str
    allow: List[str]


EMPTY_POLICY = ForbiddenPolicy(words=[], template=DEFAULT_TEMPLATE, allow=[])


def _normalized_terms(
    raw_terms: object, *, field: str, strict: bool
) -> List[str]:
    if not isinstance(raw_terms, list):
        return []
    if len(raw_terms) > MAX_TERMS_PER_LIST:
        message = f"forbidden_words.json의 {field}는 최대 {MAX_TERMS_PER_LIST:,}개여야 합니다."
        if strict:
            raise ValueError(message)
        print(f"⚠️ {message} 앞 항목만 사용합니다.")
        raw_terms = raw_terms[:MAX_TERMS_PER_LIST]
    normalized = []
    seen = set()
    for term in raw_terms:
        normalized_term = _normalize_term(str(term))
        if len(normalized_term) > MAX_TERM_CHARS:
            message = (
                f"forbidden_words.json의 {field} 항목은 "
                f"{MAX_TERM_CHARS}자 이하여야 합니다."
            )
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 해당 항목을 건너뜁니다.")
            continue
        if normalized_term and normalized_term not in seen:
            normalized.append(normalized_term)
            seen.add(normalized_term)
    return normalized


def _template_fits_discord(template: str) -> bool:
    rendered = template.replace("{mention}", "m" * MAX_MENTION_CHARS).replace(
        "{word}", "w" * MAX_TERM_CHARS
    )
    return len(rendered) <= MAX_RESPONSE_CHARS


def canonicalize_forbidden_policy(
    document: object, *, strict: bool = False
) -> ForbiddenPolicy:
    """배열(구형)과 객체(신형) 두 형태를 모두 받는다.

    기존 운영자의 파일은 배열이다. 그대로 계속 동작해야 하므로 마이그레이션을
    요구하지 않고 두 형태를 함께 받는다.
    """
    if isinstance(document, list):
        return ForbiddenPolicy(
            words=_normalized_terms(document, field="words", strict=strict),
            template=DEFAULT_TEMPLATE,
            allow=[],
        )
    if isinstance(document, dict):
        words = document.get("words")
        if not isinstance(words, list):
            message = "forbidden_words.json 객체에는 배열 words가 있어야 합니다."
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 필터를 비활성합니다.")
            return EMPTY_POLICY
        template = document.get("template", DEFAULT_TEMPLATE)
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
        allow = document.get("allow", [])
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


def canonicalize_forbidden_words(
    document: object, *, strict: bool = False
) -> List[str]:
    return canonicalize_forbidden_policy(document, strict=strict).words


def canonicalize_forbidden_document(
    document: object, *, strict: bool = False
) -> object:
    """저장할 문서를 정규화한다. 들어온 형태(배열/객체)를 그대로 돌려준다.

    형태를 바꿔 쓰면 웹 관리에서 저장할 때마다 template과 allow가 사라진다.
    """
    policy = canonicalize_forbidden_policy(document, strict=strict)
    if isinstance(document, dict):
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
    document = load_settings_json("forbidden_words.json", default=[])
    return canonicalize_forbidden_policy(document)


def load_forbidden_words() -> List[str]:
    return load_forbidden_policy().words


def _load_filter_state() -> tuple[
    ForbiddenPolicy, re.Pattern, Optional[re.Pattern]
]:
    policy = load_forbidden_policy()
    allow_pattern = _build_pattern(policy.allow) if policy.allow else None
    return policy, _build_pattern(policy.words), allow_pattern


class ForbiddenFilterCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        settings_repository: Optional[GuildSettingsRepository] = None,
    ):
        self.bot = bot
        self.settings_repository = (
            settings_repository or create_guild_settings_repository()
        )
        self._policy: ForbiddenPolicy = EMPTY_POLICY
        self._forbidden_pattern: Optional[re.Pattern] = None
        self._allow_pattern: Optional[re.Pattern] = None
        # 길드 설정은 메시지마다 조회하게 되므로 프로세스에 캐시한다.
        # 웹 길드 설정 저장이 invalidate_guild로 무효화한다.
        self._enabled_by_guild: Dict[int, bool] = {}
        self.refresh_forbidden_words()

    @property
    def _forbidden_words(self) -> List[str]:
        return self._policy.words

    def refresh_forbidden_words(self) -> List[str]:
        """JSON을 읽어 내부 캐시에 저장하고 패턴을 갱신합니다."""
        self._publish_filter_state(*_load_filter_state())
        return self._policy.words

    def _publish_filter_state(
        self,
        policy: ForbiddenPolicy,
        pattern: re.Pattern,
        allow_pattern: Optional[re.Pattern],
    ) -> None:
        self._policy = policy
        self._forbidden_pattern = pattern
        self._allow_pattern = allow_pattern
        print(f"📥 금지어 {len(policy.words)}개 로드 (허용 {len(policy.allow)}개)")

    async def reload_forbidden_words(self) -> List[str]:
        """파일 읽기·정규화·정규식 컴파일은 worker에서, 상태 교체는 loop에서 한다."""
        loaded_state = await asyncio.to_thread(_load_filter_state)
        self._publish_filter_state(*loaded_state)
        return self._policy.words

    def invalidate_guild(self, guild_id: int) -> None:
        """웹 관리가 길드별 사용 여부를 바꿨을 때 캐시를 버린다."""
        self._enabled_by_guild.pop(guild_id, None)

    async def _is_filter_enabled(self, guild_id: int) -> bool:
        enabled = self._enabled_by_guild.get(guild_id)
        if enabled is None:
            enabled = await run_db(
                self.settings_repository.get_forbidden_filter_enabled, guild_id
            )
            self._enabled_by_guild[guild_id] = enabled
        return enabled

    def _search_outside_allowlist(
        self, pattern: re.Pattern, text: str
    ) -> Optional[re.Match]:
        """허용 목록 구간 안에서 걸린 적발은 건너뛴다.

        ponytail: 허용어끼리 겹치면 finditer가 뒤엣것을 놓친다. 오탐 예외
        목록이라 겹칠 일이 드물고, 필요해지면 그때 스캔을 바꾸면 된다.
        """
        allowlist_spans = (
            [match.span() for match in self._allow_pattern.finditer(text)]
            if self._allow_pattern is not None
            else []
        )
        for match in pattern.finditer(text):
            if not any(
                start <= match.start() and match.end() <= end
                for start, end in allowlist_spans
            ):
                return match
        return None

    def _find_forbidden_word(self, content: str) -> Optional[str]:
        """금지어를 찾으면 정규화된 형태로 반환한다."""
        if not self._policy.words or self._forbidden_pattern is None:
            return None

        # 1차 검사: 기본 정규화 (공백/구두점 제거, 숫자/자모 유지) -> "18", "ㅋㅋㅋ" 등을 잡음
        normalized_text = _normalize_message_for_match(content)
        match = self._search_outside_allowlist(
            self._forbidden_pattern, normalized_text
        )

        # 2차 검사: 공격적 정규화 (숫자/자모 제거) -> "아1니", "아ㅑ니" 등을 잡음
        # 1차에서 걸리지 않았을 때만 수행 (중복 적발 방지)
        if not match:
            aggressively_normalized_text = _strip_to_core_chars(normalized_text)
            match = self._search_outside_allowlist(
                self._forbidden_pattern, aggressively_normalized_text
            )

        return match.group() if match else None

    def _format_response(
        self, message: discord.Message, forbidden_word: str
    ) -> str:
        """template의 placeholder만 치환한다.

        str.format을 쓰면 문서에 적힌 임의 속성 경로가 평가된다. 웹 관리에서
        운영자가 넣는 문자열이므로 치환 대상을 두 개로 못박는다.
        """
        return self._policy.template.replace(
            "{mention}", message.author.mention
        ).replace("{word}", forbidden_word)

    async def _inspect_message(self, message: discord.Message):
        # forbidden_count는 길드별로 집계된다. DM에는 귀속시킬 길드가 없으므로 건너뛴다.
        if message.guild is None:
            return
        if message.author.bot:
            return
        if not await self._is_filter_enabled(message.guild.id):
            return

        forbidden_word = self._find_forbidden_word(message.content)
        if not forbidden_word:
            return

        try:
            await message.channel.send(
                self._format_response(message, forbidden_word)
            )
        except Exception:
            logger.exception(
                "Failed to send forbidden response for guild_id=%s channel_id=%s",
                message.guild.id,
                message.channel.id,
            )

        usage_cog = self.bot.get_cog(UsageCog.__name__)
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
        self._enabled_by_guild.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._inspect_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """깨끗한 메시지를 올린 뒤 수정해 금지어를 넣는 우회를 막는다."""
        if before.content == after.content:
            return
        if self._find_forbidden_word(before.content):
            return  # 수정 전에 이미 적발됨
        await self._inspect_message(after)

async def setup(bot: commands.Bot):
    await bot.add_cog(ForbiddenFilterCog(bot))
