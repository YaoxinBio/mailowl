"""极简 MCP stdio 服务器（JSON-RPC 2.0，按行分隔），把 tools.MailTools 暴露给 claude -p。

不依赖 mcp 包：只需 initialize / tools/list / tools/call / ping 四个方法。
由 llm.ClaudeCLI.mcp_config() 生成的配置启动，环境变量 MAILOWL_SCOPE 标明本次对话归属，
save_draft / create_todo 写入的记录据此回到对应的飞书会话。
"""
from __future__ import annotations

import json
import logging
import os
import sys

from .config import load_config
from .db import DB
from .tools import MailTools

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def serve(tools: MailTools, stdin=sys.stdin, stdout=sys.stdout) -> None:
    def reply(msg_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method, msg_id, params = req.get("method"), req.get("id"), req.get("params") or {}
        if msg_id is None:  # 通知，无需回复
            continue
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                reply(msg_id, {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mail", "version": "0.1.0"},
                })
            elif method == "ping":
                reply(msg_id, {})
            elif method == "tools/list":
                reply(msg_id, {"tools": tools.specs()})
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                try:
                    text = tools.call(name, args)
                    is_error = '"error"' in text[:20]
                except TypeError as e:
                    text, is_error = f"参数错误: {e}", True
                except Exception as e:
                    logging.exception("tool %s failed", name)
                    text, is_error = f"{type(e).__name__}: {e}", True
                reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
            else:
                reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as e:  # 防御：任何意外都要回一个错误而不是让客户端挂起
            reply(msg_id, error={"code": -32603, "message": str(e)})


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    cfg = load_config()
    db = DB(cfg.db_path)
    serve(MailTools(cfg, db, os.environ.get("MAILOWL_SCOPE", "")))


if __name__ == "__main__":
    main()
