# Discord Bot / 모집 채널의 멤버 모집용 기능을 모아놓은 템플릿

# 이 파일에서는 slash 명령어를 사용함
# 모집 채널에서 (/모집), (/파티), (/변경), (/나가기)를 입력하면 각각의 기능을 실행함

import discord
import os
from discord.ui import View, Select, Button
from dotenv import load_dotenv

RECRUIT_CHANNEL_ID = 1339250284366659637

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

shared_views = {}
party_status = {game: {"players": {}} for game in GAMES}
user_parties = {}

# 클라이언트 및 명령 트리 초기화
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

tree = None
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self):
        await setup_party_commands(self)
        await self.tree.sync()

client = MyClient()

# 옳은 채널이 선택되었는지 체크
def ensure_recruit_channel(interaction):
    return interaction.channel_id == RECRUIT_CHANNEL_ID

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

# slash 명령어 등록
async def setup_party_commands(client: discord.Client):
    tree = client.tree

    @tree.command(name="모집", description="게임별 파티 모집 메시지를 생성합니다.")
    async def 모집(interaction: discord.Interaction):
        if not ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        available_games = [game for game, info in party_status.items() if not info["players"]]
        embed = discord.Embed(
            title="🎮 파티 생성",
            description="게임을 선택하여 파티를 생성합니다.\n이미 생성된 파티는 다시 만들 수 없습니다.",
            color=discord.Color.blue()
        )

        if not available_games:
            embed.description = "⚠️ 모든 게임에 대해 파티가 이미 생성되어 있습니다."
            await interaction.response.send_message(embed=embed)
            return

        view = View()
        view.add_item(GameSelect(interaction, available_games))
        await interaction.response.send_message(embed=embed, view=view)

    @tree.command(name="나가기", description="현재 참가 중인 파티에서 나갑니다.")
    async def 나가기(interaction: discord.Interaction):
        if not ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return
        
        user_id = interaction.user.id
        if user_id not in user_parties:
            await interaction.response.send_message("❌ 참가 중인 파티가 없습니다.", ephemeral=True)
            return

        game = user_parties[user_id]
        del party_status[game]["players"][user_id]
        del user_parties[user_id]

        if not party_status[game]["players"]:
            await interaction.response.send_message(
                f"👋 {interaction.user.mention} 님이 `{game}` 파티에서 나갔습니다.\n💨 `{game}` 파티가 해산되었습니다.")
        else:
            await interaction.response.send_message(f"👋 {interaction.user.mention} 님이 `{game}` 파티에서 나갔습니다.")

    @tree.command(name="파티", description="현재 모집 중인 파티 현황 확인")
    async def 파티(interaction: discord.Interaction):
        if not ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        has_party = False
        embeds = []

        for game, info in GAMES.items():
            players = party_status[game]["players"]
            if not players:
                continue

            has_party = True
            role_members = {}
            player_lines = []

            for uid, role in players.items():
                member = interaction.guild.get_member(uid)
                if member:
                    display_name = member.mention
                    role_lower = role.strip().lower() if role else None
                    if role_lower:
                        player_lines.append(f"- {display_name} ({role_lower})")
                        role_members.setdefault(role_lower, []).append(display_name)
                    else:
                        player_lines.append(f"- {display_name}")

            embed = discord.Embed(
                title=f"{game} 파티 현황",
                description=f"현재 인원: {len(players)} / {info['max_players']}",
                color=discord.Color.teal()
            )
            if player_lines:
                embed.add_field(name="👥 참가자", value="\n".join(player_lines), inline=False)

            if info["roles"]:
                role_lines = []
                for role in info["roles"]:
                    key = role.strip().lower()
                    members = role_members.get(key, [])
                    role_lines.append(f"{role}: {', '.join(members) if members else ''}")
                embed.add_field(name="🧙 역할 현황", value="\n".join(role_lines), inline=False)

            embeds.append(embed)

        if has_party:
            for embed in embeds:
                await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("📭 현재 모집 중인 파티가 없습니다.")

    @tree.command(name="변경", description="현재 참가 중인 파티에서 역할을 변경합니다.")
    async def 변경(interaction: discord.Interaction):
        if not ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in user_parties:
            await interaction.response.send_message("❌ 현재 참가 중인 파티가 없습니다.", ephemeral=True)
            return

        game = user_parties[user_id]
        roles = GAMES[game]["roles"]
        if not roles:
            await interaction.response.send_message(f"⚠️ `{game}` 파티에는 역할 개념이 없습니다.", ephemeral=True)
            return

        view = View()
        view.add_item(RoleUpdateSelect(game, user_id))
        await interaction.response.send_message(f"🎯 `{game}` 파티에서 변경할 역할을 선택하세요:", view=view, ephemeral=True)

# 인터랙티브 UI 구성 요소 설정
class GameSelect(Select):
    def __init__(self, interaction, game_options):
        self.interaction = interaction
        options = [
            discord.SelectOption(label=game, description=f"{game} 파티 모집", value=game)
            for game in game_options
        ]
        super().__init__(placeholder="게임을 선택하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        selected_game = self.values[0]
        await send_party_embed(interaction, selected_game)

async def send_party_embed(interaction, game):
    info = GAMES[game]
    embed = discord.Embed(
        title=f"{game} 파티 모집",
        description=f"최대 인원: {info['max_players']}명\n현재 참가자: {len(party_status[game]['players'])}명",
        color=discord.Color.green()
    )
    if info["roles"]:
        embed.add_field(name="역할 목록", value=", ".join(info["roles"]), inline=False)

    if game not in shared_views:
        view = View(timeout=600)
        view.add_item(JoinButton(game))
        shared_views[game] = view
    else:
        view = shared_views[game]

    await interaction.response.send_message(embed=embed, view=view)

class JoinButton(Button):
    def __init__(self, game):
        super().__init__(label="참가하기", style=discord.ButtonStyle.primary)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        game = self.game
        user_id = interaction.user.id

        if user_id in user_parties:
            await interaction.response.send_message("⚠️ 이미 다른 파티에 참가 중입니다. 먼저 나가주세요.", ephemeral=True)
            return

        if len(party_status[game]["players"]) >= GAMES[game]["max_players"]:
            await interaction.response.send_message("⚠️ 파티가 가득 찼습니다.", ephemeral=True)
            return

        if GAMES[game]["roles"]:
            view = View()
            view.add_item(RoleSelect(game, GAMES[game]["roles"]))
            await interaction.response.send_message("🎯 역할을 선택하세요:", view=view, ephemeral=True)
        else:
            party_status[game]["players"][user_id] = None
            user_parties[user_id] = game
            await interaction.response.send_message(f"✅ {interaction.user.mention} 님이 `{game}` 파티에 참가했습니다!", ephemeral=False)

class RoleSelect(Select):
    def __init__(self, game, roles):
        options = [discord.SelectOption(label=role, value=role) for role in roles]
        super().__init__(placeholder="역할을 선택하세요", options=options, min_values=1, max_values=1)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        game = self.game
        role = self.values[0].strip().lower()
        user_id = interaction.user.id

        if len(party_status[game]["players"]) >= GAMES[game]["max_players"]:
            await interaction.response.send_message("⚠️ 파티가 이미 가득 찼어요!", ephemeral=True)
            return

        if user_id in user_parties:
            await interaction.response.send_message("⚠️ 이미 다른 파티에 참가 중입니다. 먼저 나가주세요.", ephemeral=True)
            return

        for uid, assigned_role in party_status[game]["players"].items():
            if assigned_role == role:
                await interaction.response.send_message(f"⚠️ `{role}` 역할은 이미 다른 참가자가 선택했습니다.", ephemeral=True)
                return

        party_status[game]["players"][user_id] = role
        user_parties[user_id] = game
        await interaction.response.send_message(f"✅ {interaction.user.mention} 님이 `{game}` 파티에 역할 `{role}`로 참가했어요!", ephemeral=False)

class RoleUpdateSelect(Select):
    def __init__(self, game, user_id):
        options = [discord.SelectOption(label=role, value=role) for role in GAMES[game]["roles"]]
        super().__init__(placeholder="새 역할을 선택하세요", options=options, min_values=1, max_values=1)
        self.game = game
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 상호작용 불가", ephemeral=True)
            return

        role = self.values[0].strip().lower()
        game = self.game

        for uid, assigned_role in party_status[game]["players"].items():
            if uid != self.user_id and assigned_role == role:
                await interaction.response.send_message(f"⚠️ `{role}` 역할은 이미 다른 참가자가 선택했습니다.", ephemeral=True)
                return

        party_status[game]["players"][self.user_id] = role
        await interaction.response.send_message(f"🔄 역할이 `{role}`(으)로 변경되었습니다!", ephemeral=True)

# 1. .env 파일에서 토큰을 로드
load_dotenv(dotenv_path="DISCORD_TOKEN.env")
TOKEN = os.getenv("DISCORD_TOKEN")

client.run(TOKEN)

# 2. 토큰을 직접 입력 (실제 배포에서는 추천하지 않음)
# client.run("your_bot_token")
