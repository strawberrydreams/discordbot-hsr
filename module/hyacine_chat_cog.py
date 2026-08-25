import asyncio
import logging
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
MAX_LIGHT_RESPONSE_TOKENS = 2_000
MAX_DEEP_RESPONSE_TOKENS = 16_000
logger = logging.getLogger(__name__)


def _is_insufficient_quota(exc: Exception) -> bool:
    if not isinstance(exc, openai.RateLimitError):
        return False
    error_codes = {getattr(exc, "code", None), getattr(exc, "type", None)}
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            error_codes.update((error.get("code"), error.get("type")))
    return bool(
        {"credit_balance_exhausted", "insufficient_quota"} & error_codes
    )


def canonicalize_persona(document: object, *, strict: bool = False) -> dict:
    if not isinstance(document, dict):
        if strict:
            raise ValueError("persona.json 최상단은 객체여야 합니다.")
        document = {}
    accepted = {}
    for field_name, field_value in document.items():
        field_limit = PERSONA_FIELD_LIMITS.get(field_name)
        valid = isinstance(field_value, str) and bool(field_value) and (
            field_limit is None or len(field_value) <= field_limit
        )
        if not valid:
            message = "persona.json 값은 비어 있지 않은 문자열이어야 합니다."
            if (
                field_limit is not None
                and isinstance(field_value, str)
                and len(field_value) > field_limit
            ):
                message = (
                    f"persona.json의 {field_name}는 "
                    f"{field_limit:,}자 이하여야 합니다."
                )
            if strict:
                raise ValueError(message)
            print(f"⚠️ {message} 기본값을 씁니다.")
            continue
        accepted[field_name] = field_value
    return {**DEFAULT_PERSONA, **accepted}


def load_persona() -> dict:
    """settings/persona.json → persona.example.json → 코드 기본값 순으로 읽는다."""
    document = load_settings_json("persona.json", "persona.example.json", default={})
    return canonicalize_persona(document)


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

        self.system_prompt = load_persona()["system_prompt"]

        # 채널 ID -> ChannelSession (채널별 대화 기억 슬롯)
        self.sessions: OrderedDict[int, ChannelSession] = OrderedDict()

    def get_or_create_session(self, channel_id: int) -> ChannelSession:
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

    def _trim_history(self, session: ChannelSession):
        # system 제외하고 최근 10개(5턴)까지 유지
        conversation_messages = [
            message for message in session.history if message["role"] != "system"
        ]
        system_messages = [
            message for message in session.history if message["role"] == "system"
        ]
        retained_messages = conversation_messages[-10:]
        session.history.clear()
        for message in system_messages + retained_messages:
            session.history.append(message)

    def _build_user_content(
        self,
        text: Optional[str],
        image_attachment: Optional[discord.Attachment],
    ) -> List[Dict[str, Any]]:
        parts: List[Dict[str, Any]] = []
        if text and text.strip():
            parts.append({"type": "input_text", "text": text.strip()})
        if image_attachment and (image_attachment.content_type or "").startswith(
            "image/"
        ):
            parts.append(
                {"type": "input_image", "image_url": image_attachment.url}
            )
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

    async def _send_chunked_followup(
        self, interaction: discord.Interaction, text: str
    ):
        parts = self._split_for_discord(text)
        for part in parts:
            if not part.strip():
                continue
            await interaction.followup.send(part)

    async def _run_chat(
        self,
        interaction: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment],
        model: str,
        reasoning_effort: str,
        usage_category: str,
        daily_limit: int,
    ):
        session = self.get_or_create_session(interaction.channel_id)
        usage_cog = self.bot.get_cog("UsageCog") if self.bot else None
        api_started = False

        if not usage_cog:
            await interaction.response.send_message(
                "❌ 사용량 모듈 오류.", ephemeral=True
            )
            return

        reservation = await usage_cog.reserve_ai_usage(
            interaction.user.id, usage_category, daily_limit
        )
        if reservation is None:
            await interaction.response.send_message(
                "오늘 사용 횟수를 모두 사용했어요.", ephemeral=True
            )
            return
        usage_date, _ = reservation

        async def release_usage():
            if not api_started:
                try:
                    await usage_cog.release_ai_usage(
                        interaction.user.id, usage_date, usage_category
                    )
                except Exception:
                    print(
                        "❌ [hyacine_chat] 일일 사용량 반환 실패 "
                        f"(user={interaction.user.id}, command={usage_category})"
                    )
                    traceback.print_exc()

        try:
            await interaction.response.defer()

            parts = self._build_user_content(내용, 이미지)

            # defer()는 락 밖에 둬서 대기 중에도 인터랙션이 만료되지 않게 한다.
            async with session.lock:
                self._trim_history(session)

                # 최근 10개 메시지 (5턴) 사용
                recent_turns = [
                    message
                    for message in session.history
                    if message["role"] != "system"
                ][-10:]

                max_tokens = (
                    MAX_DEEP_RESPONSE_TOKENS
                    if model == DEEP_MODEL
                    else MAX_LIGHT_RESPONSE_TOKENS
                )

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
                response = await self.client.responses.create(**kwargs)

                reply = (response.output_text or "").strip()

                if not reply.strip():
                    await interaction.followup.send(
                        "⚠️ 모델 응답이 비어 있어서 디스코드로 전송하지 않았어요. 콘솔 로그를 확인해 주세요."
                    )
                    return

                session.last_usage = {
                    "model": response.model,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens
                }

                history_parts = [
                    part for part in parts if part.get("type") == "input_text"
                ]
                if not history_parts:
                    history_parts = [{"type": "input_text", "text": "(이전 턴에 이미지 첨부됨)"}]

                await self._send_chunked_followup(
                    interaction, f"**{interaction.user.mention}**: {내용}"
                )
                await self._send_chunked_followup(interaction, reply)

                # 사용자가 실제로 받은 턴만 다음 provider 입력에 포함한다.
                session.history.append({"role": "user", "content": history_parts})
                session.history.append({"role": "assistant", "content": reply})

        except Exception as exc:
            quota_exhausted = _is_insufficient_quota(exc)
            if quota_exhausted:
                logger.warning(
                    "[hyacine_chat] OpenAI API credit exhausted "
                    "(model=%s, channel=%s)",
                    model,
                    interaction.channel_id,
                )
                error_message = (
                    "💳 OpenAI API 크레딧이 소진되어 텍스트 대화를 사용할 수 "
                    "없어요. 운영자가 크레딧을 충전한 뒤 다시 시도해 주세요."
                )
            else:
                # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
                print(
                    f"❌ [hyacine_chat] '{model}' 호출 실패 "
                    f"(channel={interaction.channel_id})"
                )
                traceback.print_exc()
            if isinstance(exc, openai.RateLimitError) and not quota_exhausted:
                error_message = "⏳ 지금은 요청이 몰려 있어요. 잠시 후 다시 시도해 주세요."
            elif not quota_exhausted:
                error_message = "❌ 응답 생성에 실패했어요. 잠시 후 다시 시도해 주세요."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(error_message)
                else:
                    await interaction.response.send_message(error_message)
            except Exception:
                print(
                    "❌ [hyacine_chat] 오류 메시지 전송 실패 "
                    f"(channel={interaction.channel_id})"
                )
                traceback.print_exc()
        finally:
            await release_usage()

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        # 쿨다운은 콜백 진입 전에 걸리므로 일일 한도는 아직 예약되지 않았다.
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ 조금만 쉬어 가요~ {error.retry_after:.0f}초 뒤에 다시 불러 주세요.",
                ephemeral=True,
            )
            return
        raise error

    @app_commands.command(name="기본대화", description="AI와 빠르게 대화합니다.")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    @app_commands.checks.cooldown(
        1, AI_COOLDOWN_SECONDS, key=lambda interaction: interaction.user.id
    )
    async def _light_chat(
        self,
        interaction: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        await self._run_chat(
            interaction, 내용, 이미지, LIGHT_MODEL, "none", "light", LIMIT_LIGHT
        )

    @app_commands.command(name="고급대화", description="AI와 깊이 대화합니다.")
    @app_commands.describe(내용="메시지", 이미지="(선택) 이미지")
    @app_commands.checks.cooldown(
        1, AI_COOLDOWN_SECONDS, key=lambda interaction: interaction.user.id
    )
    async def _deep_chat(
        self,
        interaction: discord.Interaction,
        내용: str,
        이미지: Optional[discord.Attachment] = None,
    ):
        await self._run_chat(
            interaction, 내용, 이미지, DEEP_MODEL, "medium", "deep", LIMIT_DEEP
        )

    @app_commands.command(name="상태", description="이 채널의 현재 상태 확인")
    async def _status(self, interaction: discord.Interaction):
        session = self.get_or_create_session(interaction.channel_id)
        usage_cog = self.bot.get_cog("UsageCog") if self.bot else None
        status_message = (
            "- **대화 명령**\n"
            f"- `/기본대화`: `{LIGHT_MODEL}` (Reasoning: `none`)\n"
            f"- `/고급대화`: `{DEEP_MODEL}` (Reasoning: `medium`)\n"
        )
        if usage_cog:
            light = max(
                0,
                LIMIT_LIGHT
                - await usage_cog.get_ai_usage(interaction.user.id, "light"),
            )
            deep = max(
                0,
                LIMIT_DEEP
                - await usage_cog.get_ai_usage(interaction.user.id, "deep"),
            )
            image = max(
                0,
                LIMIT_IMAGE
                - await usage_cog.get_ai_usage(interaction.user.id, "image"),
            )
            status_message += (
                " \n- **오늘 남은 횟수 (KST · 사용자별 · 봇 인스턴스 전체)**\n"
                f"- `/기본대화`: {light}/{LIMIT_LIGHT}회\n"
                f"- `/고급대화`: {deep}/{LIMIT_DEEP}회\n"
                f"- `/이미지`: {image}/{LIMIT_IMAGE}회\n"
            )
        else:
            status_message += " \n- **오늘 남은 횟수 (KST)**: 확인할 수 없어요.\n"
        if session.last_usage:
            status_message += (
                " \n"
                f"- **직전 사용 모델**: `{session.last_usage.get('model')}`\n"
                f"직전 토큰: {session.last_usage.get('total_tokens')}\n"
            )
        await interaction.response.send_message(status_message, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(HyacineChatCog(bot))
