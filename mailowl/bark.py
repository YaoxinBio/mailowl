"""Bark 推送（iPhone 高优先级提醒）。"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import httpx

from .config import BarkConfig

log = logging.getLogger(__name__)

LEVEL_ORDER = {"passive": 0, "active": 1, "timeSensitive": 2, "critical": 3}


def in_quiet_hours(spec: str, tz: str, when: dt.datetime | None = None) -> bool:
    if not spec or "-" not in spec:
        return False
    start_s, end_s = spec.split("-", 1)
    when = when or dt.datetime.now(ZoneInfo(tz))
    t = when.hour * 60 + when.minute
    sh, sm = map(int, start_s.strip().split(":"))
    eh, em = map(int, end_s.strip().split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    return start <= t < end if start <= end else (t >= start or t < end)


@dataclass
class BarkItem:
    title: str
    body: str
    level: str = "active"
    mail_id: int | None = None
    call: bool = False


@dataclass
class Bark:
    cfg: BarkConfig
    click_url: str = ""
    sent: list[dict] = field(default_factory=list)  # 测试用

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.device_keys)

    async def push(self, title: str, body: str, *, level: str = "active", call: bool = False,
                   url: str | None = None, subtitle: str = "") -> bool:
        if not self.enabled:
            log.info("Bark 未配置，跳过：%s", title)
            return False
        if self.cfg.content == "minimal":           # 不让任何邮件内容经过 Bark 服务器和苹果推送
            title, body, subtitle = "MailOwl", "有新的提醒，点开在飞书里查看", ""
        ok = True
        async with httpx.AsyncClient(timeout=15) as client:
            for key in self.cfg.device_keys:
                payload = {"device_key": key, "title": title[:120], "body": body[:900], "level": level,
                           "group": self.cfg.group, "isArchive": "1"}
                if subtitle:
                    payload["subtitle"] = subtitle[:120]
                if url or self.click_url:
                    payload["url"] = url or self.click_url
                if self.cfg.icon:
                    payload["icon"] = self.cfg.icon
                if call:
                    payload["call"] = "1"
                try:
                    r = await client.post(self.cfg.server.rstrip("/") + "/push", json=payload)
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    if r.status_code != 200 or data.get("code", 200) != 200:
                        ok = False
                        log.warning("Bark 推送失败 %s: %s", r.status_code, r.text[:200])
                except httpx.HTTPError as e:
                    ok = False
                    log.warning("Bark 推送异常: %s", e)
        self.sent.append({"title": title, "body": body, "level": level, "call": call})
        return ok


def combine(items: list[BarkItem]) -> BarkItem:
    """把短时间内的多条提醒合并成一条，等级取最高。"""
    if len(items) == 1:
        return items[0]
    top = max(items, key=lambda i: LEVEL_ORDER.get(i.level, 1))
    body = "\n".join(f"• {i.title}" for i in items[:6])
    if len(items) > 6:
        body += f"\n…另有 {len(items) - 6} 封"
    return BarkItem(title=f"{len(items)} 封重要邮件", body=body, level=top.level,
                    call=any(i.call for i in items))
