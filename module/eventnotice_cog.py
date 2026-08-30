from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from module.database import (
    GuildSettingsRepository,
    create_guild_settings_repository,
    run_db,
)


class EventNoticeCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        settings_repository: Optional[GuildSettingsRepository] = None,
    ):
        self.bot = bot
        self.settings_repository = (
            settings_repository or create_guild_settings_repository()
        )

    @staticmethod
    def _current_and_upcoming_events(events, current_time):
        return sorted(
            (
                event
                for event in events
                if not event.end_time or event.end_time > current_time
            ),
            key=lambda event: (event.start_time, event.id),
        )

    @app_commands.command(name="이벤트", description="서버 이벤트 목록 또는 특정 번호의 상세 정보를 보여줍니다.")
    @app_commands.describe(index="이벤트 번호 (1부터 시작)")
    @app_commands.guild_only()
    async def _show_events(
        self, interaction: discord.Interaction, index: Optional[int] = None
    ):
        event_channel_id = await run_db(
            self.settings_repository.get_event_channel,
            interaction.guild_id,
        )
        if event_channel_id and interaction.channel_id != event_channel_id:
            await interaction.response.send_message(
                f"❌ `/이벤트`는 <#{event_channel_id}> 채널에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        # 응답을 예약 (타임아웃 방지)
        await interaction.response.defer()

        events = await interaction.guild.fetch_scheduled_events()

        # 현재 유효한 이벤트만 필터링
        current_time = datetime.now(timezone.utc)
        visible_events = self._current_and_upcoming_events(events, current_time)

        if not visible_events:
            await interaction.followup.send("현재 진행 중이거나 예정된 이벤트가 없습니다!")
            return

        if index is None:
            lines = [
                f"`{number}.` **{event.name}** — <t:{int(event.start_time.timestamp())}:F>"
                for number, event in enumerate(visible_events[:25], start=1)
            ]
            if len(visible_events) > 25:
                lines.append(f"외 {len(visible_events) - 25}개")
            embed = discord.Embed(
                title="📅 서버 이벤트 목록",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed)
            return

        if index <= 0 or index > len(visible_events):
            await interaction.followup.send(
                f"❌ 잘못된 번호입니다. (1 ~ {len(visible_events)} 사이로 입력하세요)"
            )
            return

        # 유효한 이벤트 리스트에서 선택
        event = visible_events[index - 1]

        embed = discord.Embed(
            title=f"이벤트 {index} - {event.name}",
            description=event.description or "설명 없음",
            color=discord.Color.blue()
        )

        # 종료 시간 타임스탬프 표시
        if event.end_time:
            end_timestamp = int(event.end_time.timestamp())
            relative_end_time = f"<t:{end_timestamp}:R>"
        else:
            relative_end_time = "종료 시간 정보 없음"
        embed.add_field(
            name="⏳ 종료까지 남은 시간",
            value=relative_end_time,
            inline=False,
        )

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
