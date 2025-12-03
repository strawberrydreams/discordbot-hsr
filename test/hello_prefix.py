# Discord App 기본 연동 프로그램
# 아무 채팅창에 (*hello)를 입력하면 봇이 (안녕하세요)를 출력함 

import discord # discord.py 라이브러리 참조 (별도 설치 필요)
import os
from discord.ext import commands # Discord 확장 명령어 세트 참조
from dotenv import load_dotenv

intents = discord.Intents.default() # Intents 설정
intents.message_content = True

# macOS에서 python을 직접 설치한 경우, 신뢰할 수 있는 인증 기관(CA)에서 발급한 SSL 인증서가 필요함
# 터미널에서 /Applications/Python\ 3.11/Install\ Certificates.command 명령어 입력 (python 버전에 따라 달라짐)

client = commands.Bot(command_prefix='*', intents=intents) # 실제 명령어 부분 

@client.command()
async def hello(ctx):
    await ctx.send('안녕하세요')

load_dotenv(dotenv_path="../settings/DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)
