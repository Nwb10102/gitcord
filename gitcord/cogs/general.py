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
        watches = (
            await self.bot.db.list_watches(interaction.guild_id)
            if interaction.guild_id
            else []
        )

        embed = discord.Embed(
            title="Gitcord",
            description=(
                "GitHub 저장소를 추적해 커밋 · PR · 이슈 · CI 결과를 이 서버로 보냅니다.\n"
                "웹훅 설정 없이, 저장소를 읽을 수만 있으면 추적할 수 있습니다."
            ),
            color=0x0969DA,
        )

        embed.add_field(
            name="시작하기",
            value=(
                "**1.** `/watch add repo:owner/name`\n"
                "**2.** `/watch list` 에서 받을 알림 종류를 켜고 끄기\n"
                "-# 명령을 실행한 채널로 알림이 갑니다. `channel:` 로 바꿀 수 있습니다."
            ),
            inline=False,
        )

        embed.add_field(
            name="구독 관리",
            value=(
                "`/watch list` — **구독 목록 · 알림 설정** (여기서 대부분 해결됩니다)\n"
                "`/watch add` — 저장소 추가\n"
                "`/watch remove` — 구독 해제\n"
                "`/watch events` — 알림 종류를 텍스트로 한 번에 지정"
            ),
            inline=False,
        )
        embed.add_field(
            name="그 외",
            value="`/repo` — 저장소 정보 조회\n`/ping` — 봇 응답 확인",
            inline=False,
        )

        embed.add_field(
            name="알림 종류",
            value="\n".join(f"`{key}` — {label}" for key, label in CATEGORIES.items()),
            inline=True,
        )

        token_state = (
            "연결됨 (5,000회/시간)"
            if self.bot.github.authenticated
            else "없음 — 60회/시간 제한"
        )
        embed.add_field(
            name="현재 상태",
            value=(
                f"구독 **{len(watches)}건**\n"
                f"폴링 주기 **{self.bot.config.poll_interval}초**\n"
                f"GitHub {token_state}"
            ),
            inline=True,
        )

        embed.set_footer(text=f"v{__version__} · 설정 변경은 서버 관리 권한이 필요합니다")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Gitcord) -> None:
    await bot.add_cog(General(bot))
