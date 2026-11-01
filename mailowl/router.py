"""通知路由：按等级决定飞书即时卡片 / 每日摘要 / Bark。

| 等级 | 飞书            | Bark                                   |
| 0    | 每日摘要        | 不推送                                 |
| 1    | 摘要（可配即时）| 不推送                                 |
| 2    | 即时卡片        | level2_push（默认 active，夜间静默）    |
| 3    | 即时卡片+待办   | timeSensitive，无人处理则再次响铃       |
"""
from __future__ import annotations

from .bark import in_quiet_hours
from .config import Config

STALE_SECONDS = 72 * 3600  # 停机期间积压的旧邮件只发卡片不响铃


def route_for(cfg: Config, level: int) -> str:
    if level >= 2:
        return "immediate"
    if level == 1:
        return "immediate" if cfg.routing.level1 == "immediate" else "digest"
    return "digest" if cfg.routing.digest_include_level0 else "none"


def bark_level(cfg: Config, level: int, date_ts: int, now_ts: int) -> str | None:
    """返回 Bark 推送级别；None 表示不推送。"""
    if level < 2 or now_ts - (date_ts or now_ts) > STALE_SECONDS:
        return None
    if level >= 3:
        return "timeSensitive"
    push = cfg.bark.level2_push
    if push == "none" or in_quiet_hours(cfg.bark.quiet_hours, cfg.timezone):
        return None
    return push
