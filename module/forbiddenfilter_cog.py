from __future__ import annotations
import asyncio
import discord
import re
import unicodedata
from typing import List, Optional
from discord.ext import commands

from module.config import load_settings_json

# 옵션: 자모 입력(ㅍ ㅔ ㄴ ...)을 완성형으로 결합할지
COMBINE_JAMO = True

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
    금지어(정상 표기) 정규화: 전각/조합 통합 + 소문자
    * 금지어 JSON에는 평범한 표기를 넣는 걸 권장.
    """
    return unicodedata.normalize("NFKC", s).lower()

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


def canonicalize_forbidden_words(data: object, *, strict: bool = False) -> List[str]:
    if not isinstance(data, list):
        if strict:
            raise ValueError("forbidden_words.json 최상단은 배열이어야 합니다.")
        print("⚠️ 금지어 JSON 최상단은 배열이어야 합니다. 필터를 비활성합니다.")
        return []
    return [_normalize_term(str(word)) for word in data if str(word).strip()]


def load_forbidden_words() -> List[str]:
    """금지어 목록을 읽는다. 없거나 비었으면 빈 목록 — 필터만 꺼진다.

    예외를 던지면 setup_hook의 load_extension에서 터져 봇 전체가 죽는다.
    금지어는 선택 기능이므로 부팅을 막을 이유가 없다.
    """
    data = load_settings_json("forbidden_words.json", default=[])
    return canonicalize_forbidden_words(data)


def _load_prohibited_pattern() -> tuple[List[str], re.Pattern]:
    banned = load_forbidden_words()
    return banned, _build_pattern(banned)


class ForbiddenFilterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._banned: List[str] = []
        self._banned_pattern: Optional[re.Pattern] = None
        self.load_prohibited_words()

    def load_prohibited_words(self) -> List[str]:
        """JSON을 읽어 내부 캐시에 저장하고 패턴을 갱신합니다."""
        banned, pattern = _load_prohibited_pattern()
        self._publish_prohibited_words(banned, pattern)
        return banned

    def _publish_prohibited_words(self, banned: List[str], pattern: re.Pattern) -> None:
        self._banned = banned
        self._banned_pattern = pattern
        print(f"📥 금지어 {len(self._banned)}개 로드")

    async def reload_prohibited_words(self) -> List[str]:
        """파일 읽기·정규화·정규식 컴파일은 worker에서, 상태 교체는 loop에서 한다."""
        banned, pattern = await asyncio.to_thread(_load_prohibited_pattern)
        self._publish_prohibited_words(banned, pattern)
        return banned

    def _find_match(self, content: str) -> Optional[str]:
        """금지어를 찾으면 정규화된 형태로 반환한다."""
        if not self._banned or self._banned_pattern is None:
            return None

        # 1차 검사: 기본 정규화 (공백/구두점 제거, 숫자/자모 유지) -> "18", "ㅋㅋㅋ" 등을 잡음
        text_norm = _normalize_message_for_match(content)
        match = self._banned_pattern.search(text_norm)

        # 2차 검사: 공격적 정규화 (숫자/자모 제거) -> "아1니", "아ㅑ니" 등을 잡음
        # 1차에서 걸리지 않았을 때만 수행 (중복 적발 방지)
        if not match:
            text_aggressive = _strip_to_core_chars(text_norm)
            match = self._banned_pattern.search(text_aggressive)

        return match.group() if match else None

    async def _inspect(self, message: discord.Message):
        # forbidden_count는 길드별로 집계된다. DM에는 귀속시킬 길드가 없으므로 건너뛴다.
        if message.guild is None:
            return
        if message.author.bot:
            return

        bad_word = self._find_match(message.content)
        if not bad_word:
            return

        await message.channel.send(
            f"🛑🛑 {message.author.mention} 삐삑~~ 나쁜 단어 **{bad_word}** 금지! 금지! 🛑🛑"
        )

        # Increment forbidden count
        attendance_cog = self.bot.get_cog("AttendanceCog")
        if attendance_cog:
            await attendance_cog.increment_forbidden_count(
                message.guild.id, message.author.id
            )

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
