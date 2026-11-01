import base64

from mailowl.mailparse import html_to_text, normalize_subject, parse_message
from tests.conftest import make_raw


def test_gbk_subject_and_html_body():
    subj = "=?gb2312?B?" + base64.b64encode("关于补充材料".encode("gbk")).decode() + "?="
    html = "<html><body><p>您好，</p><p>请于<b>11月7日</b>前提交&nbsp;CV。</p><script>x()</script></body></html>"
    raw = (f"From: =?utf-8?B?{base64.b64encode('科研处'.encode()).decode()}?= <office@uni.edu>\r\n"
           f"To: me@uni.edu\r\nSubject: {subj}\r\nMessage-ID: <a@b>\r\nMIME-Version: 1.0\r\n"
           "Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
           "--XX\r\nContent-Type: text/html; charset=gbk\r\nContent-Transfer-Encoding: base64\r\n\r\n"
           + base64.b64encode(html.encode("gbk")).decode() + "\r\n"
           "--XX\r\nContent-Type: application/pdf; name=\"form.pdf\"\r\nContent-Disposition: attachment; filename=\"form.pdf\"\r\n"
           "Content-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(b"%PDF-1.4 test").decode() + "\r\n--XX--\r\n").encode()
    r = parse_message(raw)
    assert r["subject"] == "关于补充材料"
    assert r["from_name"] == "科研处" and r["from_addr"] == "office@uni.edu"
    assert "11月7日" in r["body_text"] and "x()" not in r["body_text"]
    assert r["attachments"] == [{"filename": "form.pdf", "type": "application/pdf", "size": 13}]


def test_thread_key_and_subject_normalize():
    r = parse_message(make_raw(subject="Re: 回复: hi", msgid="<c@x>", in_reply_to="<b@x>", refs="<a@x> <b@x>"))
    assert r["thread_key"] == "<a@x>" and r["in_reply_to"] == "<b@x>"
    assert normalize_subject("Re: 回复：Fwd: Hello") == "hello"
    assert "• one" in html_to_text("<ul><li>one</li></ul>")


def test_insert_dedup_search_thread(db):
    base = dict(account="work", folder="INBOX", direction="in", status="open")
    r1 = parse_message(make_raw(subject="基金申请材料", body="请提交更新后的简历", msgid="<a@x>"))
    r2 = parse_message(make_raw(subject="Re: 基金申请材料", body="已附上", msgid="<b@x>", in_reply_to="<a@x>",
                                refs="<a@x>", sender="me@uni.edu", to="alice@x.com"))
    id1 = db.insert_mail({**r1, **base, "remote_uid": "1:1"})
    assert db.insert_mail({**r1, **base, "remote_uid": "1:1"}) is None  # 去重
    id2 = db.insert_mail({**r2, **base, "folder": "Sent", "direction": "out", "remote_uid": "1:2"})
    assert {m["id"] for m in db.search("申请材料")} == {id1, id2}
    assert [m["id"] for m in db.search("简历")] == [id1]          # 2 个字：退回 LIKE
    assert [m["id"] for m in db.thread(id1)] == [id1, id2]
    st = db.sender_stats("alice@x.com")
    assert st["received"] == 1 and st["replied"] == 1
