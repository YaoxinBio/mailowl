"""模型后端与联网研究：Claude CLI 参数隔离、OpenAI 兼容接口的工具循环、研究审批流程、内网访问拦截。"""
import asyncio
import json
import socket

import httpx
import pytest

from mailowl.config import ConfigError, load_config
from mailowl.llm import ClaudeCLI, FakeLLM, LLMError, research_backend
from mailowl.openai_compat import OpenAIChat, conform, extract_json, trim_history
from mailowl.tools import MailTools
from mailowl.triage import SCHEMA
from mailowl.web import WebError, check_url, web_fetch
from tests.test_service import action, add_mail, bind_owner, make_service, run


# ---------------------------------------------------------------- Claude CLI：对话 Agent 不能上网
def test_claude_agent_has_no_web_and_research_has_no_mailbox(cfg, monkeypatch):
    seen = []

    async def fake_run(self, args, prompt, timeout):
        seen.append(args)
        return {"result": "ok", "session_id": "s1"}

    monkeypatch.setattr(ClaudeCLI, "_run", fake_run)
    cli = ClaudeCLI(cfg)
    run(cli.agent("hi", scope="mail:1", session_id=None, system="sys"))
    agent_args = seen[-1]
    assert agent_args[agent_args.index("--tools") + 1] == ""
    assert "WebFetch" not in agent_args and "WebSearch" not in agent_args
    assert agent_args[agent_args.index("--allowedTools") + 1] == "mcp__mail"
    run(cli.research("某会议 2027 截稿日期"))
    r_args = seen[-1]
    assert "--mcp-config" not in r_args and "--strict-mcp-config" in r_args
    assert r_args[r_args.index("--tools") + 1] == "WebSearch,WebFetch"


# ---------------------------------------------------------------- 研究审批流程
def test_research_needs_approval_and_feeds_back(cfg, db):
    cfg.llm.research_backend = "claude_cli"
    bind_owner(db)
    llm = FakeLLM(research_reply="ISMB 2027 截稿 1 月 20 日（来源 https://example.org）")
    svc, bot = make_service(cfg, db, llm)
    mid = add_mail(db, "1", subject="Call for papers")

    def on_agent(prompt, scope):
        if "[研究结果]" not in prompt:
            MailTools(cfg, db, scope).request_research("ISMB 2027 的投稿截止日期是哪天？")

    llm.on_agent = on_agent
    run(svc.run_agent(f"mail:{mid}", "这个会什么时候截稿？", None, mid))
    card = [s for s in bot.sent if s["kind"] == "card"][-1]
    body = json.dumps(card["card"], ensure_ascii=False)
    assert "要上网查资料吗" in body and "ISMB 2027" in body and '"research_go"' in body
    assert not [c for c in llm.calls if c[0] == "research"], "没同意之前不能上网"

    async def go():
        await svc.on_card_action(action({"a": "research_go", "r": 1}, message_id=card["message_id"]))
        await svc.on_card_action(action({"a": "research_go", "r": 1}, message_id=card["message_id"]))  # 连点两次
        while svc._bg:
            await asyncio.gather(*list(svc._bg))

    run(go())
    assert [c[1] for c in llm.calls if c[0] == "research"] == ["ISMB 2027 的投稿截止日期是哪天？"]
    follow = [c[1] for c in llm.calls if c[0] == "agent"][-1]
    assert "[研究结果]" in follow and "1 月 20 日" in follow
    r = db.q1("SELECT * FROM research_requests WHERE id=1")
    assert r["status"] == "done" and r["mail_id"] == mid
    done = [s for s in bot.sent if s["kind"] == "update" and s["target"] == card["message_id"]][-1]
    assert "完成" in json.dumps(done["card"], ensure_ascii=False)


def test_research_rejected_and_unavailable(cfg, db):
    cfg.llm.research_backend = "claude_cli"
    bind_owner(db)
    llm = FakeLLM()
    svc, bot = make_service(cfg, db, llm)
    MailTools(cfg, db, "chat:main").request_research("查一下")
    run(svc.post_research_requests("chat:main"))
    run(svc.on_card_action(action({"a": "research_no", "r": 1})))
    assert db.q1("SELECT status FROM research_requests")["status"] == "rejected"
    run(svc.on_card_action(action({"a": "research_go", "r": 1})))
    assert not [c for c in llm.calls if c[0] == "research"]
    cfg.llm.research_backend = "none"
    assert research_backend(cfg) is None
    assert "request_research" not in {s["name"] for s in MailTools(cfg, db, "chat:main").specs()}


def test_research_interrupted_by_restart_is_marked_failed(cfg, db):
    bind_owner(db)
    svc, bot = make_service(cfg, db)
    db.x("INSERT INTO research_requests(scope,question,status,card_msg_id,created_at) VALUES('chat:main','q','running','om_r',0)")
    run(svc.recover_research())
    assert db.q1("SELECT status FROM research_requests")["status"] == "failed"
    assert bot.sent[-1]["kind"] == "update" and bot.sent[-1]["target"] == "om_r"


# ---------------------------------------------------------------- OpenAI 兼容接口
def openai_cfg(cfg):
    cfg.llm.backend, cfg.llm.base_url, cfg.llm.api_key = "openai", "https://llm.test/v1", "sk-test"
    cfg.llm.triage_model = cfg.llm.agent_model = "deepseek-chat"
    return cfg


def scripted(replies, requests):
    """按顺序返回预设的 assistant 消息，记录每次请求体。"""
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        requests.append(body)
        msg = replies[min(len(requests), len(replies)) - 1]
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    return httpx.MockTransport(handler)


def test_openai_agent_tool_loop_and_history(cfg, db):
    openai_cfg(cfg)
    mid = add_mail(db, "1", subject="Need CV")
    reqs = []
    replies = [
        {"content": None, "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": "get_mail", "arguments": json.dumps({"mail_id": mid})}},
                                          {"id": "c2", "type": "function",
                                           "function": {"name": "search_mails", "arguments": "{bad json"}}]},
        {"content": "他们要 CV。"},
        {"content": "好的，第二轮。"},
    ]
    llm = OpenAIChat(cfg, db, transport=scripted(replies, reqs))
    res = run(llm.agent("这封要什么？", scope=f"mail:{mid}", session_id=None, system="sys"))
    assert res.text == "他们要 CV。" and res.turns == 2
    assert reqs[0]["model"] == "deepseek-chat" and {t["function"]["name"] for t in reqs[0]["tools"]} >= {"get_mail", "save_draft"}
    tool_msgs = [m for m in reqs[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
    assert "Need CV" in tool_msgs[0]["content"] and "error" in tool_msgs[1]["content"]
    res2 = run(llm.agent("还有呢？", scope=f"mail:{mid}", session_id=res.session_id, system="sys"))
    roles = [m["role"] for m in reqs[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool", "assistant", "user"]
    assert res2.text == "好的，第二轮。"
    assert db.q1("SELECT SUM(json_extract(detail,'$.tokens')) n FROM actions WHERE kind='llm_tokens'")["n"] == 45


def test_openai_tool_round_cap(cfg, db):
    openai_cfg(cfg).llm.max_tool_rounds = 2
    reqs = []
    loop_call = {"content": "", "tool_calls": [{"id": "x", "type": "function",
                                                "function": {"name": "list_todos", "arguments": "{}"}}]}
    llm = OpenAIChat(cfg, db, transport=scripted([loop_call, loop_call, {"content": "就这些。"}], reqs))
    res = run(llm.agent("hi", scope="chat:main", session_id=None, system="sys"))
    assert res.text == "就这些。" and reqs[-1]["tool_choice"] == "none"


def test_openai_structured_and_helpers(cfg, db):
    openai_cfg(cfg)
    reqs = []
    content = '```json\n{"level": "2", "category": "action", "summary": "交 CV", "analysis": "", "deadline": null,' \
              ' "needs_reply": "true", "action_items": "提交 CV", "tags": []}\n```'
    llm = OpenAIChat(cfg, db, transport=scripted([{"content": content}], reqs))
    out, cost = run(llm.structured("邮件……", SCHEMA, system="分拣"))
    assert out["level"] == 2 and out["needs_reply"] is True and out["action_items"] == ["提交 CV"]
    assert reqs[0]["response_format"] == {"type": "json_object"} and "json" in reqs[0]["messages"][0]["content"]
    with pytest.raises(LLMError):
        conform({"level": 1}, SCHEMA)
    assert extract_json('好的：{"a": 1} 以上') == {"a": 1}
    hist = [{"role": "user", "content": "x" * 50},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "y" * 50},
            {"role": "user", "content": "最新"}, {"role": "assistant", "content": "答"}]
    kept = trim_history(hist, 120)
    assert kept[0]["role"] == "user" and kept[0]["content"] == "最新"


def test_openai_http_error_raises(cfg, db):
    openai_cfg(cfg)
    t = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(LLMError, match="401"):
        run(OpenAIChat(cfg, db, transport=t).chat(model="m", messages=[]))


def test_openai_config_validation(cfg, tmp_path):
    p = cfg.path
    text = p.read_text("utf-8").replace("[general]", '[llm]\nbackend = "openai"\nbase_url = "https://api.deepseek.com/v1"\n'
                                                     'agent_web = true\n[general]', 1)
    p.write_text(text, "utf-8")
    with pytest.raises(ConfigError, match="deepseek-chat"):
        load_config(p)
    p.write_text(text.replace('agent_web = true', 'agent_web = true\ntriage_model = "deepseek-chat"\n'
                                                  'agent_model = "deepseek-chat"'), "utf-8")
    c = load_config(p)
    assert c.llm.backend == "openai" and c.llm.agent_model == "deepseek-chat"


# ---------------------------------------------------------------- web_fetch 拦截内网
def fake_resolve(table):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (table[host], port))]
    return resolve


def test_check_url_blocks_internal():
    res = fake_resolve({"example.org": "93.184.216.34", "proxied.org": "198.18.0.7", "evil.org": "10.0.0.5"})
    check_url("https://example.org/a", res)
    check_url("https://proxied.org/a", res)                     # TUN fake-ip 放行
    for bad in ("http://127.0.0.1/", "http://192.168.1.1/admin", "http://localhost:8080/", "http://nas.local/",
                "http://intranet/", "https://evil.org/", "file:///etc/passwd", "http://[::1]/", "http://198.18.0.1/"):
        with pytest.raises(WebError):
            check_url(bad, res)


def test_web_fetch_rechecks_redirects():
    res = fake_resolve({"example.org": "93.184.216.34"})

    def handler(request):
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/secret"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              text="<html><body><p>Deadline: Jan 20</p></body></html>")

    t = httpx.MockTransport(handler)
    out = run(web_fetch("https://example.org/page", transport=t, resolve=res))
    assert "Deadline: Jan 20" in out["content"]
    with pytest.raises(WebError):
        run(web_fetch("https://example.org/go", transport=t, resolve=res))
