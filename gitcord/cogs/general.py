"""상태 확인과 도움말."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import __version__
from ..bot import Gitcord
from ..categories import CATEGORIES


class General(commands.Cog):
    def __init__(self, bot: Gitcord) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="봇이 살아있는지 확인합니다.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"pong! `{latency}ms`", ephemeral=True)

    @app_commands.command(name="gitcord", description="Gitcord 사용법과 현재 상태입니다.")
    async def gitcord(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"Gitcord v{__version__}",
            description=(
                "GitHub 저장소를 추적해 커밋·PR·이슈·CI 결과를 이 서버로 보냅니다.\n"
                "설정은 **서버 관리** 권한이 있어야 바꿀 수 있습니다."
            ),
            color=0x0969DA,
        )
        embed.add_field(
            name="시작하기",
            value=(
                "```\n"
                "/watch add repo:owner/name\n"
                "```\n"
                "명령을 실행한 채널로 알림이 갑니다. `channel:` 로 다른 채널을 "
                "지정할 수도 있습니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="명령어",
            value=(
                "`/watch add` — 저장소 구독 추가\n"
                "`/watch remove` — 구독 해제\n"
                "`/watch list` — 이 서버의 구독 목록\n"
                "`/watch events` — 저장소별로 받을 알림 종류 변경\n"
                "`/repo` — 저장소 정보 조회"
            ),
            inline=False,
        )
        embed.add_field(
            name="알림 종류",
            value=" · ".join(f"`{key}`({label})" for key, label in CATEGORIES.items()),
            inline=False,
        )

        watches = (
            await self.bot.db.list_watches(interaction.guild_id)
            if interaction.guild_id
            else []
        )
        token_state = "있음" if self.bot.github.authenticated else "없음 (시간당 60회 제한)"
        embed.add_field(
            name="상태",
            value=(
                f"이 서버 구독 {len(watches)}건 · "
                f"폴링 주기 {self.bot.config.poll_interval}초 · "
                f"GitHub 토큰 {token_state}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Gitcord) -> None:
    await bot.add_cog(General(bot))
