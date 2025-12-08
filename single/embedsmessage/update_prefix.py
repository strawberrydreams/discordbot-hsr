# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 업데이트 채널에서 (*업데이트)를 입력하면 임베드된 공지 내용을 출력함 

import discord
import os
from discord.ext import commands
from dotenv import load_dotenv

UPDATE_CHANNEL_ID = 1

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
@client.command(name="업데이트")
async def command_list(ctx):
    if ctx.channel.id != UPDATE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 업데이트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📢 업데이트 채널 안내",
        description=(
            "이곳은 업데이트 기록 채널입니다.\n"
            "땅끝소초 커뮤니티 서버의 공식 업데이트 기록 공간입니다.\n"
            "Discord Official의 공식 알림과 서버 내부 기능 변경사항이 업로드됩니다."
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="🔐 접근 권한 안내",
        value=(
            "- 이 채널은 관리자 권한을 가진 멤버만 접근할 수 있습니다.\n"
            "- 일반 유저는 접근 또는 열람이 제한됩니다."            
        ),
        inline=False
    )

    embed.add_field(
        name="📌 주요 내용",
        value=(
            "- Discord 측에서 배포하는 새로운 기능, 정책 변경 등 공지\n"
            "- 땅끝소초 서버의 구조, 기능, 역할, 채널 등의 업데이트 내역 공유"
        ),
        inline=False
    )

    embed.set_footer(text="업데이트 내용은 실제 서버 운영에 반영됩니다.")
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="가격")
async def command_list(ctx):
    if ctx.channel.id != UPDATE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 업데이트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="AI 모델별 API 호출 가격",
        description=(
            "모델 이름 / 입력 비용 / 출력 비용\n"
            "단위: 100만 토큰"
        ),
        color=discord.Color.dark_gold()
    )

    embed.add_field(
        name="OpenAI API",
        value=(
            "ChatGPT-4.5 / $75.00 / $150.00\n"
            "ChatGPT-o1 / $15.00 / $60.00\n"
            "ChatGPT-o3 / $10.00 / $40.00\n"
            "ChatGPT-4o / $2.50 / $10.00\n"
            "ChatGPT-4.1 / $2.00 / $8.00\n"
            "ChatGPT-4o mini / $0.15 / $0.60\n"
            "ChatGPT-4.1 mini / $0.40 / $1.60"
        ),
        inline=False
    )

    embed.add_field(
        name="Google Gemini API",
        value=(
            "Gemini 2.5 Pro / $1.25 / $10.00\n"
            "Gemini 1.5 Pro / $1.25 / $5.00\n"
            "Gemini 2.5 Flash / $0.15 / $0.60\n"
            "Gemini 2.0 Flash / $0.10 / $0.40\n"
            "Gemini 1.5 Flash / $0.075 / $0.30"
        ),
        inline=False
    )

    embed.add_field(
        name="Anthropic Claude API",
        value=(
            "Claude Opus 4 / $15.00 / $75.00\n"
            "Claude Opus 3 / $15.00 / $75.00\n"
            "Claude Sonnet 4 / $3.00 / $15.00\n"
            "Claude Sonnet 3.7 / $3.00 / $15.00\n"
            "Claude Haiku 3.5 / $0.80 / $4.00\n"
            "Claude Haiku 3 / $0.25 / $1.25\n"
        ),
        inline=False
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

# 1. .env 파일에서 토큰을 로드
load_dotenv(dotenv_path="DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")
