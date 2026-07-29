"""환경변수 로딩과 검증.

Dishost 는 패널에서 환경변수를 직접 주입할 수 있으므로 .env 파일이 없어도
os.environ 만으로 동작해야 한다. load_dotenv 는 override=False 로 호출해서
이미 주입된 값을 덮어쓰지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """설정값이 없거나 형식이 잘못됐을 때."""


def _text(key: str) -> str | None:
    value = (os.getenv(key) or "").strip()
    return value or None


def _number(key: str, default: int, *, minimum: int) -> int:
    raw = _text(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} 는 정수여야 합니다 (현재 값: {raw!r})") from None
    if value < minimum:
        raise ConfigError(f"{key} 는 {minimum} 이상이어야 합니다 (현재 값: {value})")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    github_token: str | None
    guild_id: int | None
    poll_interval: int
    db_path: Path
    secret_key: str | None

    @classmethod
    def load(cls) -> Config:
        load_dotenv(ROOT / ".env", override=False)

        token = _text("DISCORD_TOKEN")
        if token is None:
            raise ConfigError(
                "DISCORD_TOKEN 이 없습니다. .env.example 을 .env 로 복사한 뒤 봇 토큰을 "
                "채워주세요. (Dishost 에서는 패널의 환경변수 탭에 넣으면 됩니다)"
            )

        data_dir = ROOT / (_text("DATA_DIR") or "data")
        return cls(
            discord_token=token,
            github_token=_text("GITHUB_TOKEN"),
            guild_id=_number("GUILD_ID", 0, minimum=0) or None,
            poll_interval=_number("POLL_INTERVAL_SECONDS", 90, minimum=30),
            db_path=data_dir / "gitcord.db",
            secret_key=_text("GITCORD_SECRET_KEY"),
        )
