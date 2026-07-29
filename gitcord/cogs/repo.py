"""저장소 조회 커맨드.

기획대로 조회 기능은 최소한만 둔다 — 봇의 본체는 알림이고, 조회는 구독 전에
저장소를 확인하는 용도다.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import Gitcord
from ..embeds import parse_time, trim
from ..github import normalize_repo


class Repo(commands.Cog):
    def __init__(self, bot: Gitcord) -> None:
        self.bot = bot

    @app_commands.command(name="repo", description="GitHub 저장소 정보를 조회합니다.")
    @app_commands.describe(repo="owner/name 또는 저장소 URL")
    async def repo(self, interaction: discord.Interaction, repo: str) -> None:
        full_name = normalize_repo(repo)  # ValueError → 전역 핸들러가 안내
        await interaction.response.defer()

        data = await self.bot.github.get_repo(full_name)

        embed = discord.Embed(
            title=data.get("full_name") or full_name,
            url=data.get("html_url"),
            description=trim(data.get("description"), 400) or None,
            color=0x0969DA,
            timestamp=parse_time(data.get("pushed_at")),
        )
        owner = data.get("owner") or {}
        embed.set_thumbnail(url=owner.get("avatar_url"))

        embed.add_field(name="⭐ 스타", value=f"{data.get('stargazers_count', 0):,}", inline=True)
        embed.add_field(name="🍴 포크", value=f"{data.get('forks_count', 0):,}", inline=True)
        embed.add_field(
            name="🐛 열린 이슈", value=f"{data.get('open_issues_count', 0):,}", inline=True
        )

        language = data.get("language")
        if language:
            embed.add_field(name="언어", value=language, inline=True)
        embed.add_field(
            name="기본 브랜치", value=f"`{data.get('default_branch') or '?'}`", inline=True
        )
        embed.add_field(
            name="공개 여부", value="비공개" if data.get("private") else "공개", inline=True
        )

        topics = data.get("topics") or []
        if topics:
            embed.add_field(
                name="토픽", value=trim(", ".join(topics), 1024), inline=False
            )

        embed.set_footer(text="마지막 푸시")
        await interaction.followup.send(embed=embed)


async def setup(bot: Gitcord) -> None:
    await bot.add_cog(Repo(bot))
