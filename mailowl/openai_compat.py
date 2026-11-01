"""OpenAI 兼容接口后端：DeepSeek、通义千问、Kimi、智谱、OpenAI、OpenRouter、本地 Ollama 等。

与 ClaudeCLI 接口相同：structured()（分级）、agent()（对话，自己跑工具循环）、research()（联网研究）。
- 工具直接在进程内调用 MailTools，不经 MCP。
- 对话历史存在 agent_history 表；裁剪时按"用户消息"成组丢弃最早的轮次，保证带 tool_calls 的
  助手消息和它的 tool 回复不会被拆开（拆开会被接口拒绝）。
- 结构化输出用 response_format=json_object（各家都支持），schema 写进提示词，回来再校验。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable

import httpx

from .config import Config, lang_directive
from .db import DB, now
from .llm import RESEARCH_SYSTEM, AgentResult, LLMError, SessionMissing
from .tools import MailTools
from .web import WebError, web_fetch, web_search

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


def is_local_url(url: str) -> bool:
    """本机或局域网里的模型服务（Ollama、LM Studio 等）：不走 http(s)_proxy / all_proxy。"""
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost",) or host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local
TOOL_RESULT_MAX = 20000

Executor = Callable[[str, dict[str, Any]], Awaitable[str]]


def extract_json(text: str) -> dict[str, Any]:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise LLMError(f"模型没有返回 JSON：{text[:200]}")
        t = t[i:j + 1]
    try:
        out = json.loads(t)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型返回的 JSON 无法解析：{text[:200]}") from e
    if not isinstance(out, dict):
        raise LLMError("模型返回的 JSON 不是对象")
    return out


def conform(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """按 schema 补齐/纠正常见的小偏差（数字写成字符串、布尔写成 "true" 等），缺必填字段则报错。"""
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise LLMError(f"模型输出缺少字段：{', '.join(missing)}")
    out = {}
    for k, spec in props.items():
        if k not in data:
            continue
        v, typ = data[k], spec.get("type")
        types = typ if isinstance(typ, list) else [typ]
        if v is None and "null" in types:
            out[k] = None
        elif "integer" in types and not isinstance(v, bool):
            try:
                v = int(float(v))
            except (TypeError, ValueError) as e:
                raise LLMError(f"字段 {k} 不是整数：{v!r}") from e
            lo, hi = spec.get("minimum"), spec.get("maximum")
            out[k] = max(lo, v) if lo is not None else v
            out[k] = min(hi, out[k]) if hi is not None else out[k]
        elif "boolean" in types:
            out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "是")
        elif "array" in types:
            out[k] = v if isinstance(v, list) else ([] if v in (None, "") else [v])
        elif "string" in types:
            out[k] = v if isinstance(v, str) or v is None else str(v)
            if "enum" in spec and out[k] not in spec["enum"]:
                out[k] = spec["enum"][-1]
        else:
            out[k] = v
    return out


def trim_history(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for m in messages:
        if m.get("role") == "user" or not groups:
            groups.append([])
        groups[-1].append(m)
    size = lambda g: sum(len(json.dumps(x, ensure_ascii=False)) for x in g)
    while len(groups) > 1 and sum(size(g) for g in groups) > max_chars:
        groups.pop(0)
    return [m for g in groups for m in g]


def to_openai_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                              "parameters": s.get("inputSchema") or s.get("parameters")}}
            for s in specs]


RESEARCH_TOOLS = [
    {"name": "web_search", "description": "搜索网页，返回标题、网址和摘要。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "web_fetch", "description": "打开一个网页，返回正文文字。",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]


class OpenAIChat:
    def __init__(self, cfg: Config, db: DB, transport: httpx.AsyncBaseTransport | None = None, *,
                 base_url: str | None = None, api_key: str | None = None, triage_model: str | None = None):
        self.cfg, self.db = cfg, db
        self.base = (base_url or cfg.llm.base_url).rstrip("/")
        self.api_key = cfg.llm.api_key if api_key is None else api_key
        self.triage_model = triage_model or cfg.llm.triage_model
        self.transport = transport          # 测试用

    # ---------------------------------------------------------------- HTTP
    async def chat(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   tool_choice: str | None = None, json_mode: bool = False, timeout: int = 180,
                   kind: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last = ""
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=self.transport,
                                             trust_env=not is_local_url(self.base)) as c:
                    r = await c.post(f"{self.base}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code < 400:
                    data = r.json()
                    try:
                        msg = data["choices"][0]["message"]
                    except (KeyError, IndexError, TypeError) as e:
                        raise LLMError(f"模型接口返回格式不对：{str(data)[:300]}") from e
                    u = data.get("usage") or {}
                    tokens = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
                    self.db.log_action("llm_tokens", None, {"tokens": tokens, "kind": kind, "model": model})
                    return msg
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in RETRY_STATUS:
                    break
            await asyncio.sleep(2 * (attempt + 1))
        raise LLMError(f"模型接口调用失败（{self.base}）：{last}")

    async def tool_loop(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], run_tool: Executor, *,
                        model: str, timeout: int, kind: str) -> tuple[str, list[dict[str, Any]], int]:
        """跑工具循环，返回 (最终回答, 本轮新增消息, 调用次数)。messages 会被原地追加。"""
        new: list[dict[str, Any]] = []
        rounds = 0
        while True:
            last_round = rounds >= self.cfg.llm.max_tool_rounds
            if last_round:
                note = {"role": "user", "content": "（工具调用次数已达上限，请直接根据已有信息回答。）"}
                messages.append(note)
                new.append(note)
            msg = await self.chat(model=model, messages=messages, tools=tools,
                                  tool_choice="none" if last_round else None, timeout=timeout, kind=kind)
            rounds += 1
            calls = [] if last_round else (msg.get("tool_calls") or [])
            am: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                am["tool_calls"] = [{"id": c.get("id") or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                     "function": {"name": c["function"]["name"],
                                                  "arguments": c["function"].get("arguments") or "{}"}}
                                    for c in calls]
            messages.append(am)
            new.append(am)
            if not calls:
                return (am["content"] or "").strip(), new, rounds
            for c in am["tool_calls"]:
                name, raw = c["function"]["name"], c["function"]["arguments"]
                try:
                    args = json.loads(raw) if raw.strip() else {}
                    if not isinstance(args, dict):
                        raise ValueError("参数不是 JSON 对象")
                    result = await run_tool(name, args)
                except Exception as e:  # 参数坏了或工具出错：告诉模型，让它自己改
                    result = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                tm = {"role": "tool", "tool_call_id": c["id"], "content": result[:TOOL_RESULT_MAX]}
                messages.append(tm)
                new.append(tm)

    # ---------------------------------------------------------------- 三个入口
    async def structured(self, prompt: str, schema: dict, *, system: str, model: str | None = None,
                         timeout: int | None = None) -> tuple[dict[str, Any], float]:
        sys_prompt = (f"{system}\n\n只输出一个 JSON 对象（json），不要输出任何其他文字。"
                      f"它必须符合这个 JSON Schema：\n{json.dumps(schema, ensure_ascii=False)}")
        msg = await self.chat(model=model or self.triage_model, json_mode=True, kind="triage",
                              messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
                              timeout=timeout or self.cfg.llm.triage_timeout)
        return conform(extract_json(msg.get("content") or ""), schema), 0.0

    async def agent(self, prompt: str, *, scope: str, session_id: str | None, system: str,
                    model: str | None = None, timeout: int | None = None) -> AgentResult:
        history: list[dict[str, Any]] = []
        if session_id:
            row = self.db.q1("SELECT messages FROM agent_history WHERE session_id=?", (session_id,))
            if row is None:
                raise SessionMissing(f"没有会话 {session_id}")
            history = json.loads(row["messages"])
        else:
            session_id = str(uuid.uuid4())
        tools = MailTools(self.cfg, self.db, scope)

        async def run_tool(name: str, args: dict[str, Any]) -> str:
            return await asyncio.to_thread(tools.call, name, args)

        user = {"role": "user", "content": prompt}
        messages = [{"role": "system", "content": system}, *history, user]
        text, new, rounds = await self.tool_loop(messages, to_openai_tools(tools.specs()), run_tool,
                                                 model=model or self.cfg.llm.agent_model,
                                                 timeout=timeout or self.cfg.llm.agent_timeout, kind="agent")
        kept = trim_history([*history, user, *new], self.cfg.llm.history_max_chars)
        self.db.x("INSERT INTO agent_history(session_id,scope,messages,updated_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(session_id) DO UPDATE SET messages=excluded.messages, updated_at=excluded.updated_at",
                  (session_id, scope, json.dumps(kept, ensure_ascii=False), now()))
        return AgentResult(text=text, session_id=session_id, cost_usd=0.0, turns=rounds)

    async def research(self, question: str, *, timeout: int | None = None) -> str:
        key = self.cfg.llm.tavily_api_key
        specs = RESEARCH_TOOLS if key else RESEARCH_TOOLS[1:]

        async def run_tool(name: str, args: dict[str, Any]) -> str:
            try:
                if name == "web_search" and key:
                    res = await web_search(str(args.get("query", "")), key)
                elif name == "web_fetch":
                    res = await web_fetch(str(args.get("url", "")))
                else:
                    res = {"error": f"未知工具 {name}"}
            except WebError as e:
                res = {"error": str(e)}
            return json.dumps(res, ensure_ascii=False)

        messages = [{"role": "system", "content": f"{RESEARCH_SYSTEM}\n{lang_directive(self.cfg)}"},
                    {"role": "user", "content": question}]
        text, _, _ = await self.tool_loop(messages, to_openai_tools(specs), run_tool,
                                          model=self.cfg.llm.research_model or self.cfg.llm.agent_model,
                                          timeout=timeout or self.cfg.llm.agent_timeout, kind="research")
        return text
