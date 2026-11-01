import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mailowl import cards
from mailowl.bark import Bark
from mailowl.db import now
from mailowl.feishu import FakeBot
from mailowl.llm import FakeLLM
from mailowl.mailparse import parse_message
from mailowl.mcp_server import serve
from mailowl.service import Service
from mailowl.smtp_send import SendRejected, SendUncertain
from mailowl.tools import MailTools
from mailowl.triage import triage_mail
from tests.conftest import make_raw

OWNER = "ou_owner"


class FakeBark(Bark):
    async def push(self, title, body, *, level="active", call=False, url=None, subtitle=""):
        self.sent.append({"title": title, "body": body, "level": level, "call": call})
        return True


def add_mail(db, uid="1", status="open", **kw):
    rec = parse_message(make_raw(**kw))
    rec.update(account="work", folder="INBOX", remote_uid=uid, status=status)
    return db.insert_mail(rec)


def make_service(cfg, db, llm=None, smtp=None):
    bot = FakeBot()
    svc = Service(cfg, db, llm or FakeLLM(), bot, FakeBark(cfg.bark), smtp_fn=smtp or (lambda a, m: None))
    return svc, bot


def bind_owner(db):
    db.kv_set("owner_open_id", OWNER)
    db.kv_set("owner_chat_id", "oc_chat")


def inbound(text, sender=OWNER, reply_to=None, mid="om_in"):
    return NS(chat_type="p2p", sender_is_bot=False, body_text=text, content_text=text, sender_id=sender,
              chat_id="oc_chat", message_id=mid, reply_to_message_id=reply_to)


def action(value, operator=OWNER, message_id="om_card", form=None):
    return NS(operator=NS(open_id=operator), message_id=message_id,
              action=NS(value=value, form_value=form, tag="button"))


async def drain(svc):
    while svc._bg:
        await asyncio.gather(*list(svc._bg))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 分级规则
def test_triage_rules(cfg, db):
    llm = FakeLLM(triage={"level": 1, "category": "info", "summary": "s", "analysis": "a", "deadline": None,
                          "needs_reply": False, "action_items": [], "tags": []})
    boss = add_mail(db, "1", sender="Boss <boss@uni.edu>", msgid="<b@x>")
    spam = add_mail(db, "2", sender="ads@news.spam.com", msgid="<s@x>")
    secret = add_mail(db, "3", subject="审稿邀请", msgid="<r@x>")
    assert run(triage_mail(cfg, db, llm, boss))["level"] == 2
    assert run(triage_mail(cfg, db, llm, spam))["level"] == 0
    n_calls = len(llm.calls)
    assert run(triage_mail(cfg, db, llm, secret))["tags"] == ["本地处理"]
    assert len(llm.calls) == n_calls, "local_only 邮件不能发给模型"
    import datetime as dt
    soon = (dt.datetime.now() + dt.timedelta(hours=20)).strftime("%Y-%m-%d %H:%M")
    llm.triage_result.update(level=2, deadline=soon)
    urgent = add_mail(db, "4", msgid="<u@x>")
    assert run(triage_mail(cfg, db, llm, urgent))["level"] == 3


def test_triage_fallback_on_llm_error(cfg, db):
    class Broken(FakeLLM):
        async def structured(self, *a, **k):
            raise RuntimeError("boom")
    mid = add_mail(db, "1", sender="boss@uni.edu")
    r = run(triage_mail(cfg, db, Broken(), mid))
    assert r["level"] == 2 and "未经模型分析" in r["tags"]


# ---------------------------------------------------------------- 绑定与权限
def test_pairing_and_owner_only(cfg, db):
    svc, bot = make_service(cfg, db)
    code = svc.pairing_code()
    run(svc.on_message(inbound("hello", sender="ou_x")))
    assert "尚未绑定" in bot.sent[-1]["text"]
    run(svc.on_message(inbound(code, sender="ou_me")))
    assert db.kv_get("owner_open_id") == "ou_me" and "绑定成功" in bot.sent[-1]["text"]
    run(svc.on_message(inbound("hi", sender="ou_stranger")))
    assert "私人" in bot.sent[-1]["text"]
    d = db.create_draft(account="work", mail_id=None, to_addrs="a@x.com", cc_addrs="", subject="s", body="b",
                        in_reply_to=None, refs=None, origin_scope="t")
    sent = []
    svc.smtp_fn = lambda a, m: sent.append(m)
    run(svc.on_card_action(action({"a": "send", "d": d["id"]}, operator="ou_stranger")))
    assert sent == [] and db.draft(d["id"])["status"] == "draft"


# ---------------------------------------------------------------- 新邮件 → 卡片 + Bark
def test_new_mail_card_and_bark(cfg, db):
    bind_owner(db)
    llm = FakeLLM(triage={"level": 2, "category": "action", "summary": "请补交 CV", "analysis": "需要回复",
                          "deadline": None, "needs_reply": True, "action_items": ["更新 CV"], "tags": ["涉及附件"]})
    svc, bot = make_service(cfg, db, llm)
    mid = add_mail(db, "1", subject="Additional Information Required", date="Sat, 31 Oct 2026 10:00:00 +0800")
    db.update_mail(mid, date_ts=now())
    run(svc.process_mail(mid))
    card = bot.sent[-1]
    assert card["kind"] == "card" and card["to"] == "oc_chat"
    assert db.feishu_binding(card["message_id"])["mail_id"] == mid
    assert db.mail(mid)["notified_at"]
    run(svc.flush_bark())
    assert svc.bark.sent and svc.bark.sent[0]["level"] == "active"
    # 需要处理且有明确事项：自动建待办，卡片上显示，并可在卡片上直接完成
    todo = db.q1("SELECT * FROM todos WHERE mail_id=?", (mid,))
    assert todo["title"] == "更新 CV" and todo["origin_scope"] == "auto" and todo["status"] == "open"
    body = json.dumps(card["card"], ensure_ascii=False)
    assert "已建待办：更新 CV" in body and '"todo_done"' in body and '"a": "todo"' not in body
    run(svc.on_card_action(action({"a": "todo_done", "t": todo["id"]}, message_id=card["message_id"])))
    assert db.q1("SELECT status FROM todos WHERE id=?", (todo["id"],))["status"] == "done"
    upd = bot.sent[-1]
    assert upd["kind"] == "update" and upd["target"] == card["message_id"]
    assert '"a": "todo"' in json.dumps(upd["card"], ensure_ascii=False), "完成后邮件卡片恢复「创建待办」按钮"


def test_level2_without_tasks_gets_no_auto_todo(cfg, db):
    bind_owner(db)
    llm = FakeLLM(triage={"level": 2, "category": "info", "summary": "合作者分享了数据", "analysis": "",
                          "deadline": None, "needs_reply": False, "action_items": [], "tags": []})
    svc, bot = make_service(cfg, db, llm)
    mid = add_mail(db, "1")
    run(svc.process_mail(mid))
    assert db.q1("SELECT COUNT(*) n FROM todos")["n"] == 0


def test_level1_goes_to_digest(cfg, db):
    bind_owner(db)
    svc, bot = make_service(cfg, db)
    a = add_mail(db, "1", subject="组会通知")
    run(svc.process_mail(a))
    assert db.mail(a)["route"] == "digest" and not bot.sent
    run(svc.send_digest(title="摘要"))
    assert bot.sent[-1]["kind"] == "card" and db.mail(a)["digested_at"]
    n = len(bot.sent)
    run(svc.send_digest(title="摘要"))
    assert len(bot.sent) == n, "没有新邮件时定时摘要不发"


# ---------------------------------------------------------------- 发信：只发一次
def test_send_once_even_if_clicked_twice(cfg, db):
    bind_owner(db)
    sent = []
    svc, bot = make_service(cfg, db, smtp=lambda a, m: sent.append(m))
    mid = add_mail(db, "1", sender="Alice <alice@x.com>", msgid="<orig@x>", subject="Question")
    tools = MailTools(cfg, db, "mail:%d" % mid)
    d1 = tools.save_draft(mail_id=mid, body="v1")
    d2 = tools.save_draft(mail_id=mid, body="v2 shorter")
    assert d2["version"] == 2
    run(svc.on_card_action(action({"a": "send", "d": d1["draft_id"]})))
    assert sent == [], "旧版本不能发送"
    run(svc.on_card_action(action({"a": "send", "d": d2["draft_id"]})))
    run(svc.on_card_action(action({"a": "send", "d": d2["draft_id"]})))
    assert len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "alice@x.com" and msg["In-Reply-To"] == "<orig@x>" and msg["Subject"] == "Re: Question"
    assert db.draft(d2["draft_id"])["status"] == "sent"
    assert db.mail(mid)["status"] == "handled"
    out = db.q1("SELECT * FROM mails WHERE direction='out'")
    assert out and out["thread_key"] == "<orig@x>"
    assert db.sender_stats("alice@x.com")["replied"] == 1


def test_send_rejected_then_uncertain(cfg, db):
    bind_owner(db)
    mid = add_mail(db, "1", sender="alice@x.com")
    d = MailTools(cfg, db, "t").save_draft(mail_id=mid, body="hello")
    calls = []

    def reject(a, m):
        calls.append(1)
        raise SendRejected("550 no")
    svc, bot = make_service(cfg, db, smtp=reject)
    run(svc.send_draft(d["draft_id"], "om_card"))
    assert db.draft(d["draft_id"])["status"] == "failed"

    def uncertain(a, m):
        calls.append(2)
        raise SendUncertain("timeout after DATA")
    svc.smtp_fn = uncertain
    run(svc.send_draft(d["draft_id"], "om_card"))        # failed 可以重试
    assert db.draft(d["draft_id"])["status"] == "unknown"
    run(svc.send_draft(d["draft_id"], "om_card"))        # unknown 不能自动重试
    assert calls == [1, 2]
    last_update = [s for s in bot.sent if s["kind"] == "update"][-1]
    assert "结果不确定" in last_update["card"]["header"]["title"]["content"]


# ---------------------------------------------------------------- 对话：回复卡片即针对该邮件
def test_reply_to_card_runs_agent_in_mail_scope_and_posts_draft(cfg, db):
    bind_owner(db)
    llm = FakeLLM(reply="草稿已写好，请审阅。")
    svc, bot = make_service(cfg, db, llm)
    mid = add_mail(db, "1", sender="alice@x.com", subject="Question")
    db.bind_feishu("om_mailcard", "mail_card", mid)

    def agent_side_effect(prompt, scope):
        assert scope == f"mail:{mid}"
        MailTools(cfg, db, scope).save_draft(mail_id=mid, body="Dear Alice, ...")
    llm.on_agent = agent_side_effect

    async def go():
        await svc.on_message(inbound("帮我起草回复", reply_to="om_mailcard"))
        await drain(svc)
    run(go())
    kinds = [s["kind"] for s in bot.sent]
    assert "markdown" in kinds and kinds[-1] == "card"
    draft_msg = bot.sent[-1]
    assert db.feishu_binding(draft_msg["message_id"])["draft_id"]
    assert db.session(f"mail:{mid}") is not None
    assert f"#{mid}" in llm.calls[-1][1]

    # 第二轮：表单修改意见 → 同一会话
    did = db.feishu_binding(draft_msg["message_id"])["draft_id"]
    llm.on_agent = lambda p, s: MailTools(cfg, db, s).save_draft(mail_id=mid, body="Dear Alice, shorter")

    async def go2():
        await svc.on_card_action(action({"a": "revise", "d": did}, message_id=draft_msg["message_id"],
                                        form={f"instruction_{did}": "缩短一些"}))
        await drain(svc)
    run(go2())
    assert "缩短一些" in llm.calls[-1][1]
    updates = [s for s in bot.sent if s["kind"] == "update" and s["target"] == draft_msg["message_id"]]
    assert updates and "取代" in updates[-1]["card"]["header"]["title"]["content"]


# ---------------------------------------------------------------- MCP 与卡片
def test_mcp_server_roundtrip(cfg, db):
    mid = add_mail(db, "1", subject="基金申请材料", body="请提交简历")
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_mails", "arguments": {"query": "申请材料"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    ]
    out = io.StringIO()
    serve(MailTools(cfg, db, "t"), io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
    res = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [r["id"] for r in res] == [1, 2, 3, 4]
    assert res[0]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in res[1]["result"]["tools"]}
    assert "save_draft" in names and not any(n.startswith("send") for n in names)
    payload = json.loads(res[2]["result"]["content"][0]["text"])
    assert payload["mails"][0]["id"] == mid
    assert res[3]["result"]["isError"]


def test_cards_are_valid_json_and_simplify(db, cfg):
    mid = add_mail(db, "1")
    db.update_mail(mid, level=2, summary="s", triage=json.dumps({"tags": ["a"]}))
    c = cards.mail_card(db.mail(mid), "工作邮箱")
    assert c["schema"] == "2.0" and c["config"]["update_multi"] is True
    d = db.create_draft(account="work", mail_id=mid, to_addrs="a@x.com", cc_addrs="", subject="s", body="b",
                        in_reply_to=None, refs=None, origin_scope="t")
    dc = cards.draft_card(d, account_title="工作邮箱")
    assert any(e.get("tag") == "form" for e in dc["body"]["elements"])
    s = cards.simplify(dc)
    assert not any(e.get("tag") in ("form", "column_set") for e in s["body"]["elements"])
    send_btn = [e for e in s["body"]["elements"] if e.get("tag") == "button" and e["behaviors"][0]["value"]["a"] == "send"]
    assert send_btn and "confirm" in send_btn[0]


# ---------------------------------------------------------------- 附件
def test_attach_from_history_and_feishu_upload_then_send(cfg, db, monkeypatch):
    import base64
    from mailowl import attachments
    bind_owner(db)
    sent = []
    svc, bot = make_service(cfg, db, smtp=lambda a, m: sent.append(m))
    old_raw = ("From: me@uni.edu\r\nTo: office@uni.edu\r\nSubject: CV\r\nMessage-ID: <cv@x>\r\nMIME-Version: 1.0\r\n"
               'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nattached\r\n'
               '--B\r\nContent-Type: application/pdf; name="Zhang_CV.pdf"\r\nContent-Disposition: attachment; filename="Zhang_CV.pdf"\r\n'
               "Content-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(b"%PDF cv").decode() + "\r\n--B--\r\n").encode()
    rec = parse_message(old_raw)
    rec.update(account="work", folder="Sent", remote_uid="7:9", direction="out", status="history")
    old_id = db.insert_mail(rec)
    monkeypatch.setattr(attachments, "_raw_from_server", lambda cfg_, m: old_raw)
    mid = add_mail(db, "1", sender="office@uni.edu", subject="Need CV")
    tools = MailTools(cfg, db, f"mail:{mid}")
    d = tools.save_draft(mail_id=mid, body="Please find my CV attached.")
    assert tools.attach_file_from_mail(d["draft_id"], old_id, "cv")["attached"] == "Zhang_CV.pdf"
    assert "现有附件" in tools.attach_file_from_mail(d["draft_id"], old_id, "nope.docx")["error"]
    run(svc.post_new_drafts(f"mail:{mid}"))
    card_msg = [s for s in bot.sent if s["kind"] == "card"][-1]["message_id"]
    # 用户回复草稿卡片发来一个文件
    bot.files["file_k1"] = b"statement bytes"
    m = inbound("", reply_to=card_msg, mid="om_file")
    m.resources = [NS(type="file", file_key="file_k1", file_name="statement.pdf")]
    run(svc.on_message(m))
    assert "statement.pdf" in bot.sent[-1]["text"]
    upd = [s for s in bot.sent if s["kind"] == "update" and s["target"] == card_msg][-1]
    assert "statement.pdf" in json.dumps(upd["card"], ensure_ascii=False)
    # 修改正文生成 v2，附件保留
    d2 = tools.save_draft(mail_id=mid, body="v2 body")
    assert {f["filename"] for f in tools.get_draft(mid)["draft"]["attachments"]} == {"Zhang_CV.pdf", "statement.pdf"}
    run(svc.send_draft(d2["draft_id"], None))
    names = sorted(p.get_filename() for p in sent[0].iter_attachments())
    assert names == ["Zhang_CV.pdf", "statement.pdf"]
    out = db.q1("SELECT * FROM mails WHERE folder='mailowl-sent'")
    assert "statement.pdf" in out["attachments"]
    # 发出后本地附件文件删除，记录保留给卡片显示
    files = db.q("SELECT * FROM draft_files")
    assert len(files) == 2 and not any(Path(f["path"]).exists() for f in files)
    assert not (cfg.data_dir / "attachments" / f"chain_{d['draft_id']}").exists()
    # 再从这封已发出的邮件取附件：本地没有了，改从「已发送」文件夹里的同一封取
    tools2 = MailTools(cfg, db, "chat:main")
    d3 = tools2.save_draft(account="work", to="x@y.com", subject="fwd", body="b")
    assert "已从本机删除" in tools2.attach_file_from_mail(d3["draft_id"], out["id"], "statement")["error"]
    copy = parse_message(sent[0].as_bytes())
    copy.update(account="work", folder="Sent", remote_uid="7:10", direction="out", status="history")
    db.insert_mail(copy)
    monkeypatch.setattr(attachments, "_raw_from_server", lambda cfg_, m: sent[0].as_bytes())
    assert tools2.attach_file_from_mail(d3["draft_id"], out["id"], "statement")["attached"] == "statement.pdf"


def test_cancel_purges_attachment_files(cfg, db):
    from mailowl.attachments import store_file
    bind_owner(db)
    svc, bot = make_service(cfg, db)
    mid = add_mail(db, "1", sender="office@uni.edu")
    d = MailTools(cfg, db, f"mail:{mid}").save_draft(mail_id=mid, body="b")
    f = store_file(cfg, db, d["draft_id"], "a.pdf", b"pdf", "feishu")
    path = Path(db.q1("SELECT path FROM draft_files WHERE id=?", (f["id"],))["path"])
    assert path.exists()
    run(svc.act_cancel(msg_id="om_card", draft_id=d["draft_id"]))
    assert db.draft(d["draft_id"])["status"] == "cancelled" and not path.exists()


def test_attachment_size_limit(cfg, db):
    from mailowl.attachments import AttachmentError, store_file
    with pytest.raises(AttachmentError):
        store_file(cfg, db, 1, "big.bin", b"0" * (21 * 1024 * 1024), "t")


def test_buttons_unique_per_render_and_bridge(cfg, db):
    mid = add_mail(db, "1")
    vals = lambda c: [b["behaviors"][0]["value"] for e in c["body"]["elements"] if e.get("tag") == "column_set"
                      for col in e["columns"] for b in col["elements"]]
    a, b = vals(cards.mail_card(db.mail(mid), "x")), vals(cards.mail_card(db.mail(mid), "x"))
    assert [v["a"] for v in a] == [v["a"] for v in b] and all(x["n"] != y["n"] for x, y in zip(a, b))

    # FeishuBot.on 的桥接：在另一个线程的事件循环里触发，处理函数要在主循环上执行
    import threading
    from mailowl.feishu import FeishuBot
    bot = FeishuBot.__new__(FeishuBot)
    registered = {}
    bot.channel = NS(on=lambda e, h: registered.__setitem__(e, h))
    seen = []

    async def handler(ev):
        seen.append((ev, asyncio.get_running_loop()))

    async def main():
        bot._main_loop = asyncio.get_running_loop()
        bot.on("message", handler)
        t = threading.Thread(target=lambda: asyncio.run(_fire()))

        async def _fire():
            registered["message"]("ev1")
        t.start()
        t.join()
        await asyncio.sleep(0.05)
        return asyncio.get_running_loop()
    loop = run(main())
    assert seen == [("ev1", loop)]


def test_reopen_reverts_card_mute_and_self_mail_is_quiet(cfg, db):
    bind_owner(db)
    llm = FakeLLM(triage={"level": 2, "category": "action", "summary": "s", "analysis": "", "deadline": None,
                          "needs_reply": True, "action_items": [], "tags": []})
    svc, bot = make_service(cfg, db, llm)
    mid = add_mail(db, "1", sender="Prof <prof@uni.edu>")
    run(svc.on_card_action(action({"a": "mute", "m": mid})))
    assert db.sender_stats("prof@uni.edu")["pref"] < 0
    run(svc.on_card_action(action({"a": "reopen", "m": mid})))
    assert db.sender_stats("prof@uni.edu")["pref"] == 0 and db.mail(mid)["status"] == "open"
    own = add_mail(db, "2", sender="Me <me@uni.edu>", msgid="<own@x>")
    n = len(llm.calls)
    run(svc.process_mail(own))
    assert len(llm.calls) == n and db.mail(own)["level"] == 0 and not db.mail(own)["notified_at"]


def test_signature_presets_and_one_tap_switch(cfg, db):
    from mailowl.config import Signature
    cfg.signatures = [Signature("熟人", "祝好！\n\n小明"), Signature("正式", "谢谢！\n\n张小明\n{affiliation}")]
    cfg.account("work").affiliation = "示例大学生命科学学院\n示例研究中心"
    bind_owner(db)
    sent = []
    svc, bot = make_service(cfg, db, smtp=lambda a, m: sent.append(m))
    mid = add_mail(db, "1", sender="office@uni.edu")
    tools = MailTools(cfg, db, f"mail:{mid}")
    spec = next(s for s in tools.specs() if s["name"] == "save_draft")
    assert spec["inputSchema"]["properties"]["signature"]["enum"] == ["熟人", "正式", "none"]
    assert "error" in tools.save_draft(mail_id=mid, body="x", signature="随便")
    d = tools.save_draft(mail_id=mid, body="王老师好：\n\n收到。", signature="熟人")
    assert db.draft(d["draft_id"])["body"] == "王老师好：\n\n收到。\n\n祝好！\n\n小明"
    assert tools.get_draft(mid)["draft"]["body"] == "王老师好：\n\n收到。"      # 改稿时只看到正文
    run(svc.post_new_drafts(f"mail:{mid}"))
    card = [s for s in bot.sent if s["kind"] == "card"][-1]
    assert '"✓ 熟人"' in json.dumps(card["card"], ensure_ascii=False)
    run(svc.on_card_action(action({"a": "sig", "d": d["draft_id"], "s": "正式"}, message_id=card["message_id"])))
    body = db.draft(d["draft_id"])["body"]
    assert body.endswith("谢谢！\n\n张小明\n示例大学生命科学学院\n示例研究中心")
    assert db.draft(d["draft_id"])["version"] == 1                              # 换落款不产生新版本
    d2 = tools.save_draft(mail_id=mid, body="王老师好：\n\n收到，马上处理。")      # 改稿：沿用「正式」
    assert db.draft(d2["draft_id"])["sig"] == "正式" and db.draft(d2["draft_id"])["body"].endswith("示例研究中心")
    run(svc.send_draft(d2["draft_id"], None))
    assert sent[0].get_content().rstrip().endswith("示例研究中心")
    run(svc.on_card_action(action({"a": "sig", "d": d2["draft_id"], "s": "熟人"})))
    assert db.draft(d2["draft_id"])["body"].endswith("示例研究中心"), "已发出的草稿不能再改落款"
    d3 = MailTools(cfg, db, "chat:main").save_draft(account="work", to="x@y.com", subject="s",
                                                    body="常老师您好：\n\n收到。\n\n谢谢！\n\n张小明", signature="正式")
    assert db.draft(d3["draft_id"])["body"].count("谢谢") == 1 and db.draft(d3["draft_id"])["body_main"] == "常老师您好：\n\n收到。"
    from mailowl.tools import strip_closing
    assert strip_closing("您好：\n\n邮件已收到，我会尽快处理，谢谢！", set(), "谢谢！") == "您好：\n\n邮件已收到，我会尽快处理。"
    assert strip_closing("Dear Prof. Li,\n\nNoted, thanks.\n\nBest regards,", {"Best regards,"}, "Best regards,") == \
        "Dear Prof. Li,\n\nNoted, thanks."
    assert strip_closing("谢谢！", set(), "谢谢！") == "谢谢！", "整封只有一句时不删"


def test_failed_bark_push_is_not_recorded(cfg, db):
    from mailowl.bark import BarkItem
    svc, bot = make_service(cfg, db)

    async def fail(*a, **kw):
        return False

    svc.bark.push = fail
    svc.bark_items.append(BarkItem(title="t", body="b", level="active", mail_id=1))
    run(svc.flush_bark())
    assert db.q1("SELECT COUNT(*) n FROM notifications WHERE channel='bark'")["n"] == 0
