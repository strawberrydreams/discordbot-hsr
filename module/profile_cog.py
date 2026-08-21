"""게임 프로필 UID 등록.

UID 매핑은 길드별로 나뉜다. 같은 사람이 서버마다 다른 계정을 쓸 수 있고,
어느 서버에 무엇을 등록했는지가 다른 서버로 새면 안 된다.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from module.database import ProfileRepository, create_profile_repository, run_db
from module.game_profile import ADAPTERS, GameAdapter, ProfileLookupError, ProfileService, Showcase

GAME_CHOICES = [
    app_commands.Choice(name=adapter.label, value=adapter.key)
    for adapter in ADAPTERS.values()
]


class ProfileCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        repository: Optional[ProfileRepository] = None,
        service: Optional[ProfileService] = None,
    ):
        self.bot = bot
        self.db = repository or create_profile_repository()
        # 네트워크는 명령이 처음 불릴 때 열린다. 키·회선 없이도 봇은 기동해야 한다.
        self.service = service or ProfileService()

    async def cog_unload(self) -> None:
        await self.service.close()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """봇이 서버에서 제거되면 그 서버의 UID 등록을 남기지 않는다."""
        await run_db(self.db.delete_guild, guild.id)

    @app_commands.command(name="등록", description="게임 계정 UID를 이 서버에 등록합니다.")
    @app_commands.describe(게임="UID를 등록할 게임", uid="게임 안에서 확인할 수 있는 숫자 UID")
    @app_commands.choices(게임=GAME_CHOICES)
    @app_commands.guild_only()
    async def _register(
        self, inter: discord.Interaction, 게임: app_commands.Choice[str], uid: str
    ):
        # Enka 조회가 3초를 넘길 수 있다. 먼저 ACK한다.
        await inter.response.defer(ephemeral=True)
        adapter = self.service.adapter(게임.value)
        try:
            cleaned = adapter.validate_uid(uid)
            showcase = await self.service.fetch(adapter.key, cleaned)
        except ProfileLookupError as exc:
            await inter.followup.send(f"❌ {exc}", ephemeral=True)
            return

        await run_db(
            self.db.set_uid, inter.guild_id, inter.user.id, adapter.key, cleaned
        )
        message = (
            f"✅ {adapter.label} UID `{cleaned}` 을(를) 이 서버에 등록했습니다. "
            f"({showcase.nickname})"
        )
        if not showcase.characters:
            message += f"\n\nℹ️ 진열장이 비어 있습니다. {adapter.showcase_help}"
        await inter.followup.send(message, ephemeral=True)

    @app_commands.command(name="게임카드", description="등록한 게임 계정의 첫 진열 캐릭터를 봅니다.")
    @app_commands.describe(게임="카드를 볼 게임")
    @app_commands.choices(게임=GAME_CHOICES)
    @app_commands.guild_only()
    async def _card(
        self, inter: discord.Interaction, 게임: app_commands.Choice[str]
    ):
        await inter.response.defer()
        adapter = self.service.adapter(게임.value)
        uid = await run_db(
            self.db.get_uid, inter.guild_id, inter.user.id, adapter.key
        )
        if uid is None:
            await inter.followup.send(
                f"ℹ️ 이 서버에 등록된 {adapter.label} UID가 없습니다. "
                f"`/등록 게임:{adapter.label}` 으로 먼저 등록해 주세요.",
                ephemeral=True,
            )
            return

        try:
            showcase = await self.service.fetch(adapter.key, uid)
        except ProfileLookupError as exc:
            await inter.followup.send(f"❌ {exc}", ephemeral=True)
            return

        await inter.followup.send(embed=self._build_embed(adapter, showcase))

    @staticmethod
    def _build_embed(adapter: GameAdapter, showcase: Showcase) -> discord.Embed:
        embed = discord.Embed(
            title=f"{showcase.nickname} · {adapter.label}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="계정 레벨", value=str(showcase.level), inline=True)

        if not showcase.characters:
            # UID가 멀쩡해도 진열장이 비어 있을 수 있다. Enka는 인게임에서 올린
            # 캐릭터만 돌려주므로 "데이터 없음"이 아니라 올리는 방법을 알려야 한다.
            embed.description = f"진열장이 비어 있습니다.\n\n{adapter.showcase_help}"
            return embed

        character = showcase.characters[0]
        embed.description = f"**{character.name}** Lv.{character.level}"
        embed.set_image(url=character.art_url)
        return embed

    @app_commands.command(name="등록해제", description="이 서버에 등록한 게임 UID를 지웁니다.")
    @app_commands.describe(게임="등록을 지울 게임")
    @app_commands.choices(게임=GAME_CHOICES)
    @app_commands.guild_only()
    async def _unregister(
        self, inter: discord.Interaction, 게임: app_commands.Choice[str]
    ):
        adapter = self.service.adapter(게임.value)
        removed = await run_db(
            self.db.delete_uid, inter.guild_id, inter.user.id, adapter.key
        )
        text = (
            f"✅ {adapter.label} 등록을 지웠습니다."
            if removed
            else f"ℹ️ 이 서버에 등록된 {adapter.label} UID가 없습니다."
        )
        await inter.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileCog(bot))
