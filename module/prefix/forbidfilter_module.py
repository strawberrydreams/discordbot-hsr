# Discord Bot / 모든 채널에서 금지어 필터링 기능을 작동시키는 모듈

# 이 파일에서는 prefix 명령어를 사용함
# 모든 채팅 채널에서 금지어가 포함된 채팅을 발견하면 경고 메시지를 출력함
# 금지어 목록은 forbidden_words.json 파일 참조

import discord
import os
import json
from discord.ext import commands

# 금지어 배열 선언
banned_words = []

# 금지어 목록을 .json 파일에서 불러옴
def load_prohibited_words():
    banned_words_raw = os.getenv("BANNED_WORDS")
    if not banned_words_raw:
        print("⚠️ 금칙어 파일이 존재하지 않습니다.")
        return []
    try:
        return json.loads(banned_words_raw)
    except json.JSONDecodeError:
        print("❌ JSON 포맷이 맞지 않습니다.")
        return []

# 금지어 로드 함수
def register_prohibition_filter(bot):
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='*', intents=intents)

    banned_words = load_prohibited_words()

    # 이벤트: 메시지 감시 필터링
    @bot.event
    async def on_message(message):
        if message.author.bot:
            return

        lowered = message.content.lower()
        detected_words = [word for word in banned_words if word in lowered]

        if detected_words:
            words_list = ", ".join(f"**{word}**" for word in detected_words)
            await message.channel.send(
                f"⚠️ {message.author.mention} 삐삑~~ 나쁜 단어 {words_list} 금지! 금지! 🛑🧸"
            )
            return

        await bot.process_commands(message)

    return bot
