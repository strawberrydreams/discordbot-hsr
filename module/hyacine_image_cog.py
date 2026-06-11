from __future__ import annotations
import asyncio
import os
import traceback
import uuid
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from module.config import GOOGLE_API_KEY

# Model Alias
# Imagen 계열은 2026-06-24 서비스 종료 예정이라 Gemini 이미지 모델로 교체
IMAGE_MODEL = "gemini-3.1-flash-image" # Nano Banana 2 (Gemini 3.1 Flash Image)

class HyacineImageCog(commands.Cog):
    def __init__(self, bot: commands.Bot, nickname: str = "회색"):
        self.bot = bot
        self.nickname = nickname
        self.client = genai.Client(api_key=GOOGLE_API_KEY)
        
        # Ensure temp directory exists
        self.temp_dir = "temp_images"
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

    async def _delete_file_after_delay(self, file_path: str, delay: int):
        """Waits for a delay (seconds) and then deletes the file."""
        await asyncio.sleep(delay)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"🗑️ Deleted temp image: {file_path}")
        except Exception as e:
            print(f"⚠️ Failed to delete {file_path}: {e}")

    @app_commands.command(name="이미지", description="Nano Banana 2에게 그림을 그려달라고 요청합니다. (50,000 P)")
    @app_commands.describe(프롬프트="그려줘! 라고 할 내용")
    async def _image(self, inter: discord.Interaction, 프롬프트: str):
        # 0. Check Points
        attendance_cog = self.bot.get_cog("AttendanceCog")
        if not attendance_cog:
            await inter.response.send_message("❌ 출석체크 모듈이 로드되지 않아 포인트를 확인할 수 없어요.", ephemeral=True)
            return

        cost = 50000
        if not attendance_cog.deduct_points(inter.user.id, cost):
            current = attendance_cog.get_points(inter.user.id)
            await inter.response.send_message(f"❌ 포인트가 부족해요! (필요: {cost:,} P / 보유: {current:,} P)", ephemeral=True)
            return

        await inter.response.defer()

        try:
            # 1. Request Image Generation
            loop = asyncio.get_running_loop()

            # Gemini 이미지 모델(Nano Banana)은 generate_images가 아닌 generate_content를 사용
            # Run blocking SDK call in executor
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
                # Refund on failure
                attendance_cog.add_points(inter.user.id, cost)
                print(f"⚠️ Image generation blocked/failed. Response: {response}")
                await inter.followup.send("❌ 이미지를 생성하지 못했어요. 포인트는 환불해 드렸습니다.\n(구글의 안전 필터 또는 인물 생성 정책에 의해 차단되었을 가능성이 높습니다.)")
                return
            
            # 2. Save to a local file
            filename = f"{uuid.uuid4()}.png"
            filepath = os.path.join(self.temp_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(image_data)
                
            # 3. Upload to Discord
            file = discord.File(filepath, filename=filename)
            embed = discord.Embed(
                title="🎨 히아킨의 그림 선물",
                description=f"**요청**: {프롬프트}",
                color=0x9b59b6 # Purple-ish
            )
            embed.set_image(url=f"attachment://{filename}")
            embed.set_footer(text=f"Model: {IMAGE_MODEL} | 비용: {cost:,} P | 5분 후 서버에서 삭제됨")
            
            await inter.followup.send(embed=embed, file=file)
            
            # 4. Schedule deletion
            self.bot.loop.create_task(self._delete_file_after_delay(filepath, 300))

        except Exception:
            # Refund on error
            attendance_cog.add_points(inter.user.id, cost)
            # 상세 오류는 콘솔에만 남기고, 디스코드에는 일반 메시지만 전송
            print(f"❌ [hyacine_image] 이미지 생성 실패 (user={inter.user.id})")
            traceback.print_exc()
            await inter.followup.send("❌ 이미지 생성 중 오류가 발생했어요. 포인트는 환불해 드렸습니다.")

async def setup(bot: commands.Bot):
    await bot.add_cog(HyacineImageCog(bot))
