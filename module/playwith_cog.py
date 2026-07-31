import discord
import time
from typing import Optional
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Select, Button
from module.config import DISCORD_GUILD_ID, RECRUIT_CHANNEL_ID, GAMES
from module.database import PartyRepository, create_party_repository

class PlayWithCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repository: Optional[PartyRepository] = None):
        self.bot = bot
        # DB 접근은 Repository에 위임 (기본: DB_BACKEND 환경 변수에 따라 생성, 테스트 시 주입 가능)
        self.db = repository or create_party_repository()
        self.cleanup_parties.start()
        self.shared_views = {}
        for game in GAMES:
            view = View(timeout=None)
            view.add_item(JoinButton(self, game))
            self.shared_views[game] = view
            if bot is not None:
                bot.add_view(view)

    def cog_unload(self):
        self.cleanup_parties.cancel()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """서버를 나간 멤버가 파티 자리와 역할을 영구 점유하지 않게 한다."""
        if member.guild.id != DISCORD_GUILD_ID:
            return
        game = self.get_user_party(member.id)
        if not game:
            return
        self.remove_participant(game, member.id)
        if not self.get_participants(game):
            self.delete_party(game)

    @tasks.loop(minutes=10)
    async def cleanup_parties(self):
        # Delete parties older than 24 hours
        expiration_time = int(time.time()) - 24 * 60 * 60
        expired_games = self.db.delete_expired_parties(expiration_time)
        if expired_games:
            print(f"Deleted expired parties: {expired_games}")

    # ── Repository 위임 (UI 컴포넌트에서 사용) ── #

    def get_party(self, game):
        return self.db.get_party(game)

    def create_party(self, game):
        return self.db.create_party(game, int(time.time()))

    def delete_party(self, game):
        self.db.delete_party(game)

    def get_participants(self, game):
        return self.db.get_participants(game)

    def add_participant(self, game, user_id, role=None):
        # 정원은 DB가 최종 판정한다. 파이썬 검사는 친절한 메시지를 위해서만 남긴다.
        return self.db.add_participant(
            game, user_id, role, max_players=GAMES[game]["max_players"]
        )

    def remove_participant(self, game, user_id):
        self.db.remove_participant(game, user_id)

    def get_user_party(self, user_id):
        return self.db.get_user_party(user_id)

    def ensure_recruit_channel(self, interaction):
        return interaction.channel_id == RECRUIT_CHANNEL_ID

    @app_commands.command(name="모집", description="게임별 파티 모집 메시지를 생성합니다.")
    async def 모집(self, interaction: discord.Interaction):
        if not self.ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        # Find games that don't have an active party
        available_games = []
        for game in GAMES:
            if not self.get_party(game):
                available_games.append(game)

        embed = discord.Embed(
            title="🎮 파티 생성",
            description="게임을 선택하여 파티를 생성합니다.\n이미 생성된 파티는 다시 만들 수 없습니다.",
            color=discord.Color.blue()
        )

        if not available_games:
            embed.description = "⚠️ 모든 게임에 대해 파티가 이미 생성되어 있습니다."
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = View()
        view.add_item(GameSelect(self, available_games))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="나가기", description="현재 참가 중인 파티에서 나갑니다.")
    async def 나가기(self, interaction: discord.Interaction):
        if not self.ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return
        
        user_id = interaction.user.id
        game = self.get_user_party(user_id)

        if not game:
            await interaction.response.send_message("❌ 참가 중인 파티가 없습니다.", ephemeral=True)
            return

        self.remove_participant(game, user_id)
        
        participants = self.get_participants(game)
        if not participants:
            self.delete_party(game)
            await interaction.response.send_message(
                f"👋 {interaction.user.mention} 님이 `{game}` 파티에서 나갔습니다.\n💨 `{game}` 파티가 해산되었습니다.")
        else:
            await interaction.response.send_message(f"👋 {interaction.user.mention} 님이 `{game}` 파티에서 나갔습니다.")

    @app_commands.command(name="파티", description="현재 모집 중인 파티 현황 확인")
    async def 파티(self, interaction: discord.Interaction):
        if not self.ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        embeds = []

        for game, info in GAMES.items():
            if not self.get_party(game):
                continue
            
            participants = self.get_participants(game)

            role_members = {}
            player_lines = []

            for uid, role in participants.items():
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
                description=f"현재 인원: {len(player_lines)} / {info['max_players']}",
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

        if embeds:
            await interaction.response.send_message(embeds=embeds)
        else:
            await interaction.response.send_message("📭 현재 모집 중인 파티가 없습니다.")

    @app_commands.command(name="변경", description="현재 참가 중인 파티에서 역할을 변경합니다.")
    async def 변경(self, interaction: discord.Interaction):
        if not self.ensure_recruit_channel(interaction):
            await interaction.response.send_message("❌ 이 명령어는 모집 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return

        user_id = interaction.user.id
        game = self.get_user_party(user_id)
        
        if not game:
            await interaction.response.send_message("❌ 현재 참가 중인 파티가 없습니다.", ephemeral=True)
            return

        roles = GAMES[game]["roles"]
        if not roles:
            await interaction.response.send_message(f"⚠️ `{game}` 파티에는 역할 개념이 없습니다.", ephemeral=True)
            return

        view = View()
        view.add_item(RoleUpdateSelect(self, game, user_id))
        await interaction.response.send_message(f"🎯 `{game}` 파티에서 변경할 역할을 선택하세요:", view=view, ephemeral=True)

# UI Components
class GameSelect(Select):
    def __init__(self, cog, game_options):
        self.cog = cog
        options = [
            discord.SelectOption(label=game, description=f"{game} 파티 모집", value=game)
            for game in game_options
        ]
        super().__init__(placeholder="게임을 선택하세요", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        selected_game = self.values[0]
        # 드롭다운 선택 사이에 사용자 대기 시간이 있어 두 사람이 같은 게임을 고를 수 있다.
        if not self.cog.create_party(selected_game):
            await interaction.response.send_message(
                f"⚠️ `{selected_game}` 파티는 이미 생성되어 있습니다.", ephemeral=True
            )
            return
        await send_party_embed(self.cog, interaction, selected_game)

async def send_party_embed(cog, interaction, game):
    info = GAMES[game]
    participants = cog.get_participants(game)
    
    embed = discord.Embed(
        title=f"{game} 파티 모집",
        description=f"최대 인원: {info['max_players']}명\n현재 참가자: {len(participants)}명",
        color=discord.Color.green()
    )
    if info["roles"]:
        embed.add_field(name="역할 목록", value=", ".join(info["roles"]), inline=False)

    view = cog.shared_views[game]

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view)

class JoinButton(Button):
    def __init__(self, cog, game):
        super().__init__(
            label="참가하기",
            style=discord.ButtonStyle.primary,
            custom_id=f"party:join:{game}",
        )
        self.cog = cog
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        game = self.game
        user_id = interaction.user.id

        # 영속 버튼은 custom_id만으로 동작하므로 위치 무관하게 응답할 수 있다.
        if interaction.guild_id != DISCORD_GUILD_ID:
            await interaction.response.send_message(
                "❌ 이 버튼은 이 서버에서 사용할 수 없습니다.", ephemeral=True
            )
            return

        if not self.cog.get_party(game):
            await interaction.response.send_message("❌ 모집이 종료된 파티입니다.", ephemeral=True)
            return

        current_party = self.cog.get_user_party(user_id)

        if current_party:
            await interaction.response.send_message("⚠️ 이미 다른 파티에 참가 중입니다. 먼저 나가주세요.", ephemeral=True)
            return

        participants = self.cog.get_participants(game)
        if len(participants) >= GAMES[game]["max_players"]:
            await interaction.response.send_message("⚠️ 파티가 가득 찼습니다.", ephemeral=True)
            return

        if GAMES[game]["roles"]:
            view = View()
            view.add_item(RoleSelect(self.cog, game, GAMES[game]["roles"]))
            await interaction.response.send_message("🎯 역할을 선택하세요:", view=view, ephemeral=True)
        else:
            if not self.cog.add_participant(game, user_id):
                await interaction.response.send_message(
                    "❌ 참가하지 못했어요. 파티가 종료되었거나 자리가 찼습니다.", ephemeral=True
                )
                return
            await interaction.response.send_message(f"✅ {interaction.user.mention} 님이 `{game}` 파티에 참가했습니다!", ephemeral=False)

class RoleSelect(Select):
    def __init__(self, cog, game, roles):
        options = [discord.SelectOption(label=role, value=role) for role in roles]
        super().__init__(placeholder="역할을 선택하세요", options=options, min_values=1, max_values=1)
        self.cog = cog
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        game = self.game
        role = self.values[0].strip().lower()
        user_id = interaction.user.id

        if not self.cog.get_party(game):
            await interaction.response.send_message("❌ 모집이 종료된 파티입니다.", ephemeral=True)
            return

        participants = self.cog.get_participants(game)
        if len(participants) >= GAMES[game]["max_players"]:
            await interaction.response.send_message("⚠️ 파티가 이미 가득 찼어요!", ephemeral=True)
            return

        current_party = self.cog.get_user_party(user_id)
        if current_party:
            await interaction.response.send_message("⚠️ 이미 다른 파티에 참가 중입니다. 먼저 나가주세요.", ephemeral=True)
            return

        # Check if role is taken
        for uid, assigned_role in participants.items():
            if assigned_role == role:
                await interaction.response.send_message(f"⚠️ `{role}` 역할은 이미 다른 참가자가 선택했습니다.", ephemeral=True)
                return

        if not self.cog.add_participant(game, user_id, role):
            await interaction.response.send_message(
                "❌ 참가하지 못했어요. 파티가 종료되었거나 자리·역할이 이미 찼습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"✅ {interaction.user.mention} 님이 `{game}` 파티에 역할 `{role}`로 참가했어요!", ephemeral=False)

class RoleUpdateSelect(Select):
    def __init__(self, cog, game, user_id):
        options = [discord.SelectOption(label=role, value=role) for role in GAMES[game]["roles"]]
        super().__init__(placeholder="새 역할을 선택하세요", options=options, min_values=1, max_values=1)
        self.cog = cog
        self.game = game
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 상호작용 불가", ephemeral=True)
            return

        role = self.values[0].strip().lower()
        game = self.game

        if not self.cog.get_party(game):
            await interaction.response.send_message("❌ 모집이 종료된 파티입니다.", ephemeral=True)
            return

        participants = self.cog.get_participants(game)
        for uid, assigned_role in participants.items():
            if uid != self.user_id and assigned_role == role:
                await interaction.response.send_message(f"⚠️ `{role}` 역할은 이미 다른 참가자가 선택했습니다.", ephemeral=True)
                return

        if not self.cog.add_participant(game, self.user_id, role):
            await interaction.response.send_message(
                "❌ 역할을 바꾸지 못했어요. 파티가 종료되었거나 역할이 이미 찼습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"🔄 역할이 `{role}`(으)로 변경되었습니다!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(PlayWithCog(bot))
