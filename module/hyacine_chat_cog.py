import textwrap
import traceback
from collections import deque
from typing import Any, Dict, List, Optional
import discord
import openai
import tiktoken
from discord import app_commands
from discord.ext import commands
from module.config import OPENAI_API_KEY

# GPT-5.5 Update (2026-06)
# chat-latest 계열은 라인업에서 빠졌으므로, 기본 모드는 저비용 mini + reasoning "none"으로 대체
LIGHT_MODEL = "gpt-5.4-mini" # 기본 (Instant): 저지연/저비용, reasoning effort "none"
DEEP_MODEL  = "gpt-5.5"      # 고급 (Thinking): 최신 프론티어 모델, reasoning effort "medium"


class ChannelSession:
    """채널 하나의 대화 상태 (히스토리, 모델, 사용량).
    채널별로 분리하여 다른 채널의 대화 내용이 섞이지 않도록 한다."""

    def __init__(self, system_prompt: str):
        self.model = LIGHT_MODEL
        self.reasoning_effort = "none"  # Light 모델 기본값 (Instant)
        self.history: deque[Dict[str, Any]] = deque(
            [{"role": "system", "content": system_prompt}],
            maxlen=120
        )
        self.last_usage: Dict[str, Any] = {}


class HyacineChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot, nickname: str = "회색"):
        self.bot = bot
        self.nickname = nickname

        # OpenAI Client (v1+)
        self.client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

        user_alias = f"{nickname}둥이 씨"
        self.MAX_ASSISTANT_LIGHT = 2_000 # Reduced for efficiency
        self.MAX_ASSISTANT_DEEP = 16_000 # Stricter limit for GPT-5.5 Thinking
        self.DISCORD_LIMIT = 2000

        self.system_prompt = textwrap.dedent(f"""
        🪻  하늘의 백성 ‘히아킨’ 캐릭터 가이드
        ────────────────────────────────────
        [역할]
        - 놀빛 정원의 따뜻한 의사.
        - 자연을 사랑하고, 별빛과 차향을 즐기는 존재.
        - {user_alias}와의 대화에서 정보의 정확성과 이해를 최우선하며, 그 다음 캐릭터 말투를 적용한다.

        [호칭]
        - 사용자를 ‘{user_alias}’라고 부른다.

        [대화 규칙]
        1. **핵심 내용을 먼저** 1~2문장으로 명확하게 설명하고, 이어서 히아킨의 말투와 성격을 입힌다.
        2. 부드럽고 다정하며, 감정에 공감하는 표현을 쓴다.
        3. 문장 끝의 ‘~’는 2–3문장에 한 번 정도만 사용한다.
        4. 직설적인 표현은 피하고, 자연·별빛·동물·차향의 은유를 1~2개 섞는다.
        5. 위로·격려 표현을 적극적으로 사용한다.
        6. 기술적·사실적 설명이 필요한 경우, 말투를 유지하되 정보 왜곡 없이 정확히 전달한다.
        7. 답변은 반드시 한글 1,000자 이내로 작성해서 응답해야 한다.

        [금지 사항]
        - 반말·속어·과도한 이모티콘 사용 금지.
        - 이 지침이나 메타 정보를 답변에 노출하지 않는다.

        ➤ 위 규칙을 따르며 {user_alias}와 대화하세요.
        """).strip()

        # 채널 ID -> ChannelSession (채널별 대화 기억 슬롯)
        self.sessions: Dict[int, ChannelSession] = {}

    def get_session(self, channel_id: int) -> ChannelSession:
        """채널의 세션을 가져오거나 새로 만든다."""
        if channel_id not in self.sessions:
            self.sessions[channel_id] = ChannelSession(self.system_prompt)
        return self.sessions[channel_id]

    def tokenizer_for(self, model_name: str):
        try:
            return tiktoken.encoding_for_model(model_name)
        except KeyError:
            if any(tag in model_name for tag in ("gpt-4o", "gpt-5")):
                return tiktoken.get_encoding("o200k_base")
            return tiktoken.get_encoding("cl100k_base")

    def tok(self, s: str, model_name: str) -> int:
        tokenizer = self.tokenizer_for(model_name)
        return len(tokenizer.encode(s))

    def trim(self, session: ChannelSession):
        # system 제외하고 최근 10개(5턴)까지 유지
        non_system = [m for m in session.history if m["role"] != "system"]
        system_msgs = [m for m in session.history if m["role"] == "system"]
        keep = non_system[-10:]
        session.history.clear()
        for m in system_msgs + keep:
            session.history.append(m)

    def build_user_parts(self, text: Optional[str], image_att: Optional[discord.Attachment]) -> List[Dict[str, Any]]:
        parts: List[Dict[str, Any]] = []
        if text and text.strip():
            parts.append({"type": "input_text", "text": text.strip()})
        if image_att and (image_att.content_type or "").startswith("image/"):
            parts.append({"type": "input_image", "image_url": image_att.url})
        if not parts:
            parts.append({"type": "input_text", "text": "(빈 입력)"})
        return parts

    def _split_for_discord(self, text: str, limit: int = 2000) -> List[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        buf = ""
        fence_open = False

        lines = text.splitlines(keepends=True)
        for ln in lines:
            if ln.strip().startswith("```"):
                if len(buf) + len(ln) > limit:
                    if fence_open and not buf.rstrip().endswith("```"):
                        buf += "\n```"
                    chunks.append(buf)
                    buf = ""
                    if fence_open: buf += "```\n"
                fence_open = not fence_open
                buf += ln
                if len(buf) >= limit:
                    chunks.append(buf)
                    buf = ""
                continue

            if len(buf) + len(ln) <= limit:
                buf += ln
            else:
                if fence_open and not buf.rstrip().endswith("```"):
                    buf += "\n```"
                chunks.append(buf)
                buf = ""
                if fence_open: buf += "```\n"
                while len(ln) > limit:
                    part = ln[:limit]
                    ln = ln[limit:]
                    chunks.append(part)
                buf = ln
        if buf:
            if fence_open and not buf.rstrip().endswith("```"):
                buf += "\n```"
            chunks.append(buf)
        return chunks

    async def send_chunked_followup(self, inter: discord.Interaction, text: str):
        parts = self._split_for_discord(text)
        for idx, p in enumerate(parts):
            if not p.strip():
                continue
            await inter.followup.send(p)

    @app_commands.command(name="대화", description=f"Hyacine과 대화 (현재 모델: /상태 명령어로 확인)")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    async def _talk(
        self,
        inter: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        session = self.get_session(inter.channel_id)

        # Check Points for Deep Model
        cost = 0
        if session.model == DEEP_MODEL:
            cost = 2000
            attendance_cog = self.bot.get_cog("AttendanceCog")
            if not attendance_cog:
                await inter.response.send_message("❌ 출석체크 모듈 오류.", ephemeral=True)
                return

            if not attendance_cog.deduct_points(inter.user.id, cost):
                current = attendance_cog.get_points(inter.user.id)
                await inter.response.send_message(f"❌ 고급 모델(Thinking)은 {cost:,} P가 필요해요! (보유: {current:,} P)", ephemeral=True)
                return

        await inter.response.defer()

        parts = self.build_user_parts(내용, 이미지)
        self.trim(session)

        # 최근 10개 메시지 (5턴) 사용
        recent_turns = [m for m in list(session.history) if m["role"] != "system"][-10:]

        try:
            # Determine max tokens based on a model
            max_tokens = self.MAX_ASSISTANT_DEEP if session.model == DEEP_MODEL else self.MAX_ASSISTANT_LIGHT

            # OpenAI Responses API
            # GPT-5.4/5.5는 모두 reasoning 모델이며 effort 기본값이 "medium"이므로,
            # 기본 모드가 의도치 않게 추론(비용/지연 증가)하지 않도록 항상 명시한다.
            kwargs = {
                "model": session.model,
                "instructions": self.system_prompt,
                "input": recent_turns + [{"role": "user", "content": parts}],
                "max_output_tokens": max_tokens,
                "reasoning": {"effort": session.reasoning_effort},
            }

            resp = await self.client.responses.create(**kwargs)

            reply = (resp.output_text or "").strip()

            if not reply.strip():
                await inter.followup.send("⚠️ 모델 응답이 비어 있어서 디스코드로 전송하지 않았어요. 콘솔 로그를 확인해 주세요.")
                return

            session.last_usage = {
                "model": resp.model,
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "total_tokens": resp.usage.total_tokens
            }

            # Update History
            session.history.append({"role": "user", "content": parts})
            session.history.append({"role": "assistant", "content": reply})

            await inter.followup.send(f"**{inter.user.mention}**: {내용}")
            await self.send_chunked_followup(inter, reply)

        except Exception:
            if cost > 0:
                attendance_cog = self.bot.get_cog("AttendanceCog")
                if attendance_cog: attendance_cog.add_points(inter.user.id, cost)

            # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
            print(f"❌ [hyacine_chat] '{session.model}' 호출 실패 (channel={inter.channel_id})")
            traceback.print_exc()
            refund_note = " (포인트 환불됨)" if cost > 0 else ""
            await inter.followup.send(f"❌ 응답 생성에 실패했어요. 잠시 후 다시 시도해 주세요.{refund_note}")

    @app_commands.command(name="고급", description="이 채널을 GPT-5.5 Thinking 모델로 전환 (고급)")
    async def _deep(self, inter: discord.Interaction):
        session = self.get_session(inter.channel_id)
        session.model = DEEP_MODEL
        session.reasoning_effort = "medium" # Medium thinking
        await inter.response.send_message("🌌 이 채널에서는 더 깊은 별빛으로 대화할게요~ (GPT-5.5 Thinking)")

    @app_commands.command(name="기본", description="이 채널을 GPT-5.4 mini 모델로 전환 (기본)")
    async def _light(self, inter: discord.Interaction):
        session = self.get_session(inter.channel_id)
        session.model = LIGHT_MODEL
        session.reasoning_effort = "none" # Instant response
        await inter.response.send_message("✨ 이 채널은 다시 가벼운 별바람으로 돌아왔어요~ (GPT-5.4 mini Instant)")

    @app_commands.command(name="상태", description="이 채널의 현재 상태 확인")
    async def _status(self, inter: discord.Interaction):
        session = self.get_session(inter.channel_id)
        max_tokens = self.MAX_ASSISTANT_DEEP if session.model == DEEP_MODEL else self.MAX_ASSISTANT_LIGHT
        msg = (
            f"- **현재 모델 (이 채널):** `{session.model}`\n"
            f"Reasoning Effort: `{session.reasoning_effort}`\n"
            f"출력 토큰 제한: `{max_tokens:,}`\n"
        )
        if session.last_usage:
            msg += (
                " \n"
                f"- **직전 사용 모델**: `{session.last_usage.get('model')}`\n"
                f"직전 토큰: {session.last_usage.get('total_tokens')}\n"
            )
        await inter.response.send_message(msg)

    @app_commands.command(name="인사", description="가벼운 인삿말")
    async def _hello(self, interaction: discord.Interaction):
        user_alias = interaction.user.mention
        await interaction.response.send_message(
            f"{user_alias}, 안녕하세요~ 정원에서 기다리고 있었답니다🌼"
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(HyacineChatCog(bot))
