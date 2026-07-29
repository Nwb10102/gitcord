"""GitHub 이벤트 → 디스코드 임베드.

입력은 두 종류다.

1. `/repos/{repo}/events` 활동 이벤트 (push, PR, 이슈, 릴리스, 브랜치…)
2. `/repos/{repo}/actions/runs` 워크플로 실행 (활동 이벤트에 안 들어옴)

두 경우 모두 `Post(category, embed)` 로 통일해서 돌려준다. category 는
`categories.CATEGORIES` 의 키이고, watcher 가 이 값으로 구독 설정과 대조한다.
처리할 줄 모르는 이벤트 타입은 조용히 None 을 돌려준다 — 알림 잡음을 늘리는
것보다 빠뜨리는 편이 낫다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

import discord
from discord.utils import escape_markdown

GITHUB = "https://github.com"

# GitHub UI 와 비슷한 색을 쓴다.
GREEN = 0x2EA043  # 열림 · 성공
RED = 0xCF222E  # 닫힘 · 실패
PURPLE = 0x8250DF  # 머지됨
BLUE = 0x0969DA  # 푸시
GOLD = 0xD4A72C  # 릴리스
GRAY = 0x6E7781  # 그 외

# 한 임베드에 커밋을 몇 개까지 나열할지. 넘으면 "외 N개" 로 접는다.
MAX_COMMIT_LINES = 10

# patch 를 볼 가치가 거의 없는 경로. AI 요약 단계에서도 재사용한다.
NOISY_PATH_HINTS = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "cargo.lock",
    "node_modules/",
    "dist/",
    "build/",
    ".min.js",
    ".min.css",
)


class Post(NamedTuple):
    category: str
    embed: discord.Embed
    # AI 요약이 가능한 경우에만 채워진다 (push 전용).
    diff_ref: DiffRef | None = None


@dataclass(frozen=True, slots=True)
class DiffRef:
    """compare API 를 호출하기 위한 좌표.

    저장소 이벤트 API 의 PushEvent 페이로드에는 커밋 목록이 들어있지 않다
    (`before`, `head`, `push_id`, `ref`, `repository_id` 뿐). 그래서 커밋 목록은
    compare 로 따로 받아야 하고, 같은 응답을 AI 요약용 diff 로도 쓴다.
    """

    repo: str
    base: str
    head: str
    branch: str


# ── 문자열 헬퍼 ─────────────────────────────────────────────


def trim(text: str | None, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _first_line(text: str | None) -> str:
    value = (text or "").strip()
    return value.splitlines()[0] if value else ""


def parse_time(value: str | None) -> dt.datetime:
    if value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.datetime.now(dt.timezone.utc)


def _branch_of(ref: str | None) -> str:
    """`refs/heads/main` → `main`"""
    if not ref:
        return "?"
    return ref.rsplit("/", 1)[-1] or ref


def is_noisy_path(path: str) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in NOISY_PATH_HINTS)


# ── 공통 컨텍스트 ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Context:
    repo: str
    actor: str
    avatar: str | None
    when: dt.datetime
    payload: dict[str, Any]

    def embed(
        self,
        *,
        color: int,
        title: str,
        url: str | None = None,
        description: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=trim(title, 256),
            url=url,
            description=trim(description, 4096) or None,
            color=color,
            timestamp=self.when,
        )
        embed.set_author(
            name=trim(f"{self.actor} · {self.repo}", 256),
            url=f"{GITHUB}/{self.repo}",
            icon_url=self.avatar,
        )
        return embed


# ── 이벤트별 빌더 ───────────────────────────────────────────


def _commit_line(repo: str, sha: str, message: str, author: str) -> str:
    return (
        f"[`{sha[:7]}`]({GITHUB}/{repo}/commit/{sha}) "
        f"{trim(escape_markdown(_first_line(message)), 90)} — {escape_markdown(author)}"
    )


def _push(ctx: _Context) -> Post | None:
    """푸시 이벤트.

    저장소 이벤트 API 는 커밋 목록을 주지 않으므로 여기서는 뼈대만 만들고,
    watcher 가 compare API 로 `apply_compare()` 를 통해 채운다. 웹훅 페이로드나
    예전 응답처럼 `commits` 가 들어있으면 그대로 쓴다.
    """
    payload = ctx.payload
    head = payload.get("head")
    before = payload.get("before")

    # 브랜치 삭제 푸시는 DeleteEvent 로 따로 오므로 여기서는 건너뛴다.
    if not head or set(str(head)) == {"0"}:
        return None

    branch = _branch_of(payload.get("ref"))
    has_base = bool(before) and set(str(before)) != {"0"}
    compare_url = (
        f"{GITHUB}/{ctx.repo}/compare/{before}...{head}"
        if has_base
        else f"{GITHUB}/{ctx.repo}/commits/{branch}"
    )

    # distinct=False 는 다른 브랜치에서 이미 알린 커밋이다.
    commits = [c for c in (payload.get("commits") or []) if c.get("distinct", True)]
    total = payload.get("distinct_size") or len(commits)

    if commits:
        lines = [
            _commit_line(
                ctx.repo,
                str(commit.get("sha") or ""),
                commit.get("message") or "",
                (commit.get("author") or {}).get("name") or ctx.actor,
            )
            for commit in commits[:MAX_COMMIT_LINES]
        ]
        if total > MAX_COMMIT_LINES:
            lines.append(f"…외 {total - MAX_COMMIT_LINES}개")
        title = f"`{branch}` 에 커밋 {total}개"
        description = "\n".join(lines)
    else:
        title = f"`{branch}` 에 푸시"
        description = None

    embed = ctx.embed(
        color=BLUE, title=title, url=compare_url, description=description
    )
    diff_ref = (
        DiffRef(repo=ctx.repo, base=str(before), head=str(head), branch=branch)
        if has_base
        else None
    )
    return Post("push", embed, diff_ref)


def apply_compare(post: Post, data: dict[str, Any]) -> None:
    """compare API 응답으로 푸시 임베드의 커밋 목록과 개수를 채운다.

    호출 실패 시 그냥 건너뛰면 되도록 임베드를 제자리에서 수정한다 — 뼈대
    임베드만으로도 "어느 브랜치에 푸시됨 + 비교 링크"는 전달된다.
    """
    if post.diff_ref is None:
        return
    commits = data.get("commits") or []
    if not commits:
        return

    repo = post.diff_ref.repo
    total = data.get("total_commits") or len(commits)

    # compare 는 오래된 것부터 준다. 최신이 위로 오게 뒤집는다.
    lines = [
        _commit_line(
            repo,
            str(commit.get("sha") or ""),
            (commit.get("commit") or {}).get("message") or "",
            ((commit.get("commit") or {}).get("author") or {}).get("name") or "unknown",
        )
        for commit in reversed(commits[-MAX_COMMIT_LINES:])
    ]
    if total > MAX_COMMIT_LINES:
        lines.append(f"…외 {total - MAX_COMMIT_LINES}개")

    post.embed.title = trim(f"`{post.diff_ref.branch}` 에 커밋 {total}개", 256)
    post.embed.description = trim("\n".join(lines), 4096)


def _pull_request(ctx: _Context) -> Post | None:
    payload = ctx.payload
    action = payload.get("action")
    pr = payload.get("pull_request") or {}

    # 웹훅은 closed + merged=true 로 머지를 알리지만, 저장소 이벤트 API 는
    # action 자체를 "merged" 로 보낸다. 둘 다 받는다.
    if action == "merged":
        verb, color = "머지됨", PURPLE
    elif action == "closed":
        merged = bool(pr.get("merged"))
        verb, color = ("머지됨", PURPLE) if merged else ("닫힘", RED)
    elif action == "opened":
        verb, color = "열림", GREEN
    elif action == "reopened":
        verb, color = "다시 열림", GREEN
    elif action == "ready_for_review":
        verb, color = "리뷰 준비 완료", GREEN
    else:
        return None

    number = pr.get("number")
    embed = ctx.embed(
        color=color,
        title=f"PR #{number} {verb} · {trim(pr.get('title'), 180)}",
        url=pr.get("html_url"),
        description=trim(escape_markdown(pr.get("body") or ""), 400),
    )

    base = (pr.get("base") or {}).get("ref")
    head = (pr.get("head") or {}).get("ref")
    if base and head:
        embed.add_field(name="브랜치", value=f"`{head}` → `{base}`", inline=True)

    additions, deletions = pr.get("additions"), pr.get("deletions")
    if additions is not None and deletions is not None:
        changed = pr.get("changed_files")
        value = f"+{additions} −{deletions}"
        if changed is not None:
            value += f" ({changed}개 파일)"
        embed.add_field(name="변경", value=value, inline=True)

    return Post("pr", embed)


def _issues(ctx: _Context) -> Post | None:
    payload = ctx.payload
    action = payload.get("action")
    issue = payload.get("issue") or {}

    if action == "opened":
        verb, color = "열림", GREEN
    elif action == "closed":
        verb, color = "닫힘", PURPLE
    elif action == "reopened":
        verb, color = "다시 열림", GREEN
    else:
        return None

    embed = ctx.embed(
        color=color,
        title=f"이슈 #{issue.get('number')} {verb} · {trim(issue.get('title'), 180)}",
        url=issue.get("html_url"),
        description=trim(escape_markdown(issue.get("body") or ""), 400),
    )

    labels = [lbl.get("name") for lbl in (issue.get("labels") or []) if lbl.get("name")]
    if labels:
        embed.add_field(name="라벨", value=trim(", ".join(labels), 1024), inline=False)

    return Post("issue", embed)


def _issue_comment(ctx: _Context) -> Post | None:
    payload = ctx.payload
    if payload.get("action") != "created":
        return None

    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    # GitHub 은 PR 댓글도 issue_comment 로 보낸다. pull_request 키로 구분한다.
    is_pr = "pull_request" in issue
    kind = "PR" if is_pr else "이슈"

    embed = ctx.embed(
        color=GRAY,
        title=f"{kind} #{issue.get('number')} 에 댓글 · {trim(issue.get('title'), 160)}",
        url=comment.get("html_url"),
        description=trim(escape_markdown(comment.get("body") or ""), 600),
    )
    return Post("pr" if is_pr else "issue", embed)


def _pull_request_review(ctx: _Context) -> Post | None:
    payload = ctx.payload
    # 저장소 이벤트 API 는 "created", 웹훅은 "submitted" 를 쓴다.
    if payload.get("action") not in ("created", "submitted"):
        return None

    review = payload.get("review") or {}
    pr = payload.get("pull_request") or {}
    state = (review.get("state") or "").lower()

    labels = {
        "approved": ("승인", GREEN),
        "changes_requested": ("변경 요청", RED),
        "commented": ("리뷰 코멘트", GRAY),
    }
    if state not in labels:
        return None
    verb, color = labels[state]

    embed = ctx.embed(
        color=color,
        title=f"PR #{pr.get('number')} {verb} · {trim(pr.get('title'), 170)}",
        url=review.get("html_url") or pr.get("html_url"),
        description=trim(escape_markdown(review.get("body") or ""), 500),
    )
    return Post("pr", embed)


def _release(ctx: _Context) -> Post | None:
    payload = ctx.payload
    if payload.get("action") != "published":
        return None

    release = payload.get("release") or {}
    tag = release.get("tag_name") or "?"
    name = release.get("name") or tag

    embed = ctx.embed(
        color=GOLD,
        title=f"릴리스 {tag} · {trim(name, 180)}",
        url=release.get("html_url"),
        description=trim(escape_markdown(release.get("body") or ""), 1000),
    )
    if release.get("prerelease"):
        embed.add_field(name="종류", value="프리릴리스", inline=True)
    return Post("release", embed)


def _create(ctx: _Context) -> Post | None:
    payload = ctx.payload
    ref_type = payload.get("ref_type")
    ref = payload.get("ref")
    if ref_type not in ("branch", "tag") or not ref:
        return None

    noun = "브랜치" if ref_type == "branch" else "태그"
    embed = ctx.embed(
        color=GRAY,
        title=f"{noun} `{ref}` 생성",
        url=f"{GITHUB}/{ctx.repo}/tree/{ref}",
    )
    return Post("branch", embed)


def _delete(ctx: _Context) -> Post | None:
    payload = ctx.payload
    ref_type = payload.get("ref_type")
    ref = payload.get("ref")
    if ref_type not in ("branch", "tag") or not ref:
        return None

    noun = "브랜치" if ref_type == "branch" else "태그"
    embed = ctx.embed(color=GRAY, title=f"{noun} `{ref}` 삭제")
    return Post("branch", embed)


def _fork(ctx: _Context) -> Post | None:
    forkee = ctx.payload.get("forkee") or {}
    full_name = forkee.get("full_name")
    if not full_name:
        return None
    embed = ctx.embed(
        color=GRAY,
        title=f"저장소 포크 → {full_name}",
        url=forkee.get("html_url"),
    )
    return Post("star", embed)


def _watch(ctx: _Context) -> Post | None:
    if ctx.payload.get("action") != "started":
        return None
    embed = ctx.embed(color=GOLD, title="저장소에 스타를 눌렀습니다")
    return Post("star", embed)


_BUILDERS: dict[str, Callable[[_Context], Post | None]] = {
    "PushEvent": _push,
    "PullRequestEvent": _pull_request,
    "IssuesEvent": _issues,
    "IssueCommentEvent": _issue_comment,
    "PullRequestReviewEvent": _pull_request_review,
    "ReleaseEvent": _release,
    "CreateEvent": _create,
    "DeleteEvent": _delete,
    "ForkEvent": _fork,
    "WatchEvent": _watch,
}


def from_activity(event: dict[str, Any]) -> Post | None:
    """`/repos/{repo}/events` 항목 하나를 임베드로."""
    builder = _BUILDERS.get(event.get("type") or "")
    if builder is None:
        return None

    actor = event.get("actor") or {}
    ctx = _Context(
        repo=(event.get("repo") or {}).get("name") or "?",
        actor=actor.get("login") or "unknown",
        avatar=actor.get("avatar_url"),
        when=parse_time(event.get("created_at")),
        payload=event.get("payload") or {},
    )
    try:
        return builder(ctx)
    except Exception:
        # 페이로드 모양이 예상과 다르더라도 폴링 루프를 멈추면 안 된다.
        return None


# ── 워크플로 실행 (Actions) ─────────────────────────────────

_CONCLUSIONS: dict[str, tuple[str, int]] = {
    "success": ("성공", GREEN),
    "failure": ("실패", RED),
    "timed_out": ("시간 초과", RED),
    "action_required": ("조치 필요", RED),
    "cancelled": ("취소됨", GRAY),
    "neutral": ("완료", GRAY),
    "stale": ("만료됨", GRAY),
}


def from_workflow_run(repo: str, run: dict[str, Any]) -> Post | None:
    """`/repos/{repo}/actions/runs` 항목 하나를 임베드로."""
    if run.get("status") != "completed":
        return None

    conclusion = (run.get("conclusion") or "").lower()
    if conclusion == "skipped":
        return None
    verb, color = _CONCLUSIONS.get(conclusion, ("완료", GRAY))

    actor = run.get("actor") or {}
    workflow = run.get("name") or "워크플로"
    title_line = run.get("display_title") or ""

    embed = discord.Embed(
        title=trim(f"CI {verb} · {workflow}", 256),
        url=run.get("html_url"),
        description=trim(escape_markdown(title_line), 300) or None,
        color=color,
        timestamp=parse_time(run.get("updated_at")),
    )
    embed.set_author(
        name=trim(f"{actor.get('login') or 'github'} · {repo}", 256),
        url=f"{GITHUB}/{repo}",
        icon_url=actor.get("avatar_url"),
    )

    branch = run.get("head_branch")
    if branch:
        embed.add_field(name="브랜치", value=f"`{branch}`", inline=True)

    sha = run.get("head_sha")
    if sha:
        embed.add_field(
            name="커밋",
            value=f"[`{str(sha)[:7]}`]({GITHUB}/{repo}/commit/{sha})",
            inline=True,
        )

    attempt = run.get("run_attempt")
    if isinstance(attempt, int) and attempt > 1:
        embed.add_field(name="재시도", value=f"{attempt}번째", inline=True)

    if color == RED:
        embed.set_footer(text="로그는 위 제목 링크에서 확인할 수 있습니다")

    return Post("ci", embed)
