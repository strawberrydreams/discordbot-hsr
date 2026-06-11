# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 명령어 채널에서 (*명령어)를 입력하면 임베드된 공지 내용을 출력함

import discord
import os
from discord.ext import commands
from dotenv import load_dotenv

COMMAND_CHANNEL_ID = 1

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
@client.command(name="명령어")
async def command_list(ctx):
    if ctx.channel.id != COMMAND_CHANNEL_ID:
        # prefix 명령어의 ctx.send는 ephemeral을 지원하지 않음
        await ctx.send("❌ 이 명령어는 명령어 채널에서만 사용할 수 있습니다.")
        return

    embed = discord.Embed(
        title="⚙️ 명령어 테스트 채널 안내",
        description=(
            "이곳은 명령어 테스트 채널입니다.\n"
            "Discord Bot의 명령어를 실험하고 테스트할 수 있는 공간입니다.\n"
            "땅끝소초 서버의 다양한 기능을 안정적으로 적용하기 위해 사용됩니다."
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="🔐 접근 권한 안내",
        value=(
            "- 이 채널은 관리자 및 테스트 담당자만 접근할 수 있습니다.\n"
            "- 일반 유저는 접근 또는 열람이 제한됩니다"
        ),
        inline=False
    )

    embed.add_field(
        name="🛠️ 무엇을 할 수 있나요?",
        value=(
            "- 새로운 Bot 명령어 실험\n"
            "- 공지/역할 자동화 기능 테스트\n"
            "- 서버 공개 채널에 실제로 적용하기 전 검증 단계"
        ),
        inline=False
    )

    embed.set_footer(text="실험 결과는 실제 채널에 반영될 수 있습니다.")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

# 1. .env 파일에서 토큰을 로드 (프로젝트 루트에서 실행 기준)
load_dotenv(dotenv_path="settings/DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")
