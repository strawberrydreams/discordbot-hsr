import asyncio
import traceback
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional
import discord
import openai
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


DEFAULT_PERSONA = {
    "system_prompt": "당신은 놀빛 정원의 따뜻한 의사, 하늘의 백성 히아킨입니다. 사용자를 '회색둥이 씨'라 부르며, 한국어로 정확하고 다정하게 답하세요.",
    "greeting": "안녕하세요~ 정원에서 기다리고 있었답니다🌼",
}
SYSTEM_PROMPT_MAX_CHARS = 16_000
# Discord mention의 최장 형태(<@! + 20자리 ID + >)와 ", "를 더해도 2,000자다.
GREETING_MAX_CHARS = 1_974
PERSONA_FIELD_LIMITS = {
    "system_prompt": SYSTEM_PROMPT_MAX_CHARS,
    "greeting": GREETING_MAX_CHARS,
}


def canonicalize_persona(data: object, *, strict: bool = False) -> dict:
    if not isinstance(data, dict):
        if strict:
            raise ValueError("persona.json 최상단은 객체여야 합니다.")
        data = {}
    accepted = {}
    for key, value in data.items():
        limit = PERSONA_FIELD_LIMITS.get(key)
        valid = isinstance(value, str) and bool(value) and (
            limit is None or len(value) <= limit
        )
        if not valid:
            message = "persona.json 값은 비어 있지 않은 문자열이어야 합니다."
            if limit is not None and isinstance(value, str) and len(value) > limit:
                message = f"persona.json의 {key}는 {limit:,}자 이하여야 합니다."
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 기본값을 씁니다.")
            continue
        accepted[key] = value
    return {**DEFAULT_PERSONA, **accepted}


def load_persona() -> dict:
    """settings/persona.json → persona.example.json → 코드 기본값 순으로 읽는다."""
    data = load_settings_json("persona.json", "persona.example.json", default={})
    return canonicalize_persona(data)


class ChannelSession:
    """채널 하나의 대화 상태 (히스토리, 사용량).
    채널별로 분리하여 다른 채널의 대화 내용이 섞이지 않도록 한다."""

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
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

        self.MAX_ASSISTANT_LIGHT = 2_000
        self.MAX_ASSISTANT_DEEP = 16_000

        self.system_prompt = load_persona()["system_prompt"]

        # 채널 ID -> ChannelSession (채널별 대화 기억 슬롯)
        self.sessions: OrderedDict[int, ChannelSession] = OrderedDict()

    def get_session(self, channel_id: int) -> ChannelSession:
        """채널의 세션을 가져오거나 새로 만든다."""
        session = self.sessions.pop(channel_id, None)
        if session is None:
            self.system_prompt = load_persona()["system_prompt"]
            session = ChannelSession(self.system_prompt)
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
        if limit <= 0:
            raise ValueError("limit은 양수여야 합니다.")
        if len(text) <= limit:
            return [text]

        # 열린 code fence를 닫고 다음 메시지에서 다시 여는 최대 8자를 남긴다.
        reserve = 8 if "```" in text and limit > 8 else 0
        payload_limit = limit - reserve
        raw_chunks = []
        start = 0
        while start < len(text):
            end = min(start + payload_limit, len(text))
            if end < len(text):
                original_end = end
                # ``` 한가운데를 자르면 fence 상태를 잘못 계산한다.
                while end > start and text[end - 1] == "`" and text[end] == "`":
                    end -= 1
                if end == start:
                    end = original_end
            raw_chunks.append(text[start:end])
            start = end

        if reserve == 0:
            return raw_chunks

        chunks = []
        fence_open = False
        for raw in raw_chunks:
            prefix = "```\n" if fence_open else ""
            if raw.count("```") % 2:
                fence_open = not fence_open
            suffix = "\n```" if fence_open else ""
            chunks.append(prefix + raw + suffix)
        return chunks

    async def send_chunked_followup(self, inter: discord.Interaction, text: str):
        parts = self._split_for_discord(text)
        for p in parts:
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
        usage_command: str,
        daily_limit: int,
    ):
        session = self.get_session(inter.channel_id)
        usage_cog = self.bot.get_cog("UsageCog") if self.bot else None
        api_started = False

        if not usage_cog:
            await inter.response.send_message("❌ 사용량 모듈 오류.", ephemeral=True)
            return

        reservation = await usage_cog.reserve_ai_usage(
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
                    await usage_cog.release_ai_usage(
                        inter.user.id, usage_date, usage_command
                    )
                except Exception:
                    print(
                        "❌ [hyacine_chat] 일일 사용량 반환 실패 "
                        f"(user={inter.user.id}, command={usage_command})"
                    )
                    traceback.print_exc()

        try:
            await inter.response.defer()

            parts = self.build_user_parts(내용, 이미지)

            # defer()는 락 밖에 둬서 대기 중에도 인터랙션이 만료되지 않게 한다.
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
                    await inter.followup.send(
                        "⚠️ 모델 응답이 비어 있어서 디스코드로 전송하지 않았어요. 콘솔 로그를 확인해 주세요."
                    )
                    return

                session.last_usage = {
                    "model": resp.model,
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                    "total_tokens": resp.usage.total_tokens
                }

                history_parts = [
                    part for part in parts if part.get("type") == "input_text"
                ]
                if not history_parts:
                    history_parts = [{"type": "input_text", "text": "(이전 턴에 이미지 첨부됨)"}]

                await self.send_chunked_followup(
                    inter, f"**{inter.user.mention}**: {내용}"
                )
                await self.send_chunked_followup(inter, reply)

                # 사용자가 실제로 받은 턴만 다음 provider 입력에 포함한다.
                session.history.append({"role": "user", "content": history_parts})
                session.history.append({"role": "assistant", "content": reply})

        except Exception as exc:
            # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
            print(f"❌ [hyacine_chat] '{model}' 호출 실패 (channel={inter.channel_id})")
            traceback.print_exc()
            if isinstance(exc, openai.RateLimitError):
                error_message = "⏳ 지금은 요청이 몰려 있어요. 잠시 후 다시 시도해 주세요."
            else:
                error_message = "❌ 응답 생성에 실패했어요. 잠시 후 다시 시도해 주세요."
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
        # 쿨다운은 콜백 진입 전에 걸리므로 일일 한도는 아직 예약되지 않았다.
        if isinstance(error, app_commands.CommandOnCooldown):
            await inter.response.send_message(
                f"⏳ 조금만 쉬어 가요~ {error.retry_after:.0f}초 뒤에 다시 불러 주세요.",
                ephemeral=True,
            )
            return
        raise error

    @app_commands.command(name="기본대화", description="AI와 빠르게 대화합니다.")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    @app_commands.checks.cooldown(1, AI_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def _light_talk(
        self,
        inter: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        await self._run_talk(
            inter, 내용, 이미지, LIGHT_MODEL, "none", "light", LIMIT_LIGHT
        )

    @app_commands.command(name="고급대화", description="AI와 깊이 대화합니다.")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    @app_commands.checks.cooldown(1, AI_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def _deep_talk(
        self,
        inter: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        await self._run_talk(
            inter, 내용, 이미지, DEEP_MODEL, "medium", "deep", LIMIT_DEEP
        )

    @app_commands.command(name="상태", description="이 채널의 현재 상태 확인")
    async def _status(self, inter: discord.Interaction):
        session = self.get_session(inter.channel_id)
        usage_cog = self.bot.get_cog("UsageCog") if self.bot else None
        msg = (
            "- **대화 명령**\n"
            f"- `/기본대화`: `{LIGHT_MODEL}` (Reasoning: `none`)\n"
            f"- `/고급대화`: `{DEEP_MODEL}` (Reasoning: `medium`)\n"
        )
        if usage_cog:
            light = max(0, LIMIT_LIGHT - await usage_cog.get_ai_usage(inter.user.id, "light"))
            deep = max(0, LIMIT_DEEP - await usage_cog.get_ai_usage(inter.user.id, "deep"))
            image = max(0, LIMIT_IMAGE - await usage_cog.get_ai_usage(inter.user.id, "image"))
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

async def setup(bot: commands.Bot):
    await bot.add_cog(HyacineChatCog(bot))
