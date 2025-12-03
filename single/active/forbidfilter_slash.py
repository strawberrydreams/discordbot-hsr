# Discord Bot / 모든 채널에서 금지어 필터링 기능을 작동시키는 템플릿

# 이 파일에서는 slash 명령어를 사용함
# 모든 채팅 채널에서 금지어가 포함된 채팅을 발견하면 경고 메시지를 출력함
# 금지어 목록은 prohibited_words.json 파일 참조

import discord
import os
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

""" # Using Google Sheets API
import json
import requests

def load_banned_words_from_sheet(sheet_id, api_key, range_name="Sheet1!A:A"):
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}"
        f"?key={api_key}"
    )
    response = requests.get(url)
    if response.status_code != 200:
        print("❌ Google Sheets 불러오기 실패:", response.text)
        return []

    values = response.json().get("values", [])
    return [row[0].strip().lower() for row in values if row]
"""
# 금지어 배열 선언
banned_words = []

# DISCORD TOKEN을 불러오는 load_dotenv()는 금지어 .json 파일을 불러오기 전에 먼저 실행해야 함
load_dotenv(dotenv_path="DISCORD_TOKEN.env")

# 1. 금지어 목록이 .txt 파일일 때
def load_prohibited_words():
    try:
        with open("prohibited_words.txt", "r", encoding="utf-8") as f:
            return [line.strip().lower() for line in f if line.strip()]
    except FileNotFoundError:
        print("⚠️ 금칙어 파일이 존재하지 않습니다.")
        return []
    
"""
# 2. 금지어 목록이 .json 파일일 때
def load_prohibited_words():
    try:
        with open("prohibited_words.json", "r", encoding="utf-8") as f:
            words = json.load(f)
            return [word.strip().lower() for word in words if isinstance(word, str) and word.strip()]
    except FileNotFoundError:
        print("⚠️ 금칙어 파일이 존재하지 않습니다.")
        return []
    except json.JSONDecodeError:
        print("⚠️ JSON 파일 포맷이 맞지 않습니다.")
        return []
"""

def reload_prohibited_words():
    global banned_words
    banned_words = load_prohibited_words()
    print("📥 금지어 목록을 새로 불러왔습니다.")

# 클라이언트 및 명령 트리 초기화
intents = discord.Intents.default()
intents.message_content = True

class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(reload_banned_words_command)
        reload_prohibited_words()  # 봇 시작 시 금지어 로드
        self.add_listener(on_message_filter, "on_message")
        await self.tree.sync()

client = MyClient()

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

# 이벤트: 메시지 감시 필터링
async def on_message_filter(message: discord.Message):
    if message.author.bot:
        return

    lowered = message.content.lower()
    detected_words = [word for word in banned_words if word in lowered]

    if detected_words:
        words_list = ", ".join(f"**{word}**" for word in detected_words)
        await message.channel.send(
            f"⚠️ {message.author.mention} 삐삑~~ 나쁜 단어 {words_list} 금지! 금지! 🛑🧸"
        )

# slash 명령어 등록 (금지어 리로드)
@app_commands.command(name="금지어리로드", description="금지어 목록을 다시 불러옵니다.")
async def reload_banned_words_command(interaction: discord.Interaction):
    reload_prohibited_words()
    await interaction.response.send_message("📥 금지어 목록을 새로 불러왔습니다!", ephemeral=True)

# 1. .env 파일에서 토큰을 로드
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")