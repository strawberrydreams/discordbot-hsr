from __future__ import annotations
import discord
import json
import pathlib
import re
import logging
import unicodedata
from typing import List, Optional
from discord.ext import commands

# ─────────── 설정 ─────────── #
DATA_FILE = pathlib.Path(__file__).parents[2] / "settings" / "forbidden_words.json"

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

class ForbiddenFilterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._banned: List[str] = []
        self._banned_pattern: Optional[re.Pattern] = None
        self.load_prohibited_words()

    def _read_words(self) -> List[str]:
        if not DATA_FILE.exists():
            logging.warning("⚠️ %s 파일이 없습니다. 필터가 비활성화됩니다.", DATA_FILE.name)
            return []
        try:
            with DATA_FILE.open(encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, list):
                raise ValueError("JSON 최상단은 배열이어야 합니다.")
            # JSON의 각 항목을 정규화하여 저장
            return [_normalize_term(str(w)) for w in data if str(w).strip()]
        except Exception as e:
            logging.error("❌ %s 로드 실패: %s", DATA_FILE.name, e)
            return []

    def load_prohibited_words(self) -> List[str]:
        """JSON을 읽어 내부 캐시에 저장하고 패턴을 갱신합니다."""
        self._banned = self._read_words()
        self._banned_pattern = _build_pattern(self._banned)
        logging.info("📥 금지어 %d개 로드", len(self._banned))
        return self._banned

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self._banned or self._banned_pattern is None:
            return

        # 메시지 정규화 (공백/구두점/기호/제로폭 제거 포함)
        text_norm = _normalize_message_for_match(message.content)

        # 1차 검사: 기본 정규화 (공백/구두점 제거, 숫자/자모 유지) -> "18", "ㅋㅋㅋ" 등을 잡음
        text_norm = _normalize_message_for_match(message.content)
        match = self._banned_pattern.search(text_norm)
        
        # 2차 검사: 공격적 정규화 (숫자/자모 제거) -> "아1니", "아ㅑ니" 등을 잡음
        # 1차에서 걸리지 않았을 때만 수행 (중복 적발 방지)
        if not match:
            text_aggressive = _strip_to_core_chars(text_norm)
            match = self._banned_pattern.search(text_aggressive)

        if match:
            bad_word = match.group()  # 정규화된 금지어
            await message.channel.send(
                f"🛑🛑 {message.author.mention} 삐삑~~ 나쁜 단어 **{bad_word}** 금지! 금지! 🛑🛑"
            )
            
            # Increment forbidden count
            attendance_cog = self.bot.get_cog("AttendanceCog")
            if attendance_cog:
                attendance_cog.increment_forbidden_count(message.author.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(ForbiddenFilterCog(bot))