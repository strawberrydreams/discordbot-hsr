# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 프라이빗-라운지 채널에서 (*프라이빗)를 입력하면 임베드된 공지 내용을 출력함

import discord
import os
from discord.ext import commands
from dotenv import load_dotenv

PRIVATELOUNGE_CHANNEL_ID = 1367567762981130351

intents = discord.Intents.default()
intents.message_content = True

client = commands.Bot(command_prefix='*', intents=intents)

# 봇 상태 메시지 설정 (하나만 적용 가능)
@client.event
async def on_ready():
    print(f'✅ {client.user} 봇이 실행되었습니다!')
    
    activity = discord.Game(name="게임 제목") # Fix this part
    # activity = discord.Streaming(name="broadcast_title", url="broadcast_link")
    # activity = discord.Activity(type=discord.ActivityType.listening, name="music_title")
    # activity = discord.Activity(type=discord.ActivityType.watching, name="video_title")

    await client.change_presence(status=discord.Status.online, activity=activity)
    # await client.change_presence(status=discord.Status.idle, activity=activity)
    # await client.change_presence(status=discord.Status.dnd, activity=activity)
    # await client.change_presence(status=discord.Status.invisible, activity=activity)

# prefix 명령어 등록
@client.command()
async def 프라이빗(ctx):
    if ctx.channel.id != PRIVATELOUNGE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 프라이빗-라운지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🎩 프라이빗-라운지에 오신 걸 환영합니다.",
        description=(
            "이곳은 극히 소수에게만 출입이 허락된,\n"
            "눈에 띄지 않는 이야기와 조용한 속삭임이 깃드는 공간입니다.\n\n"
            "공식적인 규칙도, 정해진 주제도 없습니다.\n"
            "다만 이곳에 들어섰다는 사실만으로도, 자격은 이미 입증된 셈입니다.\n\n"
            "말은 벽에 스며들고, 고요 속에 깊은 생각이 흘러갑니다.\n"
            "시간은 천천히 흐르고, 대화는 깊게 내려앉습니다.\n\n"
            "**프라이빗-라운지**는 공간과 분위기의 조화로써 완성되며,\n"
            "선택받은 자만이 이 비밀 공간의 조명을 받습니다."
        ),
        color=discord.Color.from_rgb(75, 0, 130)
    )

    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

# 1. .env 파일에서 토큰을 로드
load_dotenv(dotenv_path="DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")
