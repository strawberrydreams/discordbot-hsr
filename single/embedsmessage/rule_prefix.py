# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 규칙 채널에서 (*규칙)를 입력하면 임베드된 공지 내용을 출력함

import discord
import os 
from discord.ext import commands
from dotenv import load_dotenv

RULE_CHANNEL_ID = 1

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
@client.command(name="규칙")
async def command_list(ctx):
    if ctx.channel.id != RULE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 규칙 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📘 디스코드 서버 이용 규칙\n",
        description="모든 멤버는 아래 규칙을 반드시 준수해야 합니다.",
        color=discord.Color.green()
    )

    embed.add_field(
        name="📌 규칙 1: 디스코드 이용약관 및 디스코드 커뮤니티 가이드라인을 준수해주세요.",
        value=(
            "[이용약관 보기](https://discord.com/terms)\n"
            "[가이드라인 보기](https://discord.com/guidelines)"
        ),
        inline=False
    )

    embed.add_field(
        name="📌 규칙 2: 다른 멤버를 존중해 주세요.",
        value=(
            "- 자극적, 악의적 컨텐츠에 대한 메시지를 게시하지 말아주세요.\n"
            "- 영리 목적으로 홍보 및 광고에 대한 메시지를 게시하지 말아주세요.\n"
            "- 다른 멤버에 대한 괴롭힘, 위협, 모욕, 인종차별 등의 공격성 메시지를 게시하지 말아주세요.\n"
            "- 다른 멤버의 개인정보를 무단으로 공유하거나 요청하지 말아주세요."
        ),
        inline=False
    )

    embed.add_field(
        name="📌 규칙 3: 부적절한 컨텐츠를 게시하지 말아주세요.",
        value=(
            "- 음란물, 고어, 불법 복제, 기타 불건전한 내용을 포함한 사진 및 동영상을 게시하지 말아주세요.\n"
            "- 미성년자를 성적 대상화하는 컨텐츠, 인종차별적 내용을 담은 컨텐츠를 게시하지 말아주세요."
        ),
        inline=False
    )

    embed.add_field(
        name="📌 규칙 4: 불법적인 주제에 대해 토론하지 말아주세요.",
        value=(
            "- 계정 거래 또는 판매에 대한 메시지를 게시하지 말아주세요.\n"
            "- 해킹 및 사이버 범죄에 관련된 메시지를 게시하지 말아주세요.\n"
            "- 부적절한 현금 거래, 사기를 목적으로 하는 거래에 관련된 메시지를 게시하지 말아주세요.\n"
            "- 마약, 성매매, 인신매매에 관련된 메시지를 게시하지 말아주세요."
        ),
        inline=False
    )

    embed.add_field(
        name="📌 규칙 5: 인간 세상의 예술가들을 존중해주세요.",
        value=(
            "- 생성형 AI를 이용하여 제작된 그림은 기본적으로 허용됩니다.\n"
            "- Sora, Midjourney, Stable Diffusion 등 그림 생성 AI 서비스를 자유롭게 이용할 수 있습니다.\n"
            "- 특정 프롬프트를 직접 입력하여 제작된 그림은 사용자의 창의적인 아이디어를 AI를 활용하여 시각화한 결과물로 간주합니다.\n"
            "- 다른 작가의 그림 스타일, 구도 등을 모방하거나, 기존 저작물을 기반으로 변형하여 제작된 그림은 저작권 침해의 소지가 있으므로 금지됩니다.\n"
            "- 이는 원작자의 권리를 존중하고 창작 생태계를 보호하기 위해 위함입니다."
        ),
        inline=False
    )

    embed.set_footer(text="🔒 위반 시 제재 조치가 취해질 수 있습니다.")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)
    
# 1. .env 파일에서 토큰을 로드
load_dotenv(dotenv_path="DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")
