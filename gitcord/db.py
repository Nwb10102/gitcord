"""SQLite 저장소.

의존성을 늘리지 않으려고 aiosqlite 대신 stdlib sqlite3 를 쓰고, 블로킹 호출만
asyncio.to_thread 로 넘긴다. 커넥션 하나를 여러 스레드가 쓰게 되므로
check_same_thread=False + asyncio.Lock 으로 직렬화한다. 이 봇 규모(서버 몇 개,
90초 주기 폴링)에서는 경합이 문제가 되지 않는다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .categories import CATEGORIES

# 인덱스 = 버전. PRAGMA user_version 과 대조해 못 적용한 것만 적용한다.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE guild_config (
            guild_id      INTEGER PRIMARY KEY,
            ai_provider   TEXT,
            ai_key_enc    BLOB,
            ai_model      TEXT,
            ai_enabled    INTEGER NOT NULL DEFAULT 1,
            ai_daily_cap  INTEGER NOT NULL DEFAULT 50,
            updated_at    TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE watch (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            repo        TEXT    NOT NULL,
            events      TEXT    NOT NULL,
            ai_summary  INTEGER NOT NULL DEFAULT 0,
            branches    TEXT,
            created_at  TEXT    NOT NULL,
            UNIQUE (guild_id, channel_id, repo)
        )
        """,
        "CREATE INDEX idx_watch_repo ON watch (repo)",
        "CREATE INDEX idx_watch_guild ON watch (guild_id)",
        # seen_events / seen_runs 는 "최근 본 id" 목록이다. 스칼라 커서를 쓸 수
        # 없는 이유는 GitHub 활동 이벤트의 id 가 단조 증가하지 않기 때문이다 —
        # PushEvent·CreateEvent·DeleteEvent 는 15,9xx,xxx,xxx 대역을, PR·이슈·
        # 스타는 12,4xx,xxx,xxx 대역을 쓴다. max(id) 를 커서로 삼으면 커밋이
        # 한 번 푸시되는 순간 이후의 PR·이슈 알림이 전부 걸러진다.
        """
        CREATE TABLE repo_cursor (
            repo           TEXT PRIMARY KEY,
            etag           TEXT,
            seen_events    TEXT    NOT NULL DEFAULT '',
            seen_runs      TEXT    NOT NULL DEFAULT '',
            events_primed  INTEGER NOT NULL DEFAULT 0,
            runs_primed    INTEGER NOT NULL DEFAULT 0,
            next_poll_at   REAL    NOT NULL DEFAULT 0,
            fail_count     INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE ai_usage (
            guild_id      INTEGER NOT NULL,
            day           TEXT    NOT NULL,
            calls         INTEGER NOT NULL DEFAULT 0,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, day)
        )
        """,
        """
        CREATE TABLE summary_cache (
            sha        TEXT NOT NULL,
            provider   TEXT NOT NULL,
            model      TEXT NOT NULL,
            summary    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (sha, provider, model)
        )
        """,
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Watch:
    id: int
    guild_id: int
    channel_id: int
    repo: str
    categories: tuple[str, ...]
    ai_summary: bool

    def wants(self, category: str) -> bool:
        return category in self.categories


# 저장소당 기억할 최근 id 개수. GitHub 은 한 번에 최대 30건을 주고 폴링 주기는
# 최소 60초라, 200건이면 폭주 상황에서도 중복 알림을 막기에 충분하다.
SEEN_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Cursor:
    """저장소 하나의 폴링 상태.

    seen_events/seen_runs 는 최신이 앞에 오는 id 목록이다. 스칼라 커서 대신
    집합을 쓰는 이유는 repo_cursor 테이블 정의의 주석을 참고.
    primed 플래그는 "첫 폴링을 마쳤다"는 뜻으로, 구독 직후 과거 이벤트가
    쏟아지는 것을 막는다. 이벤트가 하나도 없는 저장소도 정상 처리하려면
    목록이 비었는지로는 판단할 수 없어서 플래그를 따로 둔다.
    """

    repo: str
    etag: str | None = None
    seen_events: tuple[str, ...] = ()
    seen_runs: tuple[str, ...] = ()
    events_primed: bool = False
    runs_primed: bool = False
    next_poll_at: float = 0.0
    fail_count: int = 0

    def merge_seen(self, field: str, current: list[str]) -> tuple[str, ...]:
        """이번에 본 id 를 앞에 붙이고 SEEN_LIMIT 개로 자른다."""
        previous = getattr(self, field)
        seen = set(current)
        merged = list(current) + [i for i in previous if i not in seen]
        return tuple(merged[:SEEN_LIMIT])


def _to_watch(row: sqlite3.Row) -> Watch:
    stored = [c for c in row["events"].split(",") if c in CATEGORIES]
    return Watch(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        repo=row["repo"],
        # CATEGORIES 순서로 정렬해 출력이 항상 같은 순서가 되게 한다.
        categories=tuple(name for name in CATEGORIES if name in stored),
        ai_summary=bool(row["ai_summary"]),
    )


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ── 수명주기 ────────────────────────────────────────────

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)
        await self._migrate()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL 은 읽기와 쓰기가 서로를 막지 않게 한다.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def _migrate(self) -> None:
        def run(conn: sqlite3.Connection) -> None:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(_MIGRATIONS)):
                for statement in _MIGRATIONS[index]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {index + 1}")
            conn.commit()

        await self._call(run)

    # ── 저수준 실행 ─────────────────────────────────────────

    async def _call(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        if self._conn is None:
            raise RuntimeError("Database.connect() 를 먼저 호출해야 합니다")
        conn = self._conn
        async with self._lock:
            return await asyncio.to_thread(fn, conn)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """쓰기 쿼리. 영향받은 행 수를 돌려준다."""

        def run(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount

        return await self._call(run)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self._call(lambda conn: conn.execute(sql, params).fetchall())

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return await self._call(lambda conn: conn.execute(sql, params).fetchone())

    # ── 구독(watch) ─────────────────────────────────────────

    async def add_watch(
        self,
        *,
        guild_id: int,
        channel_id: int,
        repo: str,
        categories: Sequence[str],
        ai_summary: bool = False,
    ) -> bool:
        """새 구독을 추가한다. 이미 있으면 False."""

        def run(conn: sqlite3.Connection) -> bool:
            try:
                conn.execute(
                    "INSERT INTO watch"
                    " (guild_id, channel_id, repo, events, ai_summary, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        guild_id,
                        channel_id,
                        repo,
                        ",".join(categories),
                        int(ai_summary),
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            conn.commit()
            return True

        return await self._call(run)

    async def remove_watch(
        self, *, guild_id: int, repo: str, channel_id: int | None = None
    ) -> int:
        """channel_id 를 생략하면 이 서버의 모든 채널에서 해제한다."""
        if channel_id is None:
            return await self.execute(
                "DELETE FROM watch WHERE guild_id = ? AND repo = ?", (guild_id, repo)
            )
        return await self.execute(
            "DELETE FROM watch WHERE guild_id = ? AND repo = ? AND channel_id = ?",
            (guild_id, repo, channel_id),
        )

    async def remove_watches_for_channel(self, channel_id: int) -> int:
        """채널이 삭제됐거나 권한이 사라졌을 때 정리한다."""
        return await self.execute("DELETE FROM watch WHERE channel_id = ?", (channel_id,))

    async def set_categories(
        self, *, guild_id: int, repo: str, categories: Sequence[str]
    ) -> int:
        return await self.execute(
            "UPDATE watch SET events = ? WHERE guild_id = ? AND repo = ?",
            (",".join(categories), guild_id, repo),
        )

    async def list_watches(self, guild_id: int) -> list[Watch]:
        rows = await self.fetchall(
            "SELECT * FROM watch WHERE guild_id = ? ORDER BY repo, channel_id",
            (guild_id,),
        )
        return [_to_watch(row) for row in rows]

    async def watches_for_repo(self, repo: str) -> list[Watch]:
        rows = await self.fetchall("SELECT * FROM watch WHERE repo = ?", (repo,))
        return [_to_watch(row) for row in rows]

    async def tracked_repos(self) -> list[tuple[str, bool]]:
        """폴링해야 할 저장소 목록. (repo, CI 구독자 존재 여부)"""
        rows = await self.fetchall(
            "SELECT repo,"
            "       MAX(CASE WHEN ',' || events || ',' LIKE '%,ci,%' THEN 1 ELSE 0 END)"
            "         AS needs_ci"
            "  FROM watch GROUP BY repo"
        )
        return [(row["repo"], bool(row["needs_ci"])) for row in rows]

    # ── 폴링 커서 ───────────────────────────────────────────

    async def get_cursor(self, repo: str) -> Cursor:
        row = await self.fetchone("SELECT * FROM repo_cursor WHERE repo = ?", (repo,))
        if row is None:
            return Cursor(repo=repo)
        return Cursor(
            repo=row["repo"],
            etag=row["etag"],
            seen_events=tuple(i for i in row["seen_events"].split(",") if i),
            seen_runs=tuple(i for i in row["seen_runs"].split(",") if i),
            events_primed=bool(row["events_primed"]),
            runs_primed=bool(row["runs_primed"]),
            next_poll_at=row["next_poll_at"],
            fail_count=row["fail_count"],
        )

    async def save_cursor(self, cursor: Cursor) -> None:
        await self.execute(
            "INSERT INTO repo_cursor"
            " (repo, etag, seen_events, seen_runs, events_primed, runs_primed,"
            "  next_poll_at, fail_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(repo) DO UPDATE SET"
            "   etag = excluded.etag,"
            "   seen_events = excluded.seen_events,"
            "   seen_runs = excluded.seen_runs,"
            "   events_primed = excluded.events_primed,"
            "   runs_primed = excluded.runs_primed,"
            "   next_poll_at = excluded.next_poll_at,"
            "   fail_count = excluded.fail_count",
            (
                cursor.repo,
                cursor.etag,
                ",".join(cursor.seen_events),
                ",".join(cursor.seen_runs),
                int(cursor.events_primed),
                int(cursor.runs_primed),
                cursor.next_poll_at,
                cursor.fail_count,
            ),
        )

    async def prune_cursors(self) -> int:
        """구독자가 사라진 저장소의 커서를 지운다."""
        return await self.execute(
            "DELETE FROM repo_cursor WHERE repo NOT IN (SELECT DISTINCT repo FROM watch)"
        )
