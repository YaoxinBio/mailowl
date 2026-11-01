"""飞书自建应用机器人：WebSocket 长连接收消息与卡片回调，不需要公网 IP 和入站端口。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from .cards import simplify
from .config import FeishuConfig

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


def _code(res) -> str:
    err = getattr(res, "error", None)
    code = getattr(err, "code", None)
    return str(getattr(code, "value", code) or "")


class FeishuBot:
    """lark-channel-sdk 的事件处理跑在它自己的后台线程事件循环（lark-channel-bg）里，和主服务不是同一个循环。
    这里做双向桥接：入站事件立即转交主循环并返回（让 SDK 及时确认回调）；出站调用一律投递到 SDK 的循环执行。"""

    def __init__(self, cfg: FeishuConfig):
        from lark_channel import FeishuChannel, PolicyConfig, SecurityConfig
        from lark_channel.core.enum import LogLevel

        self.cfg = cfg
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self.channel = FeishuChannel(
            app_id=cfg.app_id, app_secret=cfg.app_secret, domain=cfg.domain,
            log_level=LogLevel.WARNING,
            policy=PolicyConfig(dm_policy="open", group_policy="disabled"),
            security=SecurityConfig(mode="audit"),
        )

    def on(self, event: str, handler: Handler) -> None:
        def bridge(ev):
            loop = self._main_loop
            if loop is None or loop.is_closed():
                log.warning("主循环未就绪，丢弃飞书事件 %s", event)
                return
            fut = asyncio.run_coroutine_threadsafe(handler(ev), loop)
            fut.add_done_callback(_log_failure)
        self.channel.on(event, bridge)

    async def _sdk(self, coro):
        return await asyncio.wrap_future(self.channel.schedule(coro))

    async def start(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        await self.channel.connect_until_ready(timeout=60)
        log.info("飞书长连接已建立")

    async def stop(self) -> None:
        try:
            await self.channel.disconnect()
        except Exception:
            pass

    @staticmethod
    def _opts(reply_to: str | None) -> dict | None:
        return {"reply_to": reply_to, "reply_target_gone": "fresh"} if reply_to else None

    async def send_card(self, to: str, card: dict, reply_to: str | None = None) -> str | None:
        res = await self._sdk(self.channel.send(to, {"card": card}, self._opts(reply_to)))
        if not res.success and _code(res) == "format_error":
            log.warning("卡片被拒（format_error），改发简化版: %s", getattr(res, "error", None))
            res = await self._sdk(self.channel.send(to, {"card": simplify(card)}, self._opts(reply_to)))
        if not res.success:
            log.error("发送卡片失败: %s", getattr(res, "error", None))
            return None
        return res.message_id

    async def update_card(self, message_id: str, card: dict) -> bool:
        res = await self._sdk(self.channel.update_card(message_id, card))
        if not res.success and _code(res) == "format_error":
            res = await self._sdk(self.channel.update_card(message_id, simplify(card)))
        if not res.success:
            log.warning("更新卡片失败 %s: %s", message_id, getattr(res, "error", None))
        return bool(res.success)

    async def send_markdown(self, to: str, text: str, reply_to: str | None = None) -> str | None:
        res = await self._sdk(self.channel.send(to, {"markdown": text}, self._opts(reply_to)))
        if not res.success:
            log.error("发送消息失败: %s", getattr(res, "error", None))
            return None
        return res.message_id

    async def download(self, message_id: str, file_key: str, resource_type: str) -> bytes | None:
        return await self._sdk(self.channel.download_resource(file_key, resource_type=resource_type,
                                                              message_id=message_id))

    async def typing(self, message_id: str) -> str | None:
        return await self._sdk(self.channel.add_typing_reaction(message_id))

    async def untyping(self, message_id: str, reaction_id: str | None) -> None:
        if reaction_id:
            await self._sdk(self.channel.remove_typing_reaction(message_id, reaction_id))


def _log_failure(fut: concurrent.futures.Future) -> None:
    if not fut.cancelled() and fut.exception() is not None:
        log.error("飞书事件处理异常", exc_info=fut.exception())


class FakeBot:
    """dry-run 与测试：把发出的东西记在内存，并追加写到 jsonl 便于查看。"""

    def __init__(self, outbox: Path | None = None):
        self.outbox = outbox
        self.sent: list[dict] = []
        self.handlers: dict[str, Handler] = {}
        self.files: dict[str, bytes] = {}
        self._n = 0

    def on(self, event: str, handler: Handler) -> None:
        self.handlers[event] = handler

    async def start(self) -> None:
        log.info("FakeBot 已启动（dry-run，不连接飞书）")

    async def stop(self) -> None:
        pass

    def _record(self, kind: str, **kw) -> str:
        self._n += 1
        mid = f"om_fake_{self._n}"
        item = {"kind": kind, "message_id": mid, **kw}
        self.sent.append(item)
        if self.outbox:
            with self.outbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return mid

    async def send_card(self, to, card, reply_to=None):
        return self._record("card", to=to, card=card, reply_to=reply_to)

    async def update_card(self, message_id, card):
        self._record("update", target=message_id, card=card)
        return True

    async def send_markdown(self, to, text, reply_to=None):
        return self._record("markdown", to=to, text=text, reply_to=reply_to)

    async def download(self, message_id, file_key, resource_type):
        return self.files.get(file_key)

    async def typing(self, message_id):
        return None

    async def untyping(self, message_id, reaction_id):
        return None
