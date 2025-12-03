# Discord Bot / 이벤트 채널의 이벤트 정보 업로드용 모듈

# 이 파일에서는 prefix 명령어를 사용함
# 이벤트 채널에서 (*이벤트 [숫자])를 입력하면 임베드된 공지 내용을 출력함

import discord
from discord.ext import commands
from datetime import datetime, timezone

EVENT_CHANNEL_ID = 1365885232489955428

# prefix 명령어 등록
def setup_event_commands(bot):
    @bot.command(name="이벤트")
    async def show_specific_event(ctx, index: int):
        if ctx.channel.id != EVENT_CHANNEL_ID:
            await ctx.send("❌ 이 명령어는 이벤트 채널에서만 사용할 수 있습니다.")
            return

        events = await ctx.guild.fetch_scheduled_events()

        # 현재 유효한 이벤트만 필터링
        now = datetime.now(timezone.utc)
        valid_events = [
            event for event in events
            if not event.end_time or event.end_time > now
        ]

        if not valid_events:
            await ctx.send("현재 진행 중이거나 예정된 이벤트가 없습니다!")
            return

        if index <= 0 or index > len(valid_events):
            await ctx.send(f"❌ 잘못된 번호입니다. (1 ~ {len(valid_events)} 사이로 입력하세요)")
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
        if event.creator:
            creator_mention = event.creator.mention
        else:
            creator_mention = "알 수 없음"

        embed.add_field(name="👤 이벤트 작성자", value=creator_mention, inline=False)

        # 이벤트 위치(주소) 표시
        if event.location:
            embed.add_field(name="📍 이벤트 장소", value=event.location, inline=False)

        # 커버 이미지 삽입
        if event.cover_image:
            embed.set_image(url=event.cover_image.url)

        await ctx.send(embed=embed)
