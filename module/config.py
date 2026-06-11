# Configuration Module
import os
import json
import pathlib
from dotenv import load_dotenv

# 1. Load Environment Variables
# Assuming the bot is run from the root directory
SETTINGS_DIR = pathlib.Path("settings")
# 로컬 기본값은 data/, 배포 환경에서는 DATA_DIR 환경 변수로 덮어쓸 수 있음 (예: /app/data)
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "data"))

# DB 백엔드 선택: 기본은 sqlite.
# 외부 DB(MySQL, Oracle 등)를 붙이려면 module/database.py에 Repository 구현을 추가하고
# 이 값을 해당 백엔드 이름으로 설정한다. 접속 정보는 DB_URL 환경 변수로 전달한다.
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()
DB_URL = os.getenv("DB_URL")  # 예: mysql://user:pass@host:3306/botdb (sqlite에서는 사용 안 함)

load_dotenv(dotenv_path=SETTINGS_DIR / "DISCORD_TOKEN.env")
load_dotenv(dotenv_path=SETTINGS_DIR / "OPENAI_API_KEY.env")
load_dotenv(dotenv_path=SETTINGS_DIR / "GOOGLE_API_KEY.env")

# 2. API Keys
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# 3. Channel IDs
RECRUIT_CHANNEL_ID = 1
EVENT_CHANNEL_ID = 1

# 4. Game Settings
GAMES = {
    "League of Legends": {
        "max_players": 5,
        "roles": ["탑", "정글", "미드", "원딜", "서포터"]
    },
    "PUBG": {
        "max_players": 4,
        "roles": []
    },
    "Overwatch": {
        "max_players": 5,
        "roles": ["딜러1", "딜러2", "탱커", "힐러1", "힐러2"]
    }
}

# 5. Forbidden Words
FORBIDDEN_WORDS_FILE = SETTINGS_DIR / "forbidden_words.json"

def load_forbidden_words():
    if not FORBIDDEN_WORDS_FILE.exists():
        return []
    try:
        with FORBIDDEN_WORDS_FILE.open(encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, list):
            return [str(w).lower() for w in data if w]
        return []
    except Exception:
        return []
