# Discord Bot / 각 채널에 보낼 임베디드 메시지 템플릿

# 일회용 실행을 위한 파일이므로 prefix 명령어를 사용함
# 관리자-문의 채널에서 (*문의), (*단계1), (*단계2), (*단계3), (*단계4)를 입력하면 임베드된 공지 내용을 출력함

import discord
import os
from discord.ext import commands
from dotenv import load_dotenv

CONTACTADMIN_CHANNEL_ID = 1

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
@client.command(name="문의")
async def command_list(ctx):
    if ctx.channel.id != CONTACTADMIN_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 관리자-문의 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="📢 관리자에게 연락하기",
        description=(
            "땅끝소초 서버에 관한 문의사항이 있으시면 언제든지 연락주시기 바랍니다. 여행자/개척자/방랑자님의 문의에 최대한 빠르게 답변드릴 수 있도록 노력하겠습니다!"
        ),
        color=discord.Color.green()
    )

    embed.add_field(
        name="📮 어떻게 연락하나요?",
        value=(
            "관리자에게 연락하려면 다음 단계를 따라주세요.\n"
            "1. 오른쪽 사용자 목록에서 <@575252669443211264>을 찾아주세요.\n"
            "2. 티켓을 열려면 마우스 오른쪽 버튼을 클릭하고 **메시지**를 클릭해주세요.\n"
            "3. 원하는 내용의 메시지를 작성해주세요.\n\n"
            "다음 사항에 관해 관리자에게 문의할 수 있습니다.\n"
            "1. 커뮤니티 서버 멤버의 의심스러운 활동 (예시: 스팸 메시지 게시, 서버 규칙 위반, 민감한 개인정보 요구)\n"
            "2. 적용된 관리 처분에 대한 설명 요청 또는 이의 제기 절차 진행\n"
            "3. 역할 및 채널 등 커뮤니티와 관련된 문제 (예시: 개인별 역할 부여, 관리자 권한 부여 심사)\n"
            "4. 서버의 신규 기능 제안 또는 서버 구조 개선\n"
            "5. 서버에 존재하는 각종 버그 제보"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📩 ModMail 봇 사용 지침",
        value=(
            "문의 내용과 관련하여 관리자에게 DM으로 연락하지 마시기 바랍니다. 모든 사항은 <@575252669443211264>을 이용해 주세요. 관리자에게 협박, 괴롭힘 등의 목적으로 DM을 보내는 경우 즉각적인 영구 차단의 대상이 됩니다.\n\n"
            "1. 스팸을 보내거나 ModMail을 남용하지 마십시오. ModMail을 부적절하게 사용하는 멤버는 관리 처분을 받을 수 있습니다.\n"
            "2. ModMail에 DM을 보낼 때 관리자가 문의 내용을 효율적으로 처리할 수 있도록 귀하가 겪고 있는 문제나 문의사항을 명확하고 간결하게 알려주세요.\n"
        ),
        inline=False
    )

    embed.add_field(
        name="📫 관리 팀 멤버를 호출할 수 있는 경우",
        value=(
            "커뮤니티 서버에 긴급히 해결을 요하는 문제가 발생하는 경우 관리자를 호출하실 수 있습니다. 아무 이유 없이 관리자를 반복해서 호출하지 마세요."
        ),
        inline=False
    )

    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@client.command(name="단계1")
async def command_list(ctx):
    if ctx.channel.id != CONTACTADMIN_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 관리자-문의 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    file = discord.File("screenshot1.png", filename="screenshot1.png")
    embed = discord.Embed(
        title="1단계: ModMail 봇의 프로필을 찾으세요.",
        description=(
            "PC의 경우 오른쪽 사용자 목록에서 <@575252669443211264>을 찾아 클릭하세요.\n"
            "모바일의 경우 오른쪽 상단의 돋보기 아이콘을 탭하여 사용자 목록에 액세스한 후 <@575252669443211264>을 탭하세요."
        ),
        color=discord.Color.blue()
    )
    embed.set_image(url="attachment://screenshot1.png")
    embed.timestamp = ctx.message.created_at
    await ctx.send(
        embed=embed,
        file=file
    )

@client.command(name="단계2")
async def command_list(ctx):
    if ctx.channel.id != CONTACTADMIN_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 관리자-문의 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    file = discord.File("screenshot2.png", filename="screenshot2.png")
    embed = discord.Embed(
        title="2단계: ModMail에 DM을 작성하세요.",
        description=(
            "PC의 경우 프로필 아래의 입력 창에 메시지를 입력하여 ModMail로 바로 DM을 보낼 수 있습니다.\n"
            "모바일의 경우 프로필의 **메시지**를 탭하면 ModMail의 DM으로 바로 연결됩니다."
        ),
        color=discord.Color.blue()
    )
    embed.set_image(url="attachment://screenshot2.png")
    embed.timestamp = ctx.message.created_at
    await ctx.send(
        embed=embed,
        file=file
    )

@client.command(name="단계3")
async def command_list(ctx):
    if ctx.channel.id != CONTACTADMIN_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 관리자-문의 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    
    file = discord.File("screenshot3.png", filename="screenshot3.png")
    embed = discord.Embed(
        title="3단계: 확인(Confirmation) 메시지가 뜹니다. ✅를 눌러 ModMail 티켓을 열어주세요.",
        description=(
            "**땅끝소초 커뮤니티 서버**를 대상으로 여는 티켓임을 확인하는 메시지가 있는지 반드시 확인해주세요."
        ),
        color=discord.Color.blue()
    )
    embed.set_image(url="attachment://screenshot3.png")
    embed.timestamp = ctx.message.created_at
    await ctx.send(
        embed=embed,
        file=file
    )

@client.command(name="단계4")
async def command_list(ctx):
    if ctx.channel.id != CONTACTADMIN_CHANNEL_ID:
        await ctx.send("❌ 이 명령어는 관리자-문의 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    file = discord.File("screenshot4.png", filename="screenshot4.png")
    embed = discord.Embed(
        title="4단계: DM이 성공적으로 전송되었음을 의미하는 자동 응답 메시지가 표시되었는지 확인하세요.",
        description=(
            "관리자가 귀하의 문의에 응답하기까지 시간이 소요될 수 있습니다. 답장이 올 때까지 잠시 기다려주세요.\n"
            "**ModMail에 보낸 문의는 무조건 승인되는 것이 아니며, 적절한 내용이 아닌 경우에는 도움을 드리기 어려울 수 있습니다.**"
        ),
        color=discord.Color.blue()
    )
    embed.set_image(url="attachment://screenshot4.png")
    embed.timestamp = ctx.message.created_at
    await ctx.send(
        embed=embed,
        file=file
    )

# 1. .env 파일에서 토큰을 로드
load_dotenv(dotenv_path="DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")
