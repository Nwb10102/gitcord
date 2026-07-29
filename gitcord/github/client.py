"""GitHub REST 클라이언트.

폴링 전용 봇이라 요청 절약이 설계의 중심이다.

- **ETag 조건부 요청**: 변경이 없으면 304 가 오고, 304 는 rate limit 을 소모하지
  않는다. 저장소 하나를 90초마다 찔러도 실제 소모는 커밋이 있을 때뿐이다.
- **X-Poll-Interval**: GitHub 이 응답 헤더로 권장 최소 주기를 알려준다. 우리
  설정보다 길면 그쪽을 따른다.
- **rate limit 소진**: 남은 횟수가 0이면 RateLimited 에 reset 시각을 담아 올린다.
  호출자는 그 시각까지 폴링 자체를 멈춘다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

API = "https://api.github.com"
USER_AGENT = "Gitcord-Discord-Bot"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RepoNotFound(GitHubError):
    def __init__(self, repo: str) -> None:
        super().__init__(
            404,
            f"`{repo}` 저장소를 찾을 수 없습니다. 이름을 확인하거나, 비공개 "
            "저장소라면 GITHUB_TOKEN 에 접근 권한이 있는지 확인해주세요.",
        )
        self.repo = repo


class GitHubAuthError(GitHubError):
    def __init__(self) -> None:
        super().__init__(401, "GITHUB_TOKEN 이 유효하지 않습니다. 토큰을 다시 발급해주세요.")


class RateLimited(GitHubError):
    def __init__(self, reset_at: float) -> None:
        wait = max(0, int(reset_at - time.time()))
        super().__init__(
            429, f"GitHub API 호출 한도를 모두 썼습니다. 약 {wait}초 뒤 재개합니다."
        )
        self.reset_at = reset_at


@dataclass(frozen=True, slots=True)
class EventsPage:
    """`/repos/{repo}/events` 응답.

    events 가 None 이면 304 — 지난번 이후 변경이 없다는 뜻이다.
    """

    events: list[dict[str, Any]] | None
    etag: str | None
    poll_interval: int


def normalize_repo(value: str) -> str:
    """사용자 입력을 `owner/name` 소문자로 정규화한다.

    전체 URL(`https://github.com/owner/name`), `.git` 접미사, 앞뒤 슬래시를
    모두 받아준다. 저장 키로 쓰므로 소문자로 통일한다 — GitHub 자체가
    저장소 이름을 대소문자 구분 없이 취급한다.

    Raises:
        ValueError: 형식이 맞지 않을 때.
    """
    text = value.strip()
    if not text:
        raise ValueError("저장소 이름이 비어 있습니다.")

    if "://" in text or text.lower().startswith("github.com/"):
        if "://" not in text:
            text = "https://" + text
        path = urlparse(text).path
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(f"`{value}` 에서 owner/name 을 읽을 수 없습니다.")
        text = f"{parts[0]}/{parts[1]}"

    text = text.strip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]

    if not _REPO_RE.match(text):
        raise ValueError(
            f"`{value}` 는 저장소 형식이 아닙니다. `owner/name` 또는 저장소 URL 로 "
            "입력해주세요."
        )
    return text.lower()


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._session = aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=20)
        )

    async def close(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    # ── 저수준 ──────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        etag: str | None = None,
        accept: str | None = None,
    ) -> tuple[int, Any, Mapping[str, str]]:
        if self._session is None:
            raise RuntimeError("GitHubClient.start() 를 먼저 호출해야 합니다")

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if accept:
            headers["Accept"] = accept

        async with self._session.get(
            f"{API}{path}", params=params, headers=headers
        ) as response:
            status = response.status
            # aiohttp 의 헤더는 대소문자를 무시하지만 dict() 로 바꾸는 순간 그
            # 성질을 잃는다. GitHub 은 `ETag` 를 대문자로 보내므로 그대로
            # dict 화하면 .get("etag") 가 None 이 되어 조건부 요청이 통째로
            # 무력화된다. 키를 소문자로 눕혀서 넘긴다.
            response_headers = {k.lower(): v for k, v in response.headers.items()}

            if status == 304:
                return status, None, response_headers
            if status == 401:
                raise GitHubAuthError()
            if status in (403, 429):
                # 한도 소진과 권한 부족을 구분한다.
                if response_headers.get("x-ratelimit-remaining") == "0":
                    reset = response_headers.get("x-ratelimit-reset")
                    raise RateLimited(float(reset) if reset else time.time() + 60)
                raise GitHubError(status, await self._error_message(response, status))
            if status == 404:
                raise GitHubError(404, "요청한 리소스를 찾을 수 없습니다.")
            if status >= 400:
                raise GitHubError(status, await self._error_message(response, status))

            if accept and "json" not in accept:
                return status, await response.text(), response_headers
            return status, await response.json(), response_headers

    @staticmethod
    async def _error_message(response: aiohttp.ClientResponse, status: int) -> str:
        try:
            payload = await response.json()
        except Exception:
            return f"GitHub API 오류 (HTTP {status})"
        message = payload.get("message") if isinstance(payload, dict) else None
        return f"GitHub API 오류 (HTTP {status}): {message or '알 수 없는 오류'}"

    # ── 고수준 ──────────────────────────────────────────────

    async def get_repo(self, repo: str) -> dict[str, Any]:
        try:
            _, data, _ = await self._get(f"/repos/{repo}")
        except GitHubError as exc:
            if exc.status == 404:
                raise RepoNotFound(repo) from None
            raise
        return data

    async def repo_events(self, repo: str, *, etag: str | None = None) -> EventsPage:
        """저장소 활동 이벤트. push/PR/issue/release/branch 를 한 번에 받는다."""
        try:
            status, data, headers = await self._get(
                f"/repos/{repo}/events", params={"per_page": 30}, etag=etag
            )
        except GitHubError as exc:
            if exc.status == 404:
                raise RepoNotFound(repo) from None
            raise

        raw_interval = headers.get("x-poll-interval")
        try:
            poll_interval = int(raw_interval) if raw_interval else 60
        except ValueError:
            poll_interval = 60

        return EventsPage(
            events=None if status == 304 else list(data or []),
            # 304 는 ETag 를 다시 주지 않으므로 기존 값을 유지한다.
            etag=headers.get("etag") or etag,
            poll_interval=poll_interval,
        )

    async def workflow_runs(self, repo: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """GitHub Actions 실행 목록.

        workflow_run 은 활동 이벤트 API 에 포함되지 않아서 별도로 받아야 한다.
        CI 를 구독한 저장소에만 호출한다.
        """
        try:
            _, data, _ = await self._get(
                f"/repos/{repo}/actions/runs",
                params={"per_page": limit, "exclude_pull_requests": "true"},
            )
        except GitHubError as exc:
            if exc.status == 404:
                # Actions 를 한 번도 안 쓴 저장소는 404 대신 빈 목록으로 취급한다.
                return []
            raise
        return list((data or {}).get("workflow_runs") or [])

    async def compare(self, repo: str, base: str, head: str) -> dict[str, Any]:
        """두 커밋 사이의 변경. AI 요약용 diff 를 여기서 얻는다."""
        _, data, _ = await self._get(f"/repos/{repo}/compare/{base}...{head}")
        return data
