import asyncio
import traceback
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional
import discord
import openai
import tiktoken
from discord import app_commands
from discord.ext import commands
from module.config import (
    AI_COOLDOWN_SECONDS,
    CHAT_MODEL_DEEP as DEEP_MODEL,
    CHAT_MODEL_LIGHT as LIGHT_MODEL,
    LIMIT_DEEP,
    LIMIT_IMAGE,
    LIMIT_LIGHT,
    OPENAI_API_KEY,
    load_settings_json,
)

# 포인트 수입원은 /출석 하나뿐이다. 가격이 곧 하루 사용 빈도 상한이 된다.
LIGHT_COST = 200
DEEP_COST = 2_000

DEFAULT_PERSONA = {
    "system_prompt": "당신은 놀빛 정원의 따뜻한 의사, 하늘의 백성 히아킨입니다. 사용자를 '회색둥이 씨'라 부르며, 한국어로 정확하고 다정하게 답하세요.",
    "greeting": "안녕하세요~ 정원에서 기다리고 있었답니다🌼",
}


def load_persona() -> dict:
    """settings/persona.json → persona.example.json → 코드 기본값 순으로 읽는다."""
    data = load_settings_json("persona.json", "persona.example.json", default={})
    if not isinstance(data, dict):
        data = {}
    return {**DEFAULT_PERSONA, **{k: v for k, v in data.items() if isinstance(v, str) and v}}


class ChannelSession:
    """채널 하나의 대화 상태 (히스토리, 사용량).
    채널별로 분리하여 다른 채널의 대화 내용이 섞이지 않도록 한다."""

    def __init__(self, system_prompt: str, greeting: str):
        self.system_prompt = system_prompt
        self.greeting = greeting
        self.history: deque[Dict[str, Any]] = deque(
            [{"role": "system", "content": system_prompt}],
            maxlen=120
        )
        self.last_usage: Dict[str, Any] = {}
        # 같은 채널의 동시 호출이 히스토리를 읽고 완료 순서대로 append하면 턴이 어긋난다.
        self.lock = asyncio.Lock()


class HyacineChatCog(commands.Cog):
    MAX_CHANNEL_SESSIONS = 100

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

        self.MAX_ASSISTANT_LIGHT = 2_000 # Reduced for efficiency
        self.MAX_ASSISTANT_DEEP = 16_000 # Stricter limit for GPT-5.5 Thinking
        self.DISCORD_LIMIT = 2000

        persona = load_persona()
        self.system_prompt = persona["system_prompt"]
        self.greeting = persona["greeting"]

        # 채널 ID -> ChannelSession (채널별 대화 기억 슬롯)
        self.sessions: OrderedDict[int, ChannelSession] = OrderedDict()

    def get_session(self, channel_id: int) -> ChannelSession:
        """채널의 세션을 가져오거나 새로 만든다."""
        session = self.sessions.pop(channel_id, None)
        if session is None:
            persona = load_persona()
            self.system_prompt = persona["system_prompt"]
            self.greeting = persona["greeting"]
            session = ChannelSession(self.system_prompt, self.greeting)
        self.sessions[channel_id] = session
        while len(self.sessions) > self.MAX_CHANNEL_SESSIONS:
            # 응답 대기 중인 세션을 버리면 코루틴이 고아 객체에 append해 턴이 유실된다.
            for candidate_id, candidate in self.sessions.items():
                if not candidate.lock.locked():
                    del self.sessions[candidate_id]
                    break
            else:
                break  # 전부 사용 중이면 이번 사이클은 축출하지 않음
        return session

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

    async def _run_talk(
        self,
        inter: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment],
        model: str,
        reasoning_effort: str,
        cost: int,
        usage_command: str,
        daily_limit: int,
    ):
        session = self.get_session(inter.channel_id)
        attendance_cog = self.bot.get_cog("AttendanceCog") if self.bot else None
        charged = False
        refunded = False
        api_started = False

        if not attendance_cog:
            await inter.response.send_message("❌ 출석체크 모듈 오류.", ephemeral=True)
            return

        reservation = await attendance_cog.reserve_ai_usage(
            inter.user.id, usage_command, daily_limit
        )
        if reservation is None:
            await inter.response.send_message(
                "오늘 사용 횟수를 모두 사용했어요.", ephemeral=True
            )
            return
        usage_date, _ = reservation

        async def release_usage():
            if not api_started:
                try:
                    await attendance_cog.release_ai_usage(
                        inter.user.id, usage_date, usage_command
                    )
                except Exception:
                    print(
                        "❌ [hyacine_chat] 일일 사용량 반환 실패 "
                        f"(user={inter.user.id}, command={usage_command})"
                    )
                    traceback.print_exc()

        async def refund_points():
            nonlocal refunded
            if not charged or refunded:
                return refunded
            try:
                await attendance_cog.add_points(
                    inter.guild_id, inter.user.id, cost, f"chat_refund:{model}"
                )
            except Exception:
                print(f"❌ [hyacine_chat] 포인트 환불 실패 (channel={inter.channel_id})")
                traceback.print_exc()
                return False
            refunded = True
            return True

        try:
            if cost > 0:
                if not await attendance_cog.deduct_points(
                    inter.guild_id, inter.user.id, cost, f"chat:{model}"
                ):
                    current = await attendance_cog.get_points(inter.guild_id, inter.user.id)
                    await inter.response.send_message(f"❌ 이 명령은 {cost:,} P가 필요해요! (보유: {current:,} P)", ephemeral=True)
                    return
                charged = True

            await inter.response.defer()

            parts = self.build_user_parts(내용, 이미지)

            # 포인트 차감과 defer()는 락 밖에 둬서 대기 중에도 인터랙션이 만료되지 않게 한다.
            async with session.lock:
                self.trim(session)

                # 최근 10개 메시지 (5턴) 사용
                recent_turns = [m for m in list(session.history) if m["role"] != "system"][-10:]

                max_tokens = self.MAX_ASSISTANT_DEEP if model == DEEP_MODEL else self.MAX_ASSISTANT_LIGHT

                kwargs = {
                    "model": model,
                    "instructions": session.system_prompt,
                    "input": recent_turns + [{"role": "user", "content": parts}],
                    "max_output_tokens": max_tokens,
                    "reasoning": {"effort": reasoning_effort},
                }

                # ponytail: API 호출이 시작되면 실패해도 일일 한도를 소비한다.
                # provider 사용량 대조가 필요해질 때 request ID 원장을 추가한다.
                api_started = True
                resp = await self.client.responses.create(**kwargs)

                reply = (resp.output_text or "").strip()

                if not reply.strip():
                    refund_note = " (포인트 환불됨)" if await refund_points() else ""
                    await inter.followup.send(
                        "⚠️ 모델 응답이 비어 있어서 디스코드로 전송하지 않았어요. 콘솔 로그를 확인해 주세요."
                        + refund_note
                    )
                    return

                session.last_usage = {
                    "model": resp.model,
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                    "total_tokens": resp.usage.total_tokens
                }

                # Update History
                history_parts = [
                    part for part in parts if part.get("type") == "input_text"
                ]
                if not history_parts:
                    history_parts = [{"type": "input_text", "text": "(이전 턴에 이미지 첨부됨)"}]
                session.history.append({"role": "user", "content": history_parts})
                session.history.append({"role": "assistant", "content": reply})

            await inter.followup.send(f"**{inter.user.mention}**: {내용}")
            await self.send_chunked_followup(inter, reply)

        except Exception as exc:
            # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
            print(f"❌ [hyacine_chat] '{model}' 호출 실패 (channel={inter.channel_id})")
            traceback.print_exc()
            refund_note = " (포인트 환불됨)" if await refund_points() else ""
            if isinstance(exc, openai.RateLimitError):
                error_message = (
                    f"⏳ 지금은 요청이 몰려 있어요. 잠시 후 다시 시도해 주세요.{refund_note}"
                )
            else:
                error_message = f"❌ 응답 생성에 실패했어요. 잠시 후 다시 시도해 주세요.{refund_note}"
            try:
                if inter.response.is_done():
                    await inter.followup.send(error_message)
                else:
                    await inter.response.send_message(error_message)
            except Exception:
                print(f"❌ [hyacine_chat] 오류 메시지 전송 실패 (channel={inter.channel_id})")
                traceback.print_exc()
        finally:
            await release_usage()

    async def cog_app_command_error(
        self,
        inter: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        # 쿨다운은 콜백 진입 전에 걸리므로 포인트는 아직 차감되지 않았다.
        if isinstance(error, app_commands.CommandOnCooldown):
            await inter.response.send_message(
                f"⏳ 조금만 쉬어 가요~ {error.retry_after:.0f}초 뒤에 다시 불러 주세요.",
                ephemeral=True,
            )
            return
        raise error

    @app_commands.command(name="기본대화", description="AI와 빠르게 대화합니다. (200 P)")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    @app_commands.checks.cooldown(1, AI_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def _light_talk(
        self,
        inter: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        await self._run_talk(
            inter, 내용, 이미지, LIGHT_MODEL, "none", LIGHT_COST, "light", LIMIT_LIGHT
        )

    @app_commands.command(name="고급대화", description="AI와 깊이 대화합니다. (2,000 P)")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    @app_commands.checks.cooldown(1, AI_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def _deep_talk(
        self,
        inter: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        await self._run_talk(
            inter, 내용, 이미지, DEEP_MODEL, "medium", DEEP_COST, "deep", LIMIT_DEEP
        )

    @app_commands.command(name="상태", description="이 채널의 현재 상태 확인")
    async def _status(self, inter: discord.Interaction):
        session = self.get_session(inter.channel_id)
        attendance_cog = self.bot.get_cog("AttendanceCog") if self.bot else None
        msg = (
            "- **대화 명령**\n"
            f"- `/기본대화`: `{LIGHT_MODEL}` (Reasoning: `none`, {LIGHT_COST:,} P)\n"
            f"- `/고급대화`: `{DEEP_MODEL}` (Reasoning: `medium`, {DEEP_COST:,} P)\n"
        )
        if attendance_cog:
            light = max(0, LIMIT_LIGHT - await attendance_cog.get_ai_usage(inter.user.id, "light"))
            deep = max(0, LIMIT_DEEP - await attendance_cog.get_ai_usage(inter.user.id, "deep"))
            image = max(0, LIMIT_IMAGE - await attendance_cog.get_ai_usage(inter.user.id, "image"))
            msg += (
                " \n- **오늘 남은 횟수 (KST)**\n"
                f"- `/기본대화`: {light}/{LIMIT_LIGHT}회\n"
                f"- `/고급대화`: {deep}/{LIMIT_DEEP}회\n"
                f"- `/이미지`: {image}/{LIMIT_IMAGE}회\n"
            )
        else:
            msg += " \n- **오늘 남은 횟수 (KST)**: 확인할 수 없어요.\n"
        if session.last_usage:
            msg += (
                " \n"
                f"- **직전 사용 모델**: `{session.last_usage.get('model')}`\n"
                f"직전 토큰: {session.last_usage.get('total_tokens')}\n"
            )
        await inter.response.send_message(msg)

    @app_commands.command(name="인사", description="가벼운 인삿말")
    async def _hello(self, interaction: discord.Interaction):
        session = self.get_session(interaction.channel_id)
        await interaction.response.send_message(
            f"{interaction.user.mention}, {session.greeting}"
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(HyacineChatCog(bot))
