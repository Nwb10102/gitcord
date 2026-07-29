"""구독 설정 UI.

`/watch list` 가 여는 화면이다. 저장소를 드롭다운에서 고르면 그 저장소의 알림
종류를 체크박스처럼 켜고 끌 수 있다. 카테고리를 문자열로 입력하는
`/watch events` 도 그대로 남겨뒀다 — 자동화나 빠른 입력에는 그쪽이 편하다.

화면은 하나이고 상태에 따라 다시 그린다. 컴포넌트를 데코레이터로 고정하지 않고
`rebuild()` 에서 매번 새로 만드는 이유는, 선택지(구독 목록·현재 켜진 카테고리)가
DB 값에 따라 달라지기 때문이다.
"""

from __future__ import annotations

import discord

from .categories import CATEGORIES
from .db import Watch

# 디스코드 셀렉트 메뉴의 선택지 상한
MAX_OPTIONS = 25

ON, OFF = "🟩", "⬛"


def category_lines(categories: tuple[str, ...] | list[str]) -> str:
    """켜짐/꺼짐을 한눈에 보여주는 체크리스트.

    설정 화면과 `/watch add`·`/watch events` 응답이 같은 모양을 쓰도록 공유한다.
    """
    return "\n".join(
        f"{ON if key in categories else OFF} **{label}**  `{key}`"
        for key, label in CATEGORIES.items()
    )


class GuildView(discord.ui.View):
    """길드 안에서 도는 메뉴의 공통 부분.

    - 메뉴를 연 사람만 조작할 수 있다 (다른 사람이 남의 메뉴를 건드리지 못하게).
    - 설정을 바꾸는 조작에는 서버 관리 권한을 따로 확인한다.
    - 만료되면 컴포넌트를 비활성화한다. 안 그러면 시간 지난 메뉴를 눌렀을 때
      "응답 없음"으로 보인다.
    """

    def __init__(self, bot, *, guild_id: int, author_id: int) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id
        self._interaction: discord.Interaction | None = None

    def channel_label(self, channel_id: int) -> str:
        """셀렉트 옵션의 description 에는 멘션이 렌더링되지 않아 이름을 직접 찾는다."""
        channel = self.bot.get_channel(channel_id)
        return f"#{channel.name}" if channel is not None else f"채널 {channel_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "이 메뉴는 명령을 실행한 사람만 쓸 수 있습니다. 직접 실행해주세요.",
                ephemeral=True,
            )
            return False
        return True

    async def ensure_can_edit(self, interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not permissions.manage_guild:
            await interaction.response.send_message(
                "설정을 바꾸려면 **서버 관리** 권한이 필요합니다.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self._interaction is None:
            return
        try:
            await self._interaction.edit_original_response(
                content="-# 메뉴가 만료됐습니다. 명령을 다시 실행해주세요.", view=self
            )
        except discord.HTTPException:
            pass


class RepoSelect(discord.ui.Select["SettingsView"]):
    def __init__(self, watches: list[Watch], selected_id: int | None) -> None:
        options = [
            discord.SelectOption(
                label=watch.repo[:100],
                value=str(watch.id),
                description=f"알림 {len(watch.categories)}종",
                default=watch.id == selected_id,
            )
            for watch in watches[:MAX_OPTIONS]
        ]
        super().__init__(placeholder="저장소를 고르세요", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view.selected_id = int(self.values[0])
        view.confirm_remove = False
        await view.rerender(interaction)


class CategorySelect(discord.ui.Select["SettingsView"]):
    """알림 종류 다중 선택.

    min_values=0 이라 전부 해제할 수도 있다. 그 상태는 "구독은 남기되 알림은
    끔"이라는 뜻이라 의도적으로 허용한다.
    """

    def __init__(self, watch: Watch) -> None:
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=f"`{key}`",
                default=key in watch.categories,
            )
            for key, label in CATEGORIES.items()
        ]
        super().__init__(
            placeholder="알림 종류를 켜고 끄세요 (여러 개 선택 가능)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        if not await view.ensure_can_edit(interaction):
            return
        # CATEGORIES 순서로 정렬해 저장한다. 사용자가 고른 순서는 의미가 없다.
        chosen = tuple(name for name in CATEGORIES if name in self.values)
        await view.bot.db.set_categories_by_id(view.selected_id, chosen)
        view.confirm_remove = False
        await view.rerender(interaction)


class _StateButton(discord.ui.Button["SettingsView"]):
    """전체 켜기 / 전체 끄기."""

    def __init__(self, *, label: str, categories: tuple[str, ...], emoji: str) -> None:
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=2)
        self._categories = categories

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        if not await view.ensure_can_edit(interaction):
            return
        await view.bot.db.set_categories_by_id(view.selected_id, self._categories)
        view.confirm_remove = False
        await view.rerender(interaction)


class RemoveButton(discord.ui.Button["SettingsView"]):
    """구독 해제. 실수 방지를 위해 두 번 눌러야 한다."""

    def __init__(self, *, confirming: bool) -> None:
        super().__init__(
            label="정말 해제할까요? (한 번 더)" if confirming else "구독 해제",
            emoji="⚠️" if confirming else "🗑️",
            style=discord.ButtonStyle.danger,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        if not await view.ensure_can_edit(interaction):
            return

        if not view.confirm_remove:
            view.confirm_remove = True
            await view.rerender(interaction)
            return

        watch = view.selected
        await view.bot.db.remove_watch_by_id(view.selected_id)
        await view.bot.db.prune_cursors()
        view.selected_id = None
        view.confirm_remove = False
        await view.rerender(
            interaction, notice=f"`{watch.repo}` 구독을 해제했습니다." if watch else None
        )


class BackButton(discord.ui.Button["SettingsView"]):
    def __init__(self) -> None:
        super().__init__(label="목록으로", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view.selected_id = None
        view.confirm_remove = False
        await view.rerender(interaction)


class SettingsView(GuildView):
    def __init__(self, bot, *, guild_id: int, author_id: int) -> None:
        super().__init__(bot, guild_id=guild_id, author_id=author_id)
        self.watches: list[Watch] = []
        self.selected_id: int | None = None
        self.confirm_remove = False
        self.notice: str | None = None

    # ── 상태 ────────────────────────────────────────────────

    @property
    def selected(self) -> Watch | None:
        return next((w for w in self.watches if w.id == self.selected_id), None)

    async def load(self) -> None:
        self.watches = await self.bot.db.list_watches(self.guild_id)
        if self.selected_id is not None and self.selected is None:
            self.selected_id = None
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        if not self.watches:
            return

        self.add_item(RepoSelect(self.watches, self.selected_id))
        watch = self.selected
        if watch is None:
            return

        self.add_item(CategorySelect(watch))
        self.add_item(BackButton())
        self.add_item(
            _StateButton(label="전체 켜기", categories=tuple(CATEGORIES), emoji="✅")
        )
        self.add_item(_StateButton(label="전체 끄기", categories=(), emoji="🚫"))
        self.add_item(RemoveButton(confirming=self.confirm_remove))

    # ── 렌더 ────────────────────────────────────────────────

    def embed(self) -> discord.Embed:
        if not self.watches:
            return discord.Embed(
                title="구독 중인 저장소가 없습니다",
                description=(
                    "`/watch add repo:owner/name` 으로 시작하세요.\n"
                    "추가하면 여기서 알림 종류를 켜고 끌 수 있습니다."
                ),
                color=0x6E7781,
            )

        watch = self.selected
        if watch is None:
            embed = discord.Embed(
                title="구독 설정",
                description="아래에서 저장소를 고르면 알림 종류를 켜고 끌 수 있습니다.",
                color=0x0969DA,
            )
            for item in self.watches[:MAX_OPTIONS]:
                state = "알림 꺼짐" if not item.categories else " · ".join(
                    CATEGORIES[c] for c in item.categories
                )
                embed.add_field(
                    name=item.repo,
                    value=f"<#{item.channel_id}>\n{state}",
                    inline=False,
                )
            if len(self.watches) > MAX_OPTIONS:
                embed.set_footer(
                    text=f"구독 {len(self.watches)}건 중 {MAX_OPTIONS}건만 표시됩니다"
                )
            return embed

        embed = discord.Embed(
            title=watch.repo,
            url=f"https://github.com/{watch.repo}",
            description=(
                f"알림 채널 <#{watch.channel_id}>\n\n"
                + category_lines(watch.categories)
            ),
            color=0x0969DA if watch.categories else 0x6E7781,
        )
        if not watch.categories:
            embed.set_footer(text="알림이 모두 꺼져 있습니다. 구독은 유지됩니다.")
        return embed

    async def rerender(
        self, interaction: discord.Interaction, *, notice: str | None = None
    ) -> None:
        self.notice = notice
        await self.load()
        content = f"-# {notice}" if notice else None
        await interaction.response.edit_message(
            content=content, embed=self.embed(), view=self
        )

    async def send(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await self.load()
        await interaction.response.send_message(
            embed=self.embed(), view=self, ephemeral=True
        )

    async def ensure_can_edit(self, interaction: discord.Interaction) -> bool:
        if not await super().ensure_can_edit(interaction):
            return False
        if self.selected_id is None:
            await interaction.response.send_message(
                "저장소를 먼저 고르세요.", ephemeral=True
            )
            return False
        return True


# ── 구독 해제 화면 ──────────────────────────────────────────


class RemoveSelect(discord.ui.Select["RemoveView"]):
    def __init__(self, view: RemoveView) -> None:
        options = [
            discord.SelectOption(
                label=watch.repo[:100],
                value=str(watch.id),
                description=(
                    f"{view.channel_label(watch.channel_id)} · "
                    f"{'알림 꺼짐' if not watch.categories else f'알림 {len(watch.categories)}종'}"
                )[:100],
                default=watch.id in view.chosen,
            )
            for watch in view.watches[:MAX_OPTIONS]
        ]
        super().__init__(
            placeholder="해제할 저장소를 고르세요 (여러 개 선택 가능)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view.chosen = {int(v) for v in self.values}
        await view.rerender(interaction)


class ConfirmRemoveButton(discord.ui.Button["RemoveView"]):
    """선택한 항목을 실제로 지운다.

    셀렉트에서 고르는 것만으로 바로 지우지 않는 이유는, 구독을 지우면 폴링 커서도
    함께 사라져서 다시 추가할 때 기준점을 새로 잡느라 그사이 활동을 놓치기 때문이다.
    되돌리기가 완전히 공짜는 아니라서 버튼 한 번을 확인 절차로 뒀다.
    """

    def __init__(self, count: int) -> None:
        super().__init__(
            label=f"선택한 {count}건 해제" if count else "해제할 저장소를 먼저 고르세요",
            emoji="🗑️" if count else None,
            style=discord.ButtonStyle.danger if count else discord.ButtonStyle.secondary,
            disabled=count == 0,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        if not await view.ensure_can_edit(interaction):
            return

        removed = [w for w in view.watches if w.id in view.chosen]
        for watch in removed:
            await view.bot.db.remove_watch_by_id(watch.id)
        await view.bot.db.prune_cursors()
        view.chosen.clear()
        await view.rerender(interaction, removed=removed)


class RemoveView(GuildView):
    def __init__(self, bot, *, guild_id: int, author_id: int) -> None:
        super().__init__(bot, guild_id=guild_id, author_id=author_id)
        self.watches: list[Watch] = []
        self.chosen: set[int] = set()
        self.removed: list[Watch] = []

    async def load(self) -> None:
        self.watches = await self.bot.db.list_watches(self.guild_id)
        # 이미 사라진 구독이 선택 상태로 남지 않게 정리한다.
        self.chosen &= {w.id for w in self.watches}
        self.clear_items()
        if self.watches:
            self.add_item(RemoveSelect(self))
            self.add_item(ConfirmRemoveButton(len(self.chosen)))

    def embed(self) -> discord.Embed:
        if not self.watches:
            title = "구독이 모두 해제됐습니다" if self.removed else "구독 중인 저장소가 없습니다"
            embed = discord.Embed(
                title=title,
                description="`/watch add repo:owner/name` 으로 다시 추가할 수 있습니다.",
                color=0x6E7781,
            )
        else:
            embed = discord.Embed(
                title="구독 해제",
                description="해제할 저장소를 고른 뒤 아래 버튼을 누르세요.",
                color=0xCF222E if self.chosen else 0x0969DA,
            )
            for watch in self.watches[:MAX_OPTIONS]:
                mark = "🗑️ " if watch.id in self.chosen else ""
                state = "알림 꺼짐" if not watch.categories else " · ".join(
                    CATEGORIES[c] for c in watch.categories
                )
                embed.add_field(
                    name=f"{mark}{watch.repo}",
                    value=f"<#{watch.channel_id}>\n{state}",
                    inline=False,
                )
            if len(self.watches) > MAX_OPTIONS:
                embed.set_footer(
                    text=f"구독 {len(self.watches)}건 중 {MAX_OPTIONS}건만 표시됩니다"
                )

        if self.removed:
            embed.add_field(
                name=f"방금 해제함 ({len(self.removed)}건)",
                value="\n".join(f"`{w.repo}` — <#{w.channel_id}>" for w in self.removed),
                inline=False,
            )
        return embed

    async def rerender(
        self, interaction: discord.Interaction, *, removed: list[Watch] | None = None
    ) -> None:
        self.removed = removed or []
        await self.load()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def send(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await self.load()
        await interaction.response.send_message(
            embed=self.embed(), view=self, ephemeral=True
        )
