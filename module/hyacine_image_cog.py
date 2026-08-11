from __future__ import annotations
import asyncio
import os
import pathlib
import time
import traceback
import uuid
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from module.config import AI_COOLDOWN_SECONDS, DATA_DIR, GOOGLE_API_KEY, IMAGE_MODEL, LIMIT_IMAGE

# 평균 출석 수입(17,500 P/일) 기준 이틀에 1회.
IMAGE_COST = 30_000

# 임베드 description은 4,096자 한계가 있다. 넘으면 전송이 400으로 실패하고 환불도 못 한다.
MAX_PROMPT_DISPLAY = 1_000
TEMP_IMAGE_TTL_SECONDS = 300

class HyacineImageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = genai.Client(api_key=GOOGLE_API_KEY)
        
        # CWD 상대 경로는 Docker 볼륨 밖이라 재시작 시 고아 파일이 남는다.
        self.temp_dir = DATA_DIR / "temp_images"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_images()

    def _sweep_stale_images(self):
        """삭제 태스크는 재시작에서 살아남지 못하므로 시작 시 한 번 청소한다."""
        cutoff = time.time() - TEMP_IMAGE_TTL_SECONDS
        for path in pathlib.Path(self.temp_dir).glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                print(f"⚠️ Failed to sweep {path}: {exc}")

    async def _delete_file_after_delay(self, file_path: str, delay: int):
        """Waits for a delay (seconds) and then deletes the file."""
        await asyncio.sleep(delay)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"🗑️ Deleted temp image: {file_path}")
        except Exception as e:
            print(f"⚠️ Failed to delete {file_path}: {e}")

    async def cog_app_command_error(
        self,
        inter: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        # 쿨다운은 콜백 진입 전에 걸리므로 포인트는 아직 차감되지 않았다.
        if isinstance(error, app_commands.CommandOnCooldown):
            await inter.response.send_message(
                f"⏳ 붓을 말리는 중이에요~ {error.retry_after:.0f}초 뒤에 다시 불러 주세요.",
                ephemeral=True,
            )
            return
        raise error

    @app_commands.command(name="이미지", description="AI에게 그림을 그려달라고 요청합니다. (30,000 P)")
    @app_commands.describe(프롬프트="그려줘! 라고 할 내용")
    @app_commands.checks.cooldown(1, AI_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def _image(self, inter: discord.Interaction, 프롬프트: str):
        # 0. Check Points
        attendance_cog = self.bot.get_cog("AttendanceCog")
        if not attendance_cog:
            await inter.response.send_message("❌ 출석체크 모듈이 로드되지 않아 포인트를 확인할 수 없어요.", ephemeral=True)
            return

        cost = IMAGE_COST
        reservation = await attendance_cog.reserve_ai_usage(
            inter.user.id, "image", LIMIT_IMAGE
        )
        if reservation is None:
            await inter.response.send_message(
                "오늘 사용 횟수를 모두 사용했어요.", ephemeral=True
            )
            return
        usage_date, _ = reservation

        charged = False
        refunded = False
        refund_attempted = False
        refund_failed = False
        generation_completed = False
        filepath = None
        uploaded = False
        file = None
        api_started = False

        async def release_usage():
            if not api_started:
                try:
                    await attendance_cog.release_ai_usage(
                        inter.user.id, usage_date, "image"
                    )
                except Exception:
                    print(
                        "❌ [hyacine_image] 일일 사용량 반환 실패 "
                        f"(user={inter.user.id}, command=image)"
                    )
                    traceback.print_exc()

        async def refund_points() -> bool:
            nonlocal refund_attempted, refund_failed, refunded
            if not charged or refund_attempted or generation_completed:
                return False
            refund_attempted = True
            try:
                await attendance_cog.add_points(
                    inter.guild_id, inter.user.id, cost, "image_refund"
                )
            except Exception:
                refund_failed = True
                print(
                    "❌ [hyacine_image] 포인트 자동 환불 실패 "
                    f"(user={inter.user.id}, amount={cost})"
                )
                traceback.print_exc()
                return False
            refunded = True
            return True

        try:
            if not await attendance_cog.deduct_points(
                inter.guild_id, inter.user.id, cost, "image"
            ):
                current = await attendance_cog.get_points(inter.guild_id, inter.user.id)
                await inter.response.send_message(f"❌ 포인트가 부족해요! (필요: {cost:,} P / 보유: {current:,} P)", ephemeral=True)
                return
            charged = True

            await inter.response.defer()

            # 1. Request Image Generation
            loop = asyncio.get_running_loop()

            # Gemini 이미지 모델(Nano Banana)은 generate_images가 아닌 generate_content를 사용
            # Run blocking SDK call in executor
            # ponytail: API 호출이 시작되면 실패해도 일일 한도를 소비한다.
            # provider 사용량 대조가 필요해질 때 request ID 원장을 추가한다.
            api_started = True
            response = await loop.run_in_executor(
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
                await refund_points()
                print(f"⚠️ Image generation blocked/failed. Response: {response}")
                message = "❌ 이미지를 생성하지 못했어요."
                if refunded:
                    message += " 포인트는 환불해 드렸습니다."
                elif refund_failed:
                    message += (
                        " 자동 환불에 실패했습니다. "
                        "관리자에게 수동 정산을 요청해 주세요."
                    )
                message += "\n(구글의 안전 필터 또는 인물 생성 정책에 의해 차단되었을 가능성이 높습니다.)"
                await inter.followup.send(message)
                return

            generation_completed = True
            
            # 2. Save to a local file
            filename = f"{uuid.uuid4()}.png"
            filepath = os.path.join(self.temp_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(image_data)
                
            # 3. Upload to Discord
            file = discord.File(filepath, filename=filename)
            display_prompt = (
                프롬프트
                if len(프롬프트) <= MAX_PROMPT_DISPLAY
                else 프롬프트[:MAX_PROMPT_DISPLAY] + "…"
            )
            embed = discord.Embed(
                title="🎨 히아킨의 그림 선물",
                description=f"**요청**: {display_prompt}",
                color=0x9b59b6 # Purple-ish
            )
            embed.set_image(url=f"attachment://{filename}")
            embed.set_footer(text=f"Model: {IMAGE_MODEL} | 비용: {cost:,} P | 5분 후 서버에서 삭제됨")
            
            await inter.followup.send(embed=embed, file=file)
            uploaded = True
            
            # 4. Schedule deletion
            self.bot.loop.create_task(
                self._delete_file_after_delay(filepath, TEMP_IMAGE_TTL_SECONDS)
            )

        except Exception:
            await refund_points()
            # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
            print(f"❌ [hyacine_image] 이미지 생성 실패 (user={inter.user.id})")
            traceback.print_exc()
            if generation_completed:
                message = "❌ 이미지는 생성되었지만 Discord 전송에 실패했습니다."
            else:
                message = "❌ 이미지 생성 중 오류가 발생했어요."
                if refunded:
                    message += " 포인트는 환불해 드렸습니다."
                elif refund_failed:
                    message += (
                        " 자동 환불에 실패했습니다. "
                        "관리자에게 수동 정산을 요청해 주세요."
                    )
            try:
                if inter.response.is_done():
                    await inter.followup.send(message)
                else:
                    await inter.response.send_message(message, ephemeral=True)
            except Exception:
                print(f"⚠️ [hyacine_image] 오류 메시지 전송 실패 (user={inter.user.id})")
        finally:
            await release_usage()
            if file is not None:
                file.close()
            if filepath and not uploaded:
                try:
                    os.remove(filepath)
                except FileNotFoundError:
                    pass

async def setup(bot: commands.Bot):
    await bot.add_cog(HyacineImageCog(bot))
