"""정적 인삿말.

`/인사`는 API를 호출하지 않는데도 hyacine_chat_cog에 있어 OPENAI_API_KEY가
없으면 함께 사라졌다. AI 키가 없는 운영자도 이 명령은 쓸 수 있어야 하므로
키 게이트 밖의 별도 확장으로 둔다.
"""

import discord
from discord import app_commands
from discord.ext import commands

from module.hyacine_chat_cog import load_persona


class GreetingCog(commands.Cog):
    @app_commands.command(name="인사", description="가벼운 인삿말")
    async def _hello(self, interaction: discord.Interaction):
        # persona.json은 웹 관리에서 바뀔 수 있다. 호출 때 읽으면 항상 최신이다.
        greeting = load_persona()["greeting"]
        await interaction.response.send_message(f"{interaction.user.mention}, {greeting}")


async def setup(bot: commands.Bot):
    await bot.add_cog(GreetingCog(bot))
