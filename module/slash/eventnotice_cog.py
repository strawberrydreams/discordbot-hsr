import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from module.slash.config import EVENT_CHANNEL_ID

class EventNoticeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="이벤트", description="특정 번호의 서버 이벤트 정보를 보여줍니다.")
    @app_commands.describe(index="이벤트 번호 (1부터 시작)")
    async def show_specific_event(self, interaction: discord.Interaction, index: int):
        # 먼저 응답을 예약 (타임아웃 방지)
        await interaction.response.defer()

        if interaction.channel_id != EVENT_CHANNEL_ID:
            await interaction.followup.send("❌ 이 명령어는 이벤트 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        events = await interaction.guild.fetch_scheduled_events()

        # 현재 유효한 이벤트만 필터링
        now = datetime.now(timezone.utc)
        valid_events = [
            event for event in events
            if not event.end_time or event.end_time > now
        ]

        if not valid_events:
            await interaction.followup.send("현재 진행 중이거나 예정된 이벤트가 없습니다!", ephemeral=True)
            return

        if index <= 0 or index > len(valid_events):
            await interaction.followup.send(
                f"❌ 잘못된 번호입니다. (1 ~ {len(valid_events)} 사이로 입력하세요)", ephemeral=True
            )
            return

        # 유효한 이벤트 리스트에서 선택
        event = valid_events[index - 1]

        embed = discord.Embed(
            title=f"이벤트 {index} - {event.name}",
            description=event.description or "설명 없음",
            color=discord.Color.blue()
        )

        # 종료 시간 타임스탬프 표시
        if event.end_time:
            unix_timestamp = int(event.end_time.timestamp())
            remaining_str = f"<t:{unix_timestamp}:R>"
        else:
            remaining_str = "종료 시간 정보 없음"
        embed.add_field(name="⏳ 종료까지 남은 시간", value=remaining_str, inline=False)

        # 이벤트 작성자 표시
        creator_mention = event.creator.mention if event.creator else "알 수 없음"
        embed.add_field(name="👤 이벤트 작성자", value=creator_mention, inline=False)

        # 이벤트 위치(주소) 표시
        if event.location:
            embed.add_field(name="📍 이벤트 장소", value=event.location, inline=False)

        # 커버 이미지 삽입
        if event.cover_image:
            embed.set_image(url=event.cover_image.url)

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(EventNoticeCog(bot))
