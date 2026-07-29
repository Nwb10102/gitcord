"""폴링 엔진.

저장소마다 커서를 들고 주기적으로 GitHub 을 찌른다. 서버 여러 개가 같은
저장소를 구독해도 커서는 저장소 단위라 호출은 한 번이다.

**커서는 스칼라가 아니라 "최근 본 id 집합"이다.** GitHub 활동 이벤트의 id 는
단조 증가하지 않는다 — Push/Create/Delete 는 15,9xx,xxx,xxx 대역을, PR·이슈·
스타는 12,4xx,xxx,xxx 대역을 쓴다. `max(id)` 를 커서로 삼으면 커밋이 한 번
푸시되는 순간 이후의 PR·이슈 알림이 전부 걸러진다. 워크플로 실행도 같은 방식을
쓰면 실행이 순서를 바꿔 끝나도(5번이 4번보다 먼저 완료) 놓치지 않는다.

그 외에 지키는 것:
- **첫 폴링은 조용히**: primed 플래그가 서기 전에는 현재 목록만 기록하고 아무것도
  보내지 않는다. 안 그러면 구독하자마자 과거 30건이 채널에 쏟아진다.
- **버스트 상한**: 한 사이클에 저장소당 MAX_BURST 건까지만 보낸다.
- **한도 소진 시 전면 정지**: rate limit 에 걸리면 reset 시각까지 폴링을 멈춘다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

import aiohttp
from discord.ext import commands, tasks

from ..bot import Gitcord
from ..db import Cursor
from ..embeds import Post, apply_compare, from_activity, from_workflow_run
from ..github import GitHubError, RateLimited, RepoNotFound

log = logging.getLogger("gitcord.watcher")

# 한 사이클에 저장소당 보낼 최대 임베드 수
MAX_BURST = 15
# 연속 실패 시 백오프 상한(초)
MAX_BACKOFF = 1800
# 저장소가 사라졌거나 접근 권한이 없을 때의 재시도 간격(초)
NOT_FOUND_BACKOFF = 3600


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in items if item.get("id") is not None]


class Watcher(commands.Cog):
    def __init__(self, bot: Gitcord) -> None:
        self.bot = bot
        # rate limit 에 걸리면 이 시각까지 사이클 전체를 건너뛴다.
        self._blocked_until = 0.0
        self.poll.change_interval(seconds=bot.config.poll_interval)

    async def cog_load(self) -> None:
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    @tasks.loop(seconds=90)
    async def poll(self) -> None:
        now = time.time()
        if now < self._blocked_until:
            return

        repos = await self.bot.db.tracked_repos()
        if not repos:
            return

        for repo, needs_ci in repos:
            cursor = await self.bot.db.get_cursor(repo)
            if cursor.next_poll_at > now:
                continue

            try:
                await self._poll_repo(repo, needs_ci, cursor)
            except RateLimited as exc:
                self._blocked_until = exc.reset_at
                log.warning("%s — 이번 사이클을 중단합니다", exc.message)
                return
            except RepoNotFound:
                # 토큰이 만료돼도 404 가 날 수 있어서 구독을 지우지는 않는다.
                # 크게 물러났다가 다시 시도한다.
                log.warning(
                    "%s 저장소에 접근할 수 없습니다. %d초 뒤 재시도합니다.",
                    repo, NOT_FOUND_BACKOFF,
                )
                await self._back_off(cursor, NOT_FOUND_BACKOFF)
            except (GitHubError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                delay = min(
                    self.bot.config.poll_interval * (2 ** cursor.fail_count), MAX_BACKOFF
                )
                log.warning("%s 폴링 실패(%s). %d초 뒤 재시도합니다.", repo, exc, delay)
                await self._back_off(cursor, delay, count_failure=True)

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    @poll.error
    async def _on_poll_error(self, exc: BaseException) -> None:
        # tasks.loop 은 처리되지 않은 예외를 만나면 루프를 멈춘다. 다시 살린다.
        log.exception("폴링 루프가 예외로 멈췄습니다. 재시작합니다.", exc_info=exc)
        self.poll.restart()

    # ── 내부 ────────────────────────────────────────────────

    async def _back_off(
        self, cursor: Cursor, delay: float, *, count_failure: bool = False
    ) -> None:
        await self.bot.db.save_cursor(
            replace(
                cursor,
                next_poll_at=time.time() + delay,
                fail_count=cursor.fail_count + 1 if count_failure else cursor.fail_count,
            )
        )

    async def _poll_repo(self, repo: str, needs_ci: bool, cursor: Cursor) -> None:
        page = await self.bot.github.repo_events(repo, etag=cursor.etag)

        # GitHub 이 우리 설정보다 긴 주기를 권하면 그쪽을 따른다.
        interval = max(self.bot.config.poll_interval, page.poll_interval)
        updated = replace(cursor, etag=page.etag)

        if page.events is not None:
            updated = await self._handle_events(repo, page.events, updated)
        if needs_ci:
            updated = await self._handle_workflow_runs(repo, updated)

        await self.bot.db.save_cursor(
            replace(updated, next_poll_at=time.time() + interval, fail_count=0)
        )

    async def _handle_events(
        self, repo: str, events: list[dict[str, Any]], cursor: Cursor
    ) -> Cursor:
        merged = cursor.merge_seen("seen_events", _ids(events))

        if not cursor.events_primed:
            # 첫 폴링 — 기준점만 잡고 과거 이벤트는 보내지 않는다.
            log.info("%s 기준점 설정 (이벤트 %d건 기록)", repo, len(merged))
            return replace(cursor, seen_events=merged, events_primed=True)

        known = set(cursor.seen_events)
        fresh = [e for e in events if str(e.get("id")) not in known]
        if fresh:
            # created_at 도 완전한 오름차순은 아니라 id 로 한 번 더 갈라준다.
            fresh.sort(key=lambda e: (e.get("created_at") or "", str(e.get("id"))))
            for event in fresh[-MAX_BURST:]:
                post = from_activity(event)
                if post is None:
                    continue
                if post.diff_ref is not None:
                    await self._enrich_push(post)
                await self.bot.deliver(repo, post)
            if len(fresh) > MAX_BURST:
                log.info("%s 이벤트 %d건 중 최신 %d건만 전송", repo, len(fresh), MAX_BURST)

        return replace(cursor, seen_events=merged, events_primed=True)

    async def _enrich_push(self, post: Post) -> None:
        """푸시 임베드에 커밋 목록을 채운다.

        저장소 이벤트 API 의 PushEvent 페이로드에는 커밋이 들어있지 않아서
        compare 를 한 번 더 호출해야 한다. 실패하면 뼈대 임베드(브랜치 + 비교
        링크)만으로 보낸다 — 알림을 통째로 버리는 것보다 낫다.
        """
        ref = post.diff_ref
        assert ref is not None
        try:
            data = await self.bot.github.compare(ref.repo, ref.base, ref.head)
        except RateLimited:
            # 한도 소진은 사이클 전체를 멈춰야 하므로 그대로 올린다.
            raise
        except (GitHubError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.info("%s compare 실패(%s) — 커밋 목록 없이 보냅니다", ref.repo, exc)
            return
        apply_compare(post, data)

    async def _handle_workflow_runs(self, repo: str, cursor: Cursor) -> Cursor:
        runs = await self.bot.github.workflow_runs(repo)
        # 완료된 실행만 센다. 진행 중인 실행을 기록해두면 그 실행이 끝났을 때
        # 이미 본 것으로 취급돼 알림을 놓친다.
        completed = [r for r in runs if r.get("status") == "completed"]
        merged = cursor.merge_seen("seen_runs", _ids(completed))

        if not cursor.runs_primed:
            log.info("%s CI 기준점 설정 (실행 %d건 기록)", repo, len(merged))
            return replace(cursor, seen_runs=merged, runs_primed=True)

        known = set(cursor.seen_runs)
        fresh = [r for r in completed if str(r.get("id")) not in known]
        if fresh:
            fresh.sort(key=lambda r: (r.get("updated_at") or "", str(r.get("id"))))
            for run in fresh[-MAX_BURST:]:
                post = from_workflow_run(repo, run)
                if post is not None:
                    await self.bot.deliver(repo, post)

        return replace(cursor, seen_runs=merged, runs_primed=True)


async def setup(bot: Gitcord) -> None:
    await bot.add_cog(Watcher(bot))
