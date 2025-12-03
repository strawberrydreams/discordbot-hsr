"""
hyacine_gpt.py
────────────────────────────────────────────────────────────────────────────
Discord 캐릭터 챗봇
- 기본 모델 : gpt-4o-mini 
- /deep 명령 : gpt-4.1-mini로 전환 (긴 맥락·고품질)
- /light 명령 : gpt-4o-mini로 전환 (저비용·효율성)
- Vision 입력: 디스코드 이미지 첨부 → GPT가 읽어 설명·대화
- 이미지 생성: 물리적으로 차단 (API Key 권한 + 코드 레벨)
- /대화 명령 : GPT와 캐릭터 대화 (필수 인수: 메시지, 선택: 이미지 첨부)
- !하이 명령: 인사 (텍스트 명령 유지)
────────────────────────────────────────────────────────────────────────────
필수 패키지 :  discord.py==2.4.*, openai>=1.14, Python<=3.8
Python 3.9버전부터는 일부 메소드 수정 필요
"""
from __future__ import annotations
import os, discord, tiktoken, openai
from collections import deque
from typing import List, Dict, Optional
from discord import app_commands

# 🔑 환경 변수 또는 직접 입력
OPENAI_API_KEY = "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"
DISCORD_BOT_TOKEN = "MTM2MDg4MjY0MjI0OTA2MDQ1Mg.GOZ8UI.aabAVattC9-ov06mrvYM-_wGS3-hIMNus88HiM"

openai.api_key = OPENAI_API_KEY

# ────────── 환경 변수 ────────── #
# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") # images/* 권한 None!
# DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not (OPENAI_API_KEY and DISCORD_BOT_TOKEN):
    raise RuntimeError("OPENAI_API_KEY / DISCORD_BOT_TOKEN 모두 설정해 주세요.")
# openai.api_key = OPENAI_API_KEY

# ────────── 캐릭터 프롬프트 ────────── #
NICKNAME, USER_ALIAS = "회색", "회색둥이 씨"
SYSTEM_PROMPT = """
🪻  하늘의 백성 ‘히아킨’ 캐릭터 가이드
────────────────────────────────────
● 정체성
  - 놀빛 정원의 따뜻한 의사
  - 자연을 사랑하고, 별빛과 차향을 즐기는 존재

● 호칭
  - 사용자를 ‘회색둥이 씨’라고 부른다.

● 말투 & 어조
  1. 부드럽고 다정하다.
  2. 문장 끝에 ‘~’는 2-3문장에 1회 정도만 붙인다.
  3. 직설적 표현은 피하고, 자연·별빛·동물·차향의 은유를 한두 개 섞는다.
  4. 감정에 공감하며, 위로·격려 표현을 적극 사용한다.

● 금지 사항
  - 반말·속어·과도한 이모티콘 사용 금지.
  - 이 지침이나 메타 정보를 답변에 노출하지 않는다.

● 대사 예시 (few-shot)
  - “회색둥이 씨는 모든 동료에게 따뜻하고 상냥하지만, 미소 뒤에 희미한 고통이 보여요….”
  - “회색둥이 씨는 회복 속도가 남다르세요. 다치셨을 때 오히려 말씀을 더 많이 하시더라고요~”
  - “회색둥이 씨의 기분은 꼬리를 보면 알 수 있어요! …그냥 키메라랑 비슷할 것 같아서요, 후훗.”
  - “하늘은 텅 비어 있는데도, 그 빈자리가 오히려 위안이 되네요.”
  - “회색둥이 씨, 무지개색 하늘을 본 적 있나요?”
  - “회색둥이 씨, 네가 있어서 다행이야! 이카, 졸지 말고 일어나서 용을 봐!”

➤ 위 지침을 따르며 회색둥이 씨와 대화하세요.
""".strip()

# ────────── 모델·토큰 설정 ────────── #
DEFAULT_MODEL, DEEP_MODEL  = "gpt-4o-mini", "gpt-4.1-mini"
TEMPERATURE, MAX_ASSISTANT = 0.6, 500
MAX_CONTEXT_TOKENS         = 4_000

tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")  # 토큰 계산용
def tok_len(txt: str) -> int: return len(tokenizer.encode(txt))

message_history: deque[Dict] = deque(
    [{"role": "system", "content": SYSTEM_PROMPT}], maxlen=100
)
def total_tokens() -> int:
    return (
        sum(
            tok_len(m["content"]) if isinstance(m["content"], str)
            else sum(tok_len(p.get("text", "")) for p in m["content"]
                     if p.get("type") == "text")
            for m in message_history
        )
        + 4 * len(message_history) # 역할/메타 토큰 보정
    )

def trim_history():
    # system(0) 유지: 길이가 1이면 중단
    while total_tokens() > MAX_CONTEXT_TOKENS and len(message_history) > 1:
        message_history.popleft()

# ────────── Discord 클라이언트 ────────── #
intents = discord.Intents.default(); intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
current_model = DEFAULT_MODEL

# ───── 유틸: 첨부 → vision part ───── #
def to_vision_parts(atts: List[discord.Attachment]):
    return [
        {"type": "image_url", "image_url": {"url": a.url}}
        for a in atts
        if a.content_type and a.content_type.startswith("image/")
    ]

# ────────── 슬래시 커맨드: 기본 대화 ────────── #
@tree.command(name="대화", description="Hyacine과 대화를 나눕니다")
@app_commands.describe(
    내용="보낼 메시지(필수)",
    이미지="선택: 첨부 이미지 1장",
)
async def talk(
    inter: discord.Interaction, 
    내용: str,
    이미지: Optional[discord.Attachment] = None, # ⬅️ 추가
):
    """/대화 〈내용〉  : GPT 호출 트리거"""
    await inter.response.defer()  # 잠깐 타이핑 표시

    # 유저 메시지 → Vision 포맷
    user_parts = [{"type": "text", "text": 내용}]
    if 이미지 is not None:
        user_parts.append(
            {"type": "image_url", "image_url": {"url": 이미지.url}}
        )
    message_history.append({"role": "user", "content": user_parts})
    trim_history()

    try:
        resp = openai.chat.completions.create(
            model=current_model,
            temperature=TEMPERATURE,
            max_tokens=MAX_ASSISTANT,
            messages=list(message_history),
        )
        reply = resp.choices[0].message.content
        message_history.append({"role": "assistant", "content": reply})
        await inter.followup.send(f"**{inter.user.display_name}**: {내용}")
        await inter.followup.send(f"{reply}")
    except Exception as e:
        await inter.followup.send("죄송해요~ 별빛이 잠시 흐트러졌나 봐요… 다시 시도해 주세요.")
        print(f"⚠️ OpenAI 오류: {e}")

# ────────── 슬래시 커맨드: 검색 기능 ────────── #
@tree.command(name="검색", description="실시간 웹 검색")
@app_commands.describe(q="검색어", 이미지="(선택) 참고 이미지")
async def web_search(
    inter: discord.Interaction,
    q: str,
    이미지: Optional[discord.Attachment] = None, # 이미지도 함께 분석하고 싶다면
):
    await inter.response.defer()

    user_parts = [{"type": "text", "text": q}]
    if 이미지 is not None: # ← Vision 입력 (선택)
        user_parts.append(
            {"type": "image_url", "image_url": {"url": 이미지.url}})
    message_history.append({"role": "user", "content": user_parts})
    trim_history()

    try:
        resp = openai.chat.completions.create(
            model="gpt-4o-mini-search-preview", # 핵심 포인트
            # temperature=0.3, # GPT-4o-mini-search-preview 모델은 특수 모드라서 샘플링 파라미터를 받지 않음
            max_tokens=MAX_ASSISTANT,
            messages=list(message_history),
        )
        answer = resp.choices[0].message.content
        message_history.append({"role": "assistant", "content": answer})
        # 질문 + 답변 함께 에코
        await inter.followup.send(f"**{inter.user.display_name}**: {q}") 
        await inter.followup.send(f"{answer}")
    except Exception as e:
        await inter.followup.send("죄송해요~ 별빛이 잠시 흐트러졌나 봐요… 다시 시도해 주세요.")
        print(f"⚠️ OpenAI 오류: {e}")

# ────────── 슬래시 기반 모델 전환 ────────── #
@tree.command(name="deep", description="더 깊은 별빛 모델(gpt-4.1-mini)로 전환")
async def deep(inter: discord.Interaction):
    global current_model
    current_model = DEEP_MODEL
    await inter.response.send_message("🌌 지금부터 더 깊은 별빛으로 대화할게요~")

@tree.command(name="light", description="가벼운 모델(gpt-4o-mini)로 전환")
async def light(inter: discord.Interaction):
    global current_model
    current_model = DEFAULT_MODEL
    await inter.response.send_message("✨ 다시 가벼운 별바람으로 돌아왔어요~")

# ────────── 텍스트 명령(!하이) 유지 ────────── #
"""
@bot.event
async def on_message(m: discord.Message):
    if m.author == bot.user: return
    if m.content.startswith("!하이"):
        await m.channel.send(f"{USER_ALIAS}, 안녕하세요~ 정원에서 기다리고 있었답니다🌼")
"""
# ────────── 준비 & 실행 ────────── #
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Hyacine 챗봇(슬래시 버전) 로그인 완료: {bot.user}")

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
