"""봇 본체.

Dishost 는 RAM 128MB 이므로 discord.py 의 캐시를 최대한 끈다. 슬래시 커맨드만
쓰기 때문에 멤버·메시지 캐시가 전혀 필요 없고, 이 둘이 상주 메모리의 대부분을
차지한다.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .db import Database
from .embeds import Post
from .github import GitHubClient, GitHubError, RateLimited

log = logging.getLogger("gitcord")

EXTENSIONS = (
    "gitcord.cogs.general",
    "gitcord.cogs.repo",
    "gitcord.cogs.watch",
    "gitcord.cogs.watcher",
)


class Gitcord(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True  # 슬래시 커맨드에 필요한 최소치

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # ── 메모리 절약 ──
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,  # 메시지 캐시 비활성화
        )
        self.config = config
        self.db = Database(config.db_path)
        self.github = GitHubClient(config.github_token)
        # on_ready 는 재접속 때마다 불리므로 정리는 한 번만 한다.
        self._cleaned_guild_commands = False

    # ── 수명주기 ────────────────────────────────────────────

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.github.start()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("커맨드 %d개를 테스트 서버(%s)에만 동기화했습니다 — "
                     "다른 서버에서는 커맨드가 보이지 않습니다",
                     len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("커맨드 %d개를 전역 동기화했습니다 (반영까지 최대 1시간)",
                     len(synced))

        self.tree.on_error = self._on_app_command_error

        if not self.github.authenticated:
            log.warning(
                "GITHUB_TOKEN 이 없습니다. GitHub API 가 시간당 60회로 제한되고 "
                "비공개 저장소는 추적할 수 없습니다."
            )

    async def close(self) -> None:
        await self.github.close()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("로그인: %s (서버 %d개)", self.user, len(self.guilds))

        if not self.config.guild_id and not self._cleaned_guild_commands:
            self._cleaned_guild_commands = True
            await self._clear_stale_guild_commands()

    async def _clear_stale_guild_commands(self) -> None:
        """전역 등록으로 전환했을 때 남아있는 서버 전용 커맨드를 지운다.

        개발 중 GUILD_ID 로 동기화하면 그 서버에 커맨드 사본이 생긴다. 나중에
        GUILD_ID 를 비워 전역 등록으로 바꿔도 사본은 그대로 남아서, 해당 서버에서는
        같은 커맨드가 두 개씩 보인다. 봇이 들어가 있는 서버를 훑어 정리한다.

        setup_hook 이 아니라 on_ready 에서 하는 이유는 그 시점에야 길드 목록이
        채워지기 때문이다. 남은 커맨드가 있는 서버만 동기화해서 불필요한 API
        호출을 피한다.
        """
        for guild in self.guilds:
            try:
                stale = await self.tree.fetch_commands(guild=guild)
            except discord.HTTPException:
                continue
            if not stale:
                continue
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                log.warning("%s 서버의 중복 커맨드 정리 실패: %s", guild.name, exc)
            else:
                log.info(
                    "%s 서버에 남아있던 서버 전용 커맨드 %d개를 정리했습니다 "
                    "(이제 전역 커맨드만 보입니다)",
                    guild.name, len(stale),
                )

    # ── 알림 전송 ───────────────────────────────────────────

    async def deliver(self, repo: str, post: Post) -> int:
        """이 저장소를 구독한 모든 채널에 임베드를 보낸다. 보낸 수를 돌려준다."""
        watches = await self.db.watches_for_repo(repo)
        sent = 0

        for watch in watches:
            if not watch.wants(post.category):
                continue

            channel = self.get_channel(watch.channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(watch.channel_id)
                except discord.NotFound:
                    # 채널이 삭제됐다. 계속 실패하지 않게 구독을 정리한다.
                    removed = await self.db.remove_watches_for_channel(watch.channel_id)
                    log.info(
                        "채널 %s 이(가) 사라져 구독 %d건을 정리했습니다",
                        watch.channel_id, removed,
                    )
                    continue
                except discord.Forbidden:
                    log.warning("채널 %s 에 접근 권한이 없습니다", watch.channel_id)
                    continue

            if not isinstance(channel, discord.abc.Messageable):
                continue

            try:
                await channel.send(embed=post.embed)
            except discord.Forbidden:
                log.warning(
                    "채널 %s 에 메시지를 보낼 권한이 없습니다 "
                    "(메시지 보내기 · 링크 첨부 권한을 확인해주세요)",
                    watch.channel_id,
                )
            except discord.HTTPException as exc:
                log.warning("채널 %s 전송 실패: %s", watch.channel_id, exc)
            else:
                sent += 1

        # 전송 성공을 남기지 않으면 "알림이 안 온다"는 신고가 들어왔을 때 봇이
        # 안 보낸 건지 디스코드에서 막힌 건지 로그로 가릴 수가 없다.
        if sent:
            log.info("%s [%s] → 채널 %d곳 전송", repo, post.category, sent)
        return sent

    # ── 에러 처리 ───────────────────────────────────────────

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.MissingPermissions):
            message = "이 명령은 서버 관리 권한이 있어야 사용할 수 있습니다."
        elif isinstance(original, RateLimited):
            message = original.message
        elif isinstance(original, GitHubError):
            message = original.message
        elif isinstance(original, ValueError):
            message = str(original)
        else:
            log.exception("커맨드 처리 중 오류", exc_info=original)
            message = "처리 중 오류가 났습니다. 잠시 뒤 다시 시도해주세요."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
