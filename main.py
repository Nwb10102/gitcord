"""Gitcord 엔트리포인트.

Dishost 는 이 파일을 실행 대상으로 지정하면 된다.
로컬에서는 `python main.py`.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from gitcord.bot import Gitcord
from gitcord.config import Config, ConfigError


def main() -> int:
    discord.utils.setup_logging(level=logging.INFO)
    log = logging.getLogger("gitcord")

    try:
        config = Config.load()
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 1

    bot = Gitcord(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "DISCORD_TOKEN 이 유효하지 않습니다. "
            "Developer Portal 에서 토큰을 다시 발급받아 넣어주세요."
        )
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
