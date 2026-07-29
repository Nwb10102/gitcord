"""핵심 로직 테스트.

pytest 없이 `python tests/test_core.py` 로 돈다 — Dishost 환경에 개발 의존성을
끌고 가지 않으려는 의도다. 디스코드나 GitHub 에 접속하지 않는다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISCORD_TOKEN", "test-token")

from gitcord import categories as cat  # noqa: E402
from gitcord.db import SEEN_LIMIT, Database  # noqa: E402
from gitcord.embeds import apply_compare, from_activity, from_workflow_run  # noqa: E402
from gitcord.github import normalize_repo  # noqa: E402

# 저장소 이벤트 API 가 실제로 보내는 PushEvent 페이로드. commits 가 없다 —
# 이것 때문에 커밋 목록을 compare 로 따로 받아야 한다.
REAL_PUSH_PAYLOAD = {
    "before": "ded32878" + "0" * 32,
    "head": "414f0513" + "0" * 32,
    "push_id": 123456789,
    "ref": "refs/heads/main",
    "repository_id": 1362490,
}

PASSED: list[str] = []


def ok(name: str) -> None:
    PASSED.append(name)
    print(f"  OK  {name}")


# ── 저장소 이름 정규화 ──────────────────────────────────────


def test_normalize_repo() -> None:
    print("normalize_repo")
    for raw, expected in [
        ("Owner/Repo", "owner/repo"),
        ("https://github.com/psf/requests", "psf/requests"),
        ("github.com/psf/requests.git", "psf/requests"),
        ("  /psf/requests/  ", "psf/requests"),
        ("https://github.com/psf/requests/pull/1", "psf/requests"),
    ]:
        assert normalize_repo(raw) == expected, raw
    for bad in ["", "notarepo", "a/b c", "https://example.com/"]:
        try:
            normalize_repo(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} 가 통과했습니다")
    ok("URL·대소문자·.git 접미사·잘못된 입력")


# ── 카테고리 ────────────────────────────────────────────────


def test_categories() -> None:
    print("categories")
    assert cat.parse("push, ci") == ("push", "ci")
    assert cat.parse("all") == tuple(cat.CATEGORIES)
    assert cat.parse("commits,pullrequest") == ("push", "pr")
    # 출력 순서는 입력 순서가 아니라 CATEGORIES 순서를 따른다
    assert cat.parse("ci,push") == ("push", "ci")
    try:
        cat.parse("nope")
    except cat.UnknownCategory as exc:
        assert exc.name == "nope"
    else:
        raise AssertionError("잘못된 카테고리가 통과했습니다")
    ok("별칭·all·순서 정규화·오류")


# ── 임베드 ──────────────────────────────────────────────────


def _event(type_: str, payload: dict, *, eid: str = "1") -> dict:
    return {
        "id": eid,
        "type": type_,
        "created_at": "2026-07-29T10:00:00Z",
        "actor": {"login": "alice", "avatar_url": None},
        "repo": {"name": "owner/repo"},
        "payload": payload,
    }


def test_embeds() -> None:
    print("embeds")
    push = _event(
        "PushEvent",
        {
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "head": "b" * 40,
            "distinct_size": 2,
            "commits": [
                {"sha": "a1b2c3d4", "message": "fix: 토큰 만료 처리\n\n본문",
                 "distinct": True, "author": {"name": "Alice"}},
                {"sha": "e5f6a7b8", "message": "chore: 로그 정리",
                 "distinct": True, "author": {"name": "Bob"}},
            ],
        },
    )
    post = from_activity(push)
    assert post and post.category == "push" and "커밋 2개" in post.embed.title
    assert post.diff_ref and post.diff_ref.branch == "main"
    # 커밋 메시지는 첫 줄만 쓴다
    assert "본문" not in post.embed.description
    ok("push — 제목·diff 좌표·첫 줄만")

    # 새 브랜치 푸시(before 가 0)는 diff 좌표를 만들 수 없다
    new_branch = _event("PushEvent", dict(push["payload"], before="0" * 40))
    post = from_activity(new_branch)
    assert post and post.diff_ref is None
    ok("push — 새 브랜치는 diff 좌표 없음")

    # 브랜치 삭제 푸시(head 가 0)는 무시 — DeleteEvent 로 따로 온다
    assert from_activity(_event("PushEvent", dict(push["payload"], head="0" * 40))) is None
    ok("push — 브랜치 삭제는 무시")

    # ★ 회귀 방지: 실제 저장소 이벤트 API 는 commits 를 주지 않는다.
    #   예전 구현은 commits 가 없으면 None 을 돌려줘서 푸시 알림이 전부 사라졌다.
    real = from_activity(_event("PushEvent", REAL_PUSH_PAYLOAD))
    assert real is not None, "commits 없는 실제 PushEvent 가 버려졌습니다"
    assert real.category == "push" and real.diff_ref is not None
    assert "main" in real.embed.title and "/compare/" in real.embed.url
    ok("★ commits 가 없는 실제 PushEvent 도 알림이 만들어진다 (회귀 방지)")

    # compare 응답으로 커밋 목록을 채운다
    apply_compare(real, {
        "total_commits": 12,
        "commits": [
            {"sha": f"{n:040x}",
             "commit": {"message": f"커밋 {n}\n\n본문", "author": {"name": "Alice"}}}
            for n in range(12)
        ],
    })
    assert "커밋 12개" in real.embed.title, real.embed.title
    assert "…외 2개" in real.embed.description
    # compare 는 오래된 것부터 주므로 최신이 위로 와야 한다
    first_line = real.embed.description.splitlines()[0]
    assert "커밋 11" in first_line, first_line
    ok("apply_compare — 개수·최신순·초과분 접기")

    # compare 가 실패해도(빈 응답) 임베드는 그대로 남아야 한다
    skeleton = from_activity(_event("PushEvent", REAL_PUSH_PAYLOAD))
    apply_compare(skeleton, {})
    assert skeleton.embed.title and "푸시" in skeleton.embed.title
    ok("apply_compare — 응답이 비면 뼈대 임베드를 유지")

    merged = _event("PullRequestEvent", {
        "action": "closed",
        "pull_request": {"number": 7, "title": "리팩터링", "merged": True,
                         "html_url": "https://x", "base": {"ref": "main"},
                         "head": {"ref": "feat"}, "additions": 10,
                         "deletions": 3, "changed_files": 2},
    })
    post = from_activity(merged)
    assert post and post.category == "pr" and "머지됨" in post.embed.title
    closed = _event("PullRequestEvent", {
        "action": "closed",
        "pull_request": {"number": 7, "title": "리팩터링", "merged": False,
                         "html_url": "https://x"},
    })
    assert "닫힘" in from_activity(closed).embed.title
    # 저장소 이벤트 API 는 action 자체를 "merged" 로 보낸다 (웹훅과 다름)
    api_merged = _event("PullRequestEvent", {
        "action": "merged",
        "pull_request": {"number": 8, "title": "API 머지", "html_url": "https://x"},
    })
    assert "머지됨" in from_activity(api_merged).embed.title
    # 라벨 부착 같은 잡음은 무시
    assert from_activity(_event("PullRequestEvent", {
        "action": "labeled",
        "pull_request": {"number": 9, "title": "x", "html_url": "https://x"},
    })) is None
    ok("★ PR — 머지·닫힘·API 전용 merged action·라벨 잡음 무시")

    # 저장소 이벤트 API 의 리뷰 action 은 "created" (웹훅은 "submitted")
    review = _event("PullRequestReviewEvent", {
        "action": "created",
        "review": {"state": "approved", "body": "좋습니다", "html_url": "https://x"},
        "pull_request": {"number": 4, "title": "리뷰 대상", "html_url": "https://x"},
    })
    post = from_activity(review)
    assert post and post.category == "pr" and "승인" in post.embed.title
    ok("★ PR 리뷰 — API 의 created action 을 인식한다 (회귀 방지)")

    # PR 댓글은 issue 가 아니라 pr 로 분류돼야 한다
    pr_comment = _event("IssueCommentEvent", {
        "action": "created",
        "issue": {"number": 3, "title": "버그", "pull_request": {}},
        "comment": {"body": "LGTM", "html_url": "https://x"},
    })
    assert from_activity(pr_comment).category == "pr"
    issue_comment = _event("IssueCommentEvent", {
        "action": "created",
        "issue": {"number": 3, "title": "버그"},
        "comment": {"body": "확인했습니다", "html_url": "https://x"},
    })
    assert from_activity(issue_comment).category == "issue"
    ok("댓글 — PR 댓글과 이슈 댓글 분리")

    assert from_activity({"type": "MemberEvent", "payload": {}}) is None
    # 페이로드가 망가져도 폴링을 멈추면 안 된다
    assert from_activity({"type": "PushEvent", "payload": None}) is None
    ok("알 수 없거나 깨진 이벤트는 None")

    run = {"id": 55, "status": "completed", "conclusion": "failure", "name": "CI",
           "display_title": "fix: 토큰 만료", "head_branch": "main",
           "head_sha": "c" * 40, "html_url": "https://x", "run_attempt": 2,
           "updated_at": "2026-07-29T10:05:00Z", "actor": {"login": "alice"}}
    post = from_workflow_run("owner/repo", run)
    assert post and post.category == "ci" and "실패" in post.embed.title
    assert from_workflow_run("owner/repo", {"status": "in_progress"}) is None
    assert from_workflow_run("owner/repo", dict(run, conclusion="skipped")) is None
    ok("CI — 실패 표시·진행중/건너뜀 무시")


# ── 폴링 엔진 ───────────────────────────────────────────────


class FakeGitHub:
    """미리 정해둔 페이지를 순서대로 돌려주는 가짜 클라이언트."""

    def __init__(self, pages: list[list[dict]], runs: list[list[dict]] | None = None):
        self._pages = pages
        self._runs = runs or []
        self.calls = 0
        self.compares = 0

    async def repo_events(self, repo: str, *, etag: str | None = None):
        page = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return SimpleNamespace(events=page, etag='W/"x"', poll_interval=60)

    async def workflow_runs(self, repo: str, *, limit: int = 10):
        index = min(self.calls - 1, len(self._runs) - 1)
        return self._runs[index] if self._runs else []

    async def compare(self, repo: str, base: str, head: str):
        self.compares += 1
        return {
            "total_commits": 1,
            "commits": [{"sha": "a" * 40,
                         "commit": {"message": "커밋", "author": {"name": "Alice"}}}],
        }


class FakeBot:
    def __init__(self, db: Database, github: FakeGitHub):
        self.db = db
        self.github = github
        self.config = SimpleNamespace(poll_interval=90)
        self.delivered: list[tuple[str, str]] = []

    async def deliver(self, repo: str, post) -> int:
        self.delivered.append((post.category, post.embed.title))
        return 1


async def test_watcher(db: Database) -> None:
    print("watcher (폴링 커서)")
    from gitcord.cogs.watcher import MAX_BURST, Watcher

    # GitHub 은 이벤트 종류마다 다른 id 대역을 쓴다. 실제 psf/requests 응답에서
    # 확인한 값이다.
    pr_event = _event("PullRequestEvent", {
        "action": "opened",
        "pull_request": {"number": 1, "title": "첫 PR", "html_url": "https://x"},
    }, eid="12400000000")
    # 실제 API 모양(commits 없음)을 써서 compare 보강 경로까지 태운다
    push_event = _event("PushEvent", REAL_PUSH_PAYLOAD, eid="15900000000")
    later_pr = _event("PullRequestEvent", {
        "action": "opened",
        "pull_request": {"number": 2, "title": "나중 PR", "html_url": "https://x"},
    }, eid="12400000001")

    pages = [
        [pr_event],                          # 1회차: 기준점만
        [push_event, pr_event],              # 2회차: push 신규
        [later_pr, push_event, pr_event],    # 3회차: id 가 더 작은 PR 신규
    ]
    github = FakeGitHub(pages)
    bot = FakeBot(db, github)
    watcher = Watcher(bot)

    await watcher._poll_repo("owner/repo", False, await db.get_cursor("owner/repo"))
    assert bot.delivered == [], f"첫 폴링에서 알림이 나갔습니다: {bot.delivered}"
    ok("첫 폴링은 기준점만 잡고 아무것도 보내지 않는다")

    await watcher._poll_repo("owner/repo", False, await db.get_cursor("owner/repo"))
    assert [c for c, _ in bot.delivered] == ["push"], bot.delivered
    assert github.compares == 1, "푸시 알림에 compare 보강이 호출되지 않았습니다"
    assert "커밋 1개" in bot.delivered[0][1], bot.delivered[0][1]
    ok("두 번째 폴링에서 신규 push 만 전송 (compare 로 커밋 목록 보강)")

    bot.delivered.clear()
    await watcher._poll_repo("owner/repo", False, await db.get_cursor("owner/repo"))
    # 회귀 방지: max(id) 커서였다면 12.4억대 PR 이 15.9억대 push 에 가려 사라진다.
    assert [c for c, _ in bot.delivered] == ["pr"], (
        "id 대역이 다른 이벤트가 유실됐습니다 (max(id) 커서 회귀). "
        f"실제: {bot.delivered}"
    )
    ok("★ id 대역이 낮은 PR 이 push 이후에도 전송된다 (회귀 방지)")

    bot.delivered.clear()
    await watcher._poll_repo("owner/repo", False, await db.get_cursor("owner/repo"))
    assert bot.delivered == [], f"같은 이벤트가 재전송됐습니다: {bot.delivered}"
    ok("이미 본 이벤트는 재전송하지 않는다")

    # ── 버스트 상한 ──
    many = [
        _event("PullRequestEvent", {
            "action": "opened",
            "pull_request": {"number": n, "title": f"PR {n}", "html_url": "https://x"},
        }, eid=str(13000000000 + n))
        for n in range(MAX_BURST + 8)
    ]
    github2 = FakeGitHub([[], many])
    bot2 = FakeBot(db, github2)
    watcher2 = Watcher(bot2)
    await watcher2._poll_repo("owner/burst", False, await db.get_cursor("owner/burst"))
    await watcher2._poll_repo("owner/burst", False, await db.get_cursor("owner/burst"))
    assert len(bot2.delivered) == MAX_BURST, len(bot2.delivered)
    ok(f"한 사이클에 최대 {MAX_BURST}건만 전송")

    # 이벤트가 없는 저장소도 기준점이 잡혀야 한다 — 안 그러면 첫 활동을 놓친다
    cursor = await db.get_cursor("owner/burst")
    assert cursor.events_primed
    ok("이벤트가 0건인 저장소도 기준점이 잡힌다")

    # ── CI: 순서를 바꿔 끝나는 실행 ──
    def run(rid: int, updated: str, status: str = "completed"):
        return {"id": rid, "status": status, "conclusion": "success", "name": "CI",
                "head_branch": "main", "head_sha": "d" * 40, "html_url": "https://x",
                "updated_at": updated, "actor": {"login": "alice"}}

    runs_pages = [
        [run(100, "2026-07-29T10:00:00Z")],                    # 기준점
        [run(101, "2026-07-29T10:05:00Z", "in_progress"),      # 진행중은 기록 안 함
         run(102, "2026-07-29T10:04:00Z"),
         run(100, "2026-07-29T10:00:00Z")],
        [run(101, "2026-07-29T10:06:00Z"),                     # 101 이 뒤늦게 완료
         run(102, "2026-07-29T10:04:00Z"),
         run(100, "2026-07-29T10:00:00Z")],
    ]
    github3 = FakeGitHub([[], [], []], runs_pages)
    bot3 = FakeBot(db, github3)
    watcher3 = Watcher(bot3)
    for _ in range(3):
        await watcher3._poll_repo("owner/ci", True, await db.get_cursor("owner/ci"))
    delivered_titles = [t for _, t in bot3.delivered]
    assert len(delivered_titles) == 2, f"CI 알림 {delivered_titles}"
    ok("★ 진행중이던 실행이 뒤늦게 끝나도 알림이 간다 (역순 완료)")

    # ── seen 목록 상한 ──
    cursor = await db.get_cursor("owner/repo")
    huge = cursor.merge_seen("seen_events", [str(i) for i in range(SEEN_LIMIT + 50)])
    assert len(huge) == SEEN_LIMIT
    assert huge[0] == "0", "최신이 앞에 와야 합니다"
    ok(f"seen 목록은 {SEEN_LIMIT}개로 잘린다")


# ── DB ──────────────────────────────────────────────────────


async def test_db(db: Database) -> None:
    """watcher 테스트와 같은 DB 를 쓰므로 저장소 이름을 겹치지 않게 둔다."""
    print("db")
    alpha, beta = "dbtest/alpha", "dbtest/beta"

    assert await db.add_watch(guild_id=1, channel_id=2, repo=alpha,
                              categories=("push", "ci"))
    assert not await db.add_watch(guild_id=1, channel_id=2, repo=alpha,
                                  categories=("push",)), "중복 구독이 허용됐습니다"
    ok("같은 서버·채널·저장소 중복 구독 차단")

    watches = await db.list_watches(1)
    assert len(watches) == 1 and watches[0].categories == ("push", "ci")
    assert watches[0].wants("push") and not watches[0].wants("pr")
    ok("구독 조회와 카테고리 판정")

    await db.add_watch(guild_id=9, channel_id=8, repo=beta, categories=("pr",))
    tracked = dict(await db.tracked_repos())
    assert tracked[alpha] is True and tracked[beta] is False, tracked
    ok("tracked_repos 가 CI 구독 여부를 함께 알려준다")

    cursor = await db.get_cursor(alpha)
    assert cursor.seen_events == () and not cursor.events_primed
    await db.save_cursor(replace(cursor, etag='W/"e"', seen_events=("1", "2"),
                                 events_primed=True))
    restored = await db.get_cursor(alpha)
    assert restored.seen_events == ("1", "2") and restored.events_primed
    assert restored.etag == 'W/"e"'
    ok("커서 저장·복원 (seen 목록과 primed 플래그 포함)")

    await db.set_categories(guild_id=1, repo=alpha, categories=("pr",))
    assert (await db.list_watches(1))[0].categories == ("pr",)
    ok("알림 종류 변경")

    assert await db.remove_watch(guild_id=1, repo=alpha) == 1
    assert await db.prune_cursors() >= 1
    leftover = await db.fetchall("SELECT 1 FROM repo_cursor WHERE repo = ?", (alpha,))
    assert leftover == [], "구독이 사라졌는데 커서가 남았습니다"
    ok("구독 해제 시 커서도 정리된다")


# ── 실행 ────────────────────────────────────────────────────


async def main() -> int:
    test_normalize_repo()
    test_categories()
    test_embeds()

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        await db.connect()
        try:
            await test_watcher(db)
            await test_db(db)
        finally:
            await db.close()

    print(f"\n{len(PASSED)}개 항목 전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
