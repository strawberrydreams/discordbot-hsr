# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 모드메일-명령어 채널에서 (*모드), (*구성), (*핵심), (*일반), (*기타)를 입력하면 임베드된 공지 내용을 출력함 

import discord
import os
from discord.ext import commands
from dotenv import load_dotenv

MODMAIL_COMMAND_CHANNEL_ID = 1368382842782093443

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
@client.command(name="모드")
async def command_list(ctx):
    if ctx.channel.id != MODMAIL_COMMAND_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 모드메일-명령어 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="ModMail 시스템 로그 채널",
        description=(
            "- 이 채널은 ModMail 봇의 설정 변경, 명령어 실행 기록, 티켓 열림/닫힘 등의 시스템 이벤트 로그를 저장합니다.\n"
            "- 이 채널에는 유저와 관리자의 대화 내용은 기록되지 않으며, 봇의 시스템 동작 및 명령어 관련 로그만 표시됩니다.\n"
            "- 일반 유저는 접근할 수 없으며, 운영진은 여기서 ModMail 봇의 상태를 확인할 수 있습니다.\n"
            "- 이 채널은 ModMail 봇이 작동하기 위해서 반드시 필요합니다."
        ),
        color=discord.Color.green()
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="구성")
async def command_list(ctx):
    if ctx.channel.id != MODMAIL_COMMAND_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 모드메일-명령어 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔧 구성 명령어 (Configuration)",
        description=(
            "**=setup**\n"
            "ModMail을 서버에 설정합니다.(처음 실행 시에만)\n"
            "**=prefix 접두사**\n"
            "명령어 접두사를 변경하거나 현재 접두사를 확인합니다.\n"
            "**=category 이름**\n"
            "ModMail 채널을 위한 카테고리를 재생성합니다.\n"
            "**=accessrole 역할**\n"
            "티켓 관련 명령어 및 응답 권한이 있는 역할을 설정하거나 해제합니다.\n"
            "**=commandonly**\n"
            "티켓에 응답할 때 명령어 사용을 필수로 설정하거나 해제합니다.\n"
            "**=anonymous**\n"
            "기본 익명 메시지 전송을 설정하거나 해제합니다.\n"
            "**=toggle 이유**\n"
            "티켓 생성을 가능하게 하거나 비활성화합니다.\n"
            "**=viewconfig**\n"
            "현재 서버의 설정을 확인합니다."
        ),
        color=discord.Color.green()
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="핵심")
async def command_list(ctx):
    if ctx.channel.id != MODMAIL_COMMAND_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 모드메일-명령어 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="💬 핵심 명령어 (Core)",
        description=(
            "**=reply 메시지**\n"
            "티켓에 응답합니다.\n"
            "**=areply 메시지**\n"
            "티켓에 익명으로 응답합니다.\n"
            "**=close 이유**\n"
            "티켓 채널을 닫습니다.\n"
            "**=aclose 이유**\n"
            "현재 티켓 채널을 익명으로 닫습니다.\n"
            "**=closeall 이유**\n"
            "모든 티켓 채널을 닫습니다.\n"
            "**=acloseall 이유**\n"
            "모든 티켓 채널을 익명으로 닫습니다.\n"
            "**=blacklist 유저**\n"
            "티켓 생성을 차단할 사용자를 블랙리스트에 추가합니다.\n"
            "**=whitelist 유저**\n"
            "티켓 생성을 허용할 사용자를 화이트리스트에 추가합니다.\n"
            "**=blacklistclear**\n"
            "블랙리스트를 초기화합니다.\n"
            "**=viewblacklist**\n"
            "블랙리스트를 확인합니다."
        ),
        color=discord.Color.green()
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="일반")
async def command_list(ctx):
    if ctx.channel.id != MODMAIL_COMMAND_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 모드메일-명령어 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="ℹ️ 일반 명령어 (General)",
        description=(
            "**=help 명령어**\n"
            "도움말 메뉴를 표시하거나 특정 명령어에 대한 정보를 제공합니다.\n"
            "**=ping**\n"
            "봇의 지연 시간을 확인합니다.\n"
            "**=stats**\n"
            "봇의 통계를 확인합니다.\n"
            "**=website**\n"
            "ModMail 웹사이트 링크를 제공합니다."
        ),
        color=discord.Color.green()
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="기타")
async def command_list(ctx):
    if ctx.channel.id != MODMAIL_COMMAND_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 모드메일-명령어 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🛠️ 기타 명령어 (Miscellaneous)",
        description=(
            "**=permissions 멤버 채널**\n"
            "특정 채널에서 멤버의 권한을 확인합니다.\n"
            "**=userinfo 멤버**\n"
            "자신이나 지정한 멤버의 정보를 확인합니다.\n"
            "**=serverinfo**\n"
            "서버 정보를 확인합니다."
        ),
        color=discord.Color.green()
    )
    embed.timestamp = ctx.message.created_at
    await ctx.send(embed=embed)

@client.command(name="작동")
async def command_list(ctx):
    if ctx.channel.id != MODMAIL_COMMAND_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 모드메일-명령어 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="ModMail 봇 작동 방식",
        description=(
            "- 유저가 ModMail 봇에게 디스코드 DM을 보내면, 서버에 해당 유저의 전용 티켓 채널이 자동으로 생성됩니다.\n"
            "- 이 채널에서 유저와 관리자는 1:1로 대화할 수 있으며, 다른 유저에게는 보이지 않습니다.\n"
            "- 운영진은 =reply, =close 같은 명령어를 사용하여 응답하거나 티켓을 종료할 수 있으며, 이 모든 과정은 봇이 중계합니다.\n"
            "- 채널은 한 유저당 하나씩만 열리며, 티켓이 닫히면 자동으로 삭제됩니다.\n"
            "- 블랙리스트, 화이트리스트 기능을 통해 특정 유저의 디스코드 DM을 받을지 여부를 선택할 수 있습니다."
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
# client.run("your_bot_token")
