"""알림 카테고리 정의.

레포별로 어떤 이벤트를 받을지 고르는 단위다. embeds 가 이벤트를 카테고리로
분류하고, watch 커맨드가 사용자 입력을 이 목록으로 검증하며, watcher 가
구독 설정과 대조해 전송 여부를 정한다. 세 곳이 같은 상수를 봐야 하므로
의존성 없는 별도 모듈로 뒀다.
"""

from __future__ import annotations

# 표시 순서가 곧 /watch list 와 도움말의 출력 순서다.
CATEGORIES: dict[str, str] = {
    "push": "커밋 푸시",
    "pr": "풀 리퀘스트",
    "issue": "이슈 · 댓글",
    "ci": "GitHub Actions",
    "release": "릴리스",
    "branch": "브랜치 · 태그",
    "star": "스타 · 포크",
}

# /watch add 에서 events 를 생략했을 때 켜지는 기본값.
# branch/star 는 잡음이 많아 기본에서 뺐다.
DEFAULT_CATEGORIES: tuple[str, ...] = ("push", "pr", "issue", "ci", "release")

# 사람들이 흔히 쓰는 다른 이름을 받아준다.
_ALIASES: dict[str, str] = {
    "commit": "push",
    "commits": "push",
    "pull_request": "pr",
    "pullrequest": "pr",
    "pulls": "pr",
    "issues": "issue",
    "comment": "issue",
    "comments": "issue",
    "actions": "ci",
    "workflow": "ci",
    "build": "ci",
    "releases": "release",
    "tag": "branch",
    "tags": "branch",
    "branches": "branch",
    "fork": "star",
    "stars": "star",
}


class UnknownCategory(ValueError):
    """알 수 없는 카테고리 이름."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def parse(raw: str) -> tuple[str, ...]:
    """쉼표/공백으로 구분된 문자열을 정규화된 카테고리 튜플로 바꾼다.

    "all" 은 전체를 의미한다. 중복은 제거하되 CATEGORIES 의 순서를 유지한다.
    """
    tokens = [t.strip().lower() for t in raw.replace(" ", ",").split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        return DEFAULT_CATEGORIES

    selected: set[str] = set()
    for token in tokens:
        if token in ("all", "전체", "*"):
            return tuple(CATEGORIES)
        name = _ALIASES.get(token, token)
        if name not in CATEGORIES:
            raise UnknownCategory(token)
        selected.add(name)
    return tuple(name for name in CATEGORIES if name in selected)


def label(names: tuple[str, ...] | list[str]) -> str:
    """사람이 읽을 수 있는 한 줄 요약."""
    if not names:
        return "없음"
    if len(names) == len(CATEGORIES):
        return "전체"
    return " · ".join(CATEGORIES.get(name, name) for name in names)
