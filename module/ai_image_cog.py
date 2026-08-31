from __future__ import annotations

import asyncio
import os
import time
import traceback
import uuid

import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import errors as genai_errors

from module.config import (
    AI_COOLDOWN_SECONDS,
    DATA_DIR,
    GOOGLE_API_KEY,
    IMAGE_MODEL,
    LIMIT_IMAGE,
    ensure_private_directory,
)
from module.usage_cog import UsageCog

# 임베드 description은 4,096자 한계가 있다. 넘으면 전송이 400으로 실패한다.
MAX_DISPLAYED_PROMPT_CHARS = 1_000
TEMP_IMAGE_TTL_SECONDS = 300
GEMINI_QUOTA_MESSAGE = (
    "💳 Google Gemini API의 요청 할당량 또는 결제 한도에 도달해 이미지를 생성할 수 없어요. "
    "잠시 후 다시 시도하고, 계속되면 운영자가 Google AI Studio의 사용량·요금제·결제 상태를 확인해 주세요."
)


def _is_gemini_quota_error(error: Exception) -> bool:
    return isinstance(error, genai_errors.APIError) and (
        error.code == 429 or error.status == "RESOURCE_EXHAUSTED"
    )


class AIImageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = genai.Client(api_key=GOOGLE_API_KEY)
        
        # CWD 상대 경로는 Docker 볼륨 밖이라 재시작 시 고아 파일이 남는다.
        self.temporary_image_directory = DATA_DIR / "temp_images"
        ensure_private_directory(self.temporary_image_directory)
        self._sweep_stale_images()

    def _sweep_stale_images(self):
        """삭제 태스크는 재시작에서 살아남지 못하므로 시작 시 한 번 청소한다."""
        cutoff = time.time() - TEMP_IMAGE_TTL_SECONDS
        for path in self.temporary_image_directory.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                print(f"⚠️ Failed to sweep {path}: {exc}")

    async def _delete_image_after_delay(
        self, image_path: str, delay_seconds: int
    ):
        """Waits for a delay (seconds) and then deletes the file."""
        await asyncio.sleep(delay_seconds)
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
                print(f"🗑️ Deleted temp image: {image_path}")
        except Exception as exc:
            print(f"⚠️ Failed to delete {image_path}: {exc}")

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        # 쿨다운은 콜백 진입 전에 걸리므로 일일 한도는 아직 예약되지 않았다.
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ 붓을 말리는 중이에요~ {error.retry_after:.0f}초 뒤에 다시 불러 주세요.",
                ephemeral=True,
            )
            return
        raise error

    @app_commands.command(name="이미지", description="AI에게 그림을 그려달라고 요청합니다.")
    @app_commands.describe(프롬프트="그려줘! 라고 할 내용")
    @app_commands.checks.cooldown(
        1, AI_COOLDOWN_SECONDS, key=lambda interaction: interaction.user.id
    )
    async def _generate_image(
        self, interaction: discord.Interaction, 프롬프트: str
    ):
        usage_cog = self.bot.get_cog(UsageCog.__name__)
        if not usage_cog:
            await interaction.response.send_message(
                "❌ 사용량 모듈이 로드되지 않아 일일 한도를 확인할 수 없어요.", ephemeral=True
            )
            return

        reservation = await usage_cog.reserve_ai_usage(
            interaction.user.id, "image", LIMIT_IMAGE
        )
        if reservation is None:
            await interaction.response.send_message(
                "오늘 사용 횟수를 모두 사용했어요.", ephemeral=True
            )
            return
        usage_date, _ = reservation

        image_generated = False
        image_path = None
        image_uploaded = False
        discord_file = None
        api_started = False

        async def release_usage():
            if not api_started:
                try:
                    await usage_cog.release_ai_usage(
                        interaction.user.id, usage_date, "image"
                    )
                except Exception:
                    print(
                        "❌ [ai_image] 일일 사용량 반환 실패 "
                        f"(user={interaction.user.id}, command=image)"
                    )
                    traceback.print_exc()

        try:
            await interaction.response.defer()

            # 1. Request Image Generation
            event_loop = asyncio.get_running_loop()

            # Gemini 이미지 모델(Nano Banana)은 generate_images가 아닌 generate_content를 사용
            # Run blocking SDK call in executor
            # ponytail: API 호출이 시작되면 실패해도 일일 한도를 소비한다.
            # provider 사용량 대조가 필요해질 때 request ID 원장을 추가한다.
            api_started = True
            response = await event_loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=IMAGE_MODEL,
                    contents=[프롬프트],
                )
            )

            # 응답 파트에서 이미지(inline_data)를 추출
            image_data = None
            for part in (response.parts or []):
                if part.inline_data is not None and part.inline_data.data:
                    image_data = part.inline_data.data
                    break

            if image_data is None:
                finish_reasons = [
                    str(getattr(candidate, "finish_reason", "unknown"))
                    for candidate in list(
                        getattr(response, "candidates", None) or ()
                    )[:3]
                ]
                print(
                    "⚠️ [ai_image] 이미지 없는 provider 응답 "
                    f"(model={IMAGE_MODEL}, finish={','.join(finish_reasons) or 'unknown'})"
                )
                await interaction.followup.send(
                    "❌ 이미지를 생성하지 못했어요."
                    "\n(구글의 안전 필터 또는 인물 생성 정책에 의해 차단되었을 가능성이 높습니다.)"
                )
                return

            image_generated = True
            
            # 2. Save to a local file
            image_filename = f"{uuid.uuid4()}.png"
            image_path = self.temporary_image_directory / image_filename
            descriptor = os.open(
                image_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as image_file:
                image_file.write(image_data)
                
            # 3. Upload to Discord
            discord_file = discord.File(image_path, filename=image_filename)
            display_prompt = (
                프롬프트
                if len(프롬프트) <= MAX_DISPLAYED_PROMPT_CHARS
                else 프롬프트[:MAX_DISPLAYED_PROMPT_CHARS] + "…"
            )
            embed = discord.Embed(
                title="🎨 히아킨의 그림 선물",
                description=f"**요청**: {display_prompt}",
                color=0x9b59b6 # Purple-ish
            )
            embed.set_image(url=f"attachment://{image_filename}")
            embed.set_footer(text=f"Model: {IMAGE_MODEL} | 5분 후 서버에서 삭제됨")
            
            await interaction.followup.send(embed=embed, file=discord_file)
            image_uploaded = True
            
            # 4. Schedule deletion
            self.bot.loop.create_task(
                self._delete_image_after_delay(
                    image_path, TEMP_IMAGE_TTL_SECONDS
                )
            )

        except Exception as error:
            quota_error = _is_gemini_quota_error(error)
            if quota_error:
                # provider가 생성 요청을 수락하지 않았으므로 사용자 일일 예약을 반환한다.
                api_started = False
            print(
                "❌ [ai_image] 이미지 생성 실패 "
                f"(user={interaction.user.id})"
            )
            if quota_error:
                print(
                    "⚠️ [ai_image] provider quota/billing 제한 "
                    f"(provider=google, status={getattr(error, 'code', 'RESOURCE_EXHAUSTED')})"
                )
            else:
                # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
                traceback.print_exc()
            if image_generated:
                message = "❌ 이미지는 생성되었지만 Discord 전송에 실패했습니다."
            elif quota_error:
                message = GEMINI_QUOTA_MESSAGE
            else:
                message = "❌ 이미지 생성 중 오류가 발생했어요."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message)
                else:
                    await interaction.response.send_message(
                        message, ephemeral=True
                    )
            except Exception:
                print(
                    "⚠️ [ai_image] 오류 메시지 전송 실패 "
                    f"(user={interaction.user.id})"
                )
        finally:
            await release_usage()
            if discord_file is not None:
                discord_file.close()
            if image_path and not image_uploaded:
                try:
                    os.remove(image_path)
                except FileNotFoundError:
                    pass

async def setup(bot: commands.Bot):
    await bot.add_cog(AIImageCog(bot))
