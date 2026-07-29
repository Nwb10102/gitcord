"""구독 관리 커맨드.

`list` 는 누구나 볼 수 있고, 설정을 바꾸는 명령은 서버 관리 권한을 요구한다.
그래서 그룹 전체에 default_permissions 를 걸지 않고 개별 커맨드에 체크를 단다.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import categories as cat
from ..bot import Gitcord
from ..github import normalize_repo

# 알림을 보낼 수 있는 채널 종류
SendableChannel = discord.TextChannel | discord.Thread | discord.VoiceChannel


class Watch(commands.Cog):
    group = app_commands.Group(
        name="watch",
        description="GitHub 저장소 구독을 관리합니다.",
        guild_only=True,
    )

    def __init__(self, bot: Gitcord) -> None:
        self.bot = bot

    # ── 추가 ────────────────────────────────────────────────

    @group.command(name="add", description="저장소를 구독해 이 서버로 알림을 받습니다.")
    @app_commands.describe(
        repo="owner/name 또는 저장소 URL",
        channel="알림을 받을 채널 (생략하면 이 채널)",
        events=(
            "받을 알림 종류를 쉼표로. 생략하면 "
            + ",".join(cat.DEFAULT_CATEGORIES)
            + " / 전체는 all"
        ),
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(
        self,
        interaction: discord.Interaction,
        repo: str,
        channel: SendableChannel | None = None,
        events: str | None = None,
    ) -> None:
        full_name = normalize_repo(repo)
        selected = cat.parse(events) if events else cat.DEFAULT_CATEGORIES
        target = channel or interaction.channel

        if not isinstance(target, discord.abc.Messageable) or target.guild is None:
            raise ValueError("알림을 보낼 수 있는 채널을 지정해주세요.")

        # 권한을 미리 확인해야 구독해놓고 조용히 실패하는 일을 막을 수 있다.
        permissions = target.permissions_for(interaction.guild.me)
        missing = [
            name
            for name, ok in (
                ("메시지 보내기", permissions.send_messages),
                ("링크 첨부", permissions.embed_links),
            )
            if not ok
        ]
        if missing:
            raise ValueError(
                f"{target.mention} 채널에 대한 권한이 부족합니다: {', '.join(missing)}"
            )

        await interaction.response.defer(ephemeral=True)

        # 존재하지 않는 저장소를 구독해두면 폴링이 계속 404 를 맞는다. 먼저 확인.
        info = await self.bot.github.get_repo(full_name)

        added = await self.bot.db.add_watch(
            guild_id=interaction.guild_id,
            channel_id=target.id,
            repo=full_name,
            categories=selected,
        )
        if not added:
            await interaction.followup.send(
                f"`{full_name}` 은(는) 이미 {target.mention} 에서 구독 중입니다. "
                "알림 종류를 바꾸려면 `/watch events` 를 써주세요.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"**{info.get('full_name') or full_name}** 구독을 시작했습니다.\n"
            f"채널: {target.mention} · 알림: {cat.label(selected)}\n"
            f"-# 첫 폴링에서는 과거 이벤트를 보내지 않고 기준점만 잡습니다. "
            f"다음 활동부터 알림이 갑니다.",
            ephemeral=True,
        )

    # ── 해제 ────────────────────────────────────────────────

    @group.command(name="remove", description="저장소 구독을 해제합니다.")
    @app_commands.describe(
        repo="owner/name 또는 저장소 URL",
        channel="이 채널의 구독만 해제 (생략하면 서버 전체)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        repo: str,
        channel: SendableChannel | None = None,
    ) -> None:
        full_name = normalize_repo(repo)
        removed = await self.bot.db.remove_watch(
            guild_id=interaction.guild_id,
            repo=full_name,
            channel_id=channel.id if channel else None,
        )
        if removed == 0:
            await interaction.response.send_message(
                f"`{full_name}` 구독을 찾지 못했습니다. `/watch list` 로 확인해주세요.",
                ephemeral=True,
            )
            return

        await self.bot.db.prune_cursors()
        scope = channel.mention if channel else "이 서버"
        await interaction.response.send_message(
            f"`{full_name}` 구독 {removed}건을 {scope} 에서 해제했습니다.", ephemeral=True
        )

    # ── 목록 ────────────────────────────────────────────────

    @group.command(name="list", description="이 서버의 구독 목록을 봅니다.")
    async def list_(self, interaction: discord.Interaction) -> None:
        watches = await self.bot.db.list_watches(interaction.guild_id)
        if not watches:
            await interaction.response.send_message(
                "구독 중인 저장소가 없습니다. `/watch add repo:owner/name` 으로 시작하세요.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="구독 중인 저장소", color=0x0969DA)
        for watch in watches[:25]:  # 임베드 필드 상한
            embed.add_field(
                name=watch.repo,
                value=(
                    f"<#{watch.channel_id}>\n"
                    f"알림: {cat.label(watch.categories)}\n"
                    f"AI 요약: {'켜짐' if watch.ai_summary else '꺼짐'}"
                ),
                inline=False,
            )
        if len(watches) > 25:
            embed.set_footer(text=f"…외 {len(watches) - 25}건")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── 알림 종류 변경 ──────────────────────────────────────

    @group.command(name="events", description="저장소별로 받을 알림 종류를 바꿉니다.")
    @app_commands.describe(
        repo="owner/name 또는 저장소 URL",
        events="쉼표로 구분. 예) push,pr,ci · 전체는 all",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def events(
        self, interaction: discord.Interaction, repo: str, events: str
    ) -> None:
        full_name = normalize_repo(repo)
        try:
            selected = cat.parse(events)
        except cat.UnknownCategory as exc:
            raise ValueError(
                f"`{exc.name}` 은(는) 알 수 없는 알림 종류입니다.\n"
                f"사용 가능: {', '.join(f'`{k}`' for k in cat.CATEGORIES)} (전체는 `all`)"
            ) from None

        updated = await self.bot.db.set_categories(
            guild_id=interaction.guild_id, repo=full_name, categories=selected
        )
        if updated == 0:
            await interaction.response.send_message(
                f"`{full_name}` 구독을 찾지 못했습니다. 먼저 `/watch add` 로 추가해주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"`{full_name}` 의 알림을 **{cat.label(selected)}** 로 바꿨습니다. "
            f"({updated}개 채널)",
            ephemeral=True,
        )

    @add.autocomplete("events")
    @events.autocomplete("events")
    async def _events_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        presets = {
            ",".join(cat.DEFAULT_CATEGORIES): "기본 (푸시·PR·이슈·CI·릴리스)",
            "all": "전체",
            "push": "커밋 푸시만",
            "push,ci": "푸시 + CI 결과",
            "pr,issue": "PR + 이슈만",
        }
        return [
            app_commands.Choice(name=f"{label} — {value}", value=value)
            for value, label in presets.items()
            if current.lower() in value.lower()
        ][:25]


async def setup(bot: Gitcord) -> None:
    await bot.add_cog(Watch(bot))
