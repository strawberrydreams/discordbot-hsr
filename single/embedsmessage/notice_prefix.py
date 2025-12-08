# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 공지 채널에서 (*공지)를 입력하면 임베드된 공지 내용을 출력함 

import discord
import os
from discord.ext import commands
from dotenv import load_dotenv

NOTICE_CHANNEL_ID = 1

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
@client.command(name="공지")
async def command_list(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🎉 땅끝소초에 오신 걸 환영합니다!",
        description=(
            "땅끝소초는 친구들과 함께 게임도 즐기고,\n"
            "팬아트, 창작물, 뉴스 등을 자유롭게 공유하는 커뮤니티입니다.\n"
            "물론 AI를 이용한 작품도 허용됩니다!\n"
            "땅끝소초는 기술의 무한한 발전을 지향합니다."
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="📌 이런 걸 할 수 있어요!",
        value=(
            "- 다양한 게임을 함께 플레이 🎮\n"
            "- 내가 만든 그림, 영상, 창작물 공유 🎨\n"
            "- 뉴스, 밈, 흥미로운 이야기 나누기 🗞️"
        ),
        inline=False
    )

    embed.add_field(
        name="✅ 꼭 확인해 주세요!",
        value=(
            "- <#1361244860236959844>에서 이용 수칙을 읽고\n"
            "- <id:customize>에서 자신을 설명할 수 있는 항목을 골라주세요."
        ),
        inline=False
    )

    embed.set_footer(text="모두 환영합니다! 따뜻한 커뮤니티가 되길 바라요 😊")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@client.command(name="권한")
async def send_announcement(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="서버에 대한 접근 권한 획득 방법",
        description=(
            "땅끝소초 커뮤니티 서버에서 활동하기 위해서는 <@&1361278035545817210>, <@&1361278297601867899>, <@&1361278386881953812> 중 하나의 역할을 획득해야 합니다."
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="📘 규칙을 먼저 읽어보세요.",
        value=(
            "- 다음 게시판으로 이동하세요. <id:customize>\n"
            "- 맞춤형 설정 질문에 응답해주세요.\n"
            "- 마지막 질문에서 무엇을 선택하느냐에 따라 역할이 달라집니다.\n"
            "- <@&1361278035545817210> 역할을 획득하기 위해서는 [여행자]를 선택하세요.\n"
            "- <@&1361278297601867899> 역할을 획득하기 위해서는 [개척자]를 선택하세요.\n"
            "- <@&1361278386881953812> 역할을 획득하기 위해서는 [방랑자]를 선택하세요."
        ),
        inline=False
    )

    embed.add_field(
        name="🔄 리프레시",
        value=(
            "- 역할 획득이 정상적으로 동작하지 않을 경우, CTRL + R/CMD + R 을 눌러 프로그램을 새로고침하거나 Discord 모바일 애플리케이션을 재시작하세요."
        ),
        inline=False
    )

    embed.add_field(
        name="💡 기억해두세요!",
        value=(
            "- 역할을 획득하는 것은, 서버 가이드라인을 읽고 그에 동의했음을 의미합니다."
        ),
        inline=False
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="역할1")
async def send_announcement(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🚀 디스코드 전용 연동 역할 출시!",
        description=(
            "여행자님, 개척자님, 방랑자님, 안녕하세요!\n"
            "새로운 디스코드 이벤트가 열렸습니다!"
        ),
        color=discord.Color.blue()
    )

    embed.add_field(
        name="🎉 이벤트에 어떻게 참여하나요?",
        value=(
            "다음 중 최소 1개의 서비스에서 유효한 계정을 디스코드 계정과 연동하면 전용 역할 <@&1375524068370944030>을 자동으로 획득할 수 있게 됩니다.\n"
            "- 인증된 TikTok 계정\n"
            "- Spotify 계정\n"
            "- Amazon Music 계정\n"
            "- Twitch 계정\n"
            "- Youtube 계정"
        ),
        inline=False
    )

    embed.add_field(
        name="🎁 기대해 주세요!",
        value=(
            "추후 더 많은 이벤트가 진행될 예정이니, 다음 소식을 기대해 주세요!"
        ),
        inline=False
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="챗봇")
async def send_annoucement(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    file = discord.File("hyacine1.jpg", filename="hyacine1.jpg")
    embed = discord.Embed(
        title="✨ 히아킨 챗봇 출시 안내 ✨",
        description=(
            "여행자님, 개척자님, 방랑자님, 안녕하세요!\n"
            "놀빛 정원의 따뜻한 의사, 히아킨이 여러분과 대화하기 위해 도착했어요! 🌿☕️"
        ),
        color=discord.Color.fuchsia()
    )

    embed.add_field(
        name="📌 히아킨은 어떤 존재인가요?",
        value=(
            "하늘의 후예로서, 여러분과 함께 불을 쫓는 사명을 완수하겠어요.\n"
            "그리고 놀빛 정원의 의사로서 최선을 다해 여러분을 지원할게요.\n"
            "걱정 마세요, 히아킨이——흔쾌히 도와드릴게요."
        ),
        inline=False
    )

    embed.add_field(
        name="💬 사용 방법",
        value=(
            "**/대화** 명령으로 <@1360882642249060452>에게 말을 걸어보세요! 이미지를 함께 전송하면 더 풍성한 대화를 나눌 수 있어요.\n"
            "**/검색** 명령을 사용하면 여러분이 궁금해하시는 정보를 웹 검색을 통해 찾아드립니다.\n"
            "**/deep** 명령을 사용하면 더 깊은 대화를 나눌 수 있게 되고,\n"
            "**/light** 명령을 사용하면 가벼운 대화를 자주 나눌 수 있게 됩니다."
        ),
        inline=False
    )

    embed.add_field(
        name="🧠 AI 모델 라이브러리",
        value=(
            "기본 대화 모델: **ChatGPT-4o mini** (빠르고 저렴한 소형 모델)\n"
            "고급 대화 모델: **ChatGPT-4.1 mini** (지능, 속도, 비용의 균형)\n"
            "검색 모델: **Gemini 2.0 Flash** (차세대 기능, 속도, 사고, 실시간 스트리밍)\n"
            "AI 모델 라이브러리는 예고 없이 변경될 수 있습니다."
        ),
        inline=False
    )
    embed.set_image(url="attachment://hyacine1.jpg")
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed, file=file)

@client.command(name="역할2")
async def send_announcement(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🚀 디스코드 전용 연동 역할 출시!",
        description=(
            "여행자님, 개척자님, 방랑자님, 안녕하세요!\n"
            "새로운 디스코드 이벤트가 열렸습니다!"
        ),
        color=discord.Color.blue()
    )

    embed.add_field(
        name="🎉 이벤트에 어떻게 참여하나요?",
        value=(
            "다음 중 최소 1개의 서비스에서 유효한 계정을 디스코드 계정과 연동하면 전용 역할 <@&1388524243247169658>을 자동으로 획득할 수 있게 됩니다.\n"
            "- 최소 5개의 게임을 보유한 Steam 계정\n"
            "- Sony PlayStation Network 계정\n"
            "- Microsoft Xbox 계정\n"
            "- Blizzard Battle.net 계정\n"
            "- Sony Bungie.net 계정\n"
            "- Epic Games 계정\n"
            "- Roblox 계정\n"
            "- League of Legends 계정"
        ),
        inline=False
    )

    embed.add_field(
        name="🎁 기대해 주세요!",
        value=(
            "추후 더 많은 이벤트가 진행될 예정이니, 다음 소식을 기대해 주세요!"
        ),
        inline=False
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="역할3")
async def send_announcement(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🚀 디스코드 전용 연동 역할 출시!",
        description=(
            "여행자님, 개척자님, 방랑자님, 안녕하세요!\n"
            "새로운 디스코드 이벤트가 열렸습니다!"
        ),
        color=discord.Color.blue()
    )

    embed.add_field(
        name="🎉 이벤트에 어떻게 참여하나요?",
        value=(
            "다음 중 최소 1개의 서비스에서 유효한 계정을 디스코드 계정과 연동하면 전용 역할 <@&1388471167140237463>을 자동으로 획득할 수 있게 됩니다.\n"
            "- Bluesky 계정\n"
            "- Reddit 계정\n"
            "- 인증된 X(Twitter) 계정\n"
            "- Facebook 계정\n"
            "- GitHub 계정"
        ),
        inline=False
    )

    embed.add_field(
        name="🎁 기대해 주세요!",
        value=(
            "추후 더 많은 이벤트가 진행될 예정이니, 다음 소식을 기대해 주세요!"
        ),
        inline=False
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="역할획득")
async def send_announcement(ctx):
    if ctx.channel.id != NOTICE_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 공지 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="디스코드 전용 연동 역할을 획득하는 방법",
        description=(
            "디스코드 전용 연동 역할의 경우 접속하는 것만으로 자동으로 부여되는 것은 아니며,\n"
            "다음과 같은 방식으로 <@&1375378579373690980>, <@&1388524243247169658> 등 디스코드 전용 연동 역할을 획득할 수 있습니다.\n"
            "- PC 환경에서 Discord [땅끝소초] 서버에 접속하기\n"
            "- 왼쪽 상단의 서버 이름을 클릭하여 서버 설정 메뉴 표시\n"
            "- [연결된 역할] 메뉴 클릭\n"
            "- 획득하고자 하는 역할을 클릭하고 연결 완료"
        ),
        color=discord.Color.green()
    )

    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

# 1. .env 파일에서 토큰을 로드
load_dotenv(dotenv_path="DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("")
