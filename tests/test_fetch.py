from mailowl.fetch import IMAPSync, POP3Sync
from tests.conftest import make_raw


class FakeIMAP:
    def __init__(self, folders, uidvalidity=7):
        self.folders, self.uidvalidity = folders, uidvalidity
        self.readonly_flags, self.fetches, self.specs = [], [], {}

    def select(self, mailbox, readonly=False):
        self.sel = mailbox.strip('"')
        self.readonly_flags.append(readonly)
        msgs = self.folders[self.sel]
        self._resp = {"UIDVALIDITY": [str(self.uidvalidity).encode()],
                      "UIDNEXT": [str(max(msgs or [0]) + 1).encode()]}
        return "OK", [str(len(msgs)).encode()]

    def response(self, code):
        return code, self._resp.get(code, [None])

    def uid(self, cmd, *args):
        msgs = self.folders[self.sel]
        if cmd == "SEARCH":
            crit = args[1:]
            if crit[0] == "SINCE":
                uids = sorted(msgs)
            elif crit[1] == "*":
                uids = [max(msgs)] if msgs else []
            else:
                lo = int(crit[1].split(":")[0])
                uids = [u for u in msgs if u >= lo] or ([max(msgs)] if msgs else [])  # 模拟 n:* 总返回最后一封
            return "OK", [" ".join(map(str, uids)).encode()]
        spec, what = args
        if "RFC822.SIZE" in what:
            return "OK", [f"{i} (UID {u} RFC822.SIZE {len(msgs[u])})".encode()
                          for i, u in enumerate(map(int, spec.split(",")), 1)]
        assert "PEEK" in what, "必须用 BODY.PEEK，不能把邮件标为已读"
        u = int(spec)
        self.fetches.append(u)
        self.specs[u] = what
        return "OK", [(f"1 (UID {u} BODY[] {{{len(msgs[u])}}}".encode(), msgs[u]), b")"]

    def logout(self):
        pass


def test_imap_backfill_then_incremental(cfg, db):
    acct = cfg.account("work")
    inbox = {1: make_raw(msgid="<1@x>"), 2: make_raw(msgid="<2@x>"), 3: make_raw(msgid="<3@x>")}
    sent = {5: make_raw(msgid="<s@x>", sender="me@uni.edu", to="alice@x.com")}
    fake = FakeIMAP({"INBOX": inbox, "Sent": sent})
    sync = IMAPSync(acct, db, cfg.max_message_bytes, connect=lambda a: fake)
    assert sync.run() == []                                   # 首次：全部作为历史
    assert db.q1("SELECT COUNT(*) n FROM mails WHERE status='history'")["n"] == 4
    assert all(fake.readonly_flags)
    inbox[4] = make_raw(msgid="<4@x>", subject="new one")
    new = sync.run()
    assert len(new) == 1 and db.mail(new[0])["subject"] == "new one"
    assert db.mail(new[0])["status"] == "open"
    assert sync.run() == []                                   # 没有新邮件时不重复入库
    assert fake.fetches.count(4) == 1
    out = db.q1("SELECT * FROM mails WHERE folder='Sent'")
    assert out["direction"] == "out" and out["status"] == "history"


def test_backfill_large_mail_fetches_only_text(cfg, db):
    acct = cfg.account("work")
    big = make_raw(msgid="<big@x>", body="x" * 400_000)
    fake = FakeIMAP({"INBOX": {1: big, 2: make_raw(msgid="<small@x>")}, "Sent": {}})
    sync = IMAPSync(acct, db, cfg.max_message_bytes, connect=lambda a: fake)
    sync.run()
    assert "BODY.PEEK[TEXT]<0." in fake.specs[1] and fake.specs[2] == "(BODY.PEEK[])"
    fake.folders["INBOX"][3] = make_raw(msgid="<big2@x>", body="y" * 400_000)
    sync.run()                                                # 新邮件不受回填上限影响，整封取
    assert fake.specs[3] == "(BODY.PEEK[])"


def test_imap_uidvalidity_change_is_backfill_not_flood(cfg, db):
    acct = cfg.account("work")
    fake = FakeIMAP({"INBOX": {1: make_raw(msgid="<1@x>")}, "Sent": {}})
    sync = IMAPSync(acct, db, cfg.max_message_bytes, connect=lambda a: fake)
    sync.run()
    fake.uidvalidity = 99
    fake.folders["INBOX"][2] = make_raw(msgid="<2@x>")
    assert sync.run() == []


class FakePOP:
    def __init__(self, msgs):
        self.msgs = msgs

    def uidl(self):
        return b"+OK", [f"{i} {u}".encode() for i, (u, _) in enumerate(self.msgs, 1)], 0

    def list(self):
        return b"+OK", [f"{i} {len(r)}".encode() for i, (_, r) in enumerate(self.msgs, 1)], 0

    def retr(self, n):
        return b"+OK", self.msgs[n - 1][1].split(b"\r\n"), 0

    def quit(self):
        pass
    # 没有 dele()：如果代码试图删除服务器上的邮件，测试会直接报错


def test_pop3_never_deletes_and_dedups(cfg, db):
    acct = cfg.account("old")
    msgs = [("u1", make_raw(msgid="<p1@x>", date="Thu, 29 Oct 2026 10:00:00 +0800"))]
    fake = FakePOP(msgs)
    sync = POP3Sync(acct, db, cfg.max_message_bytes, connect=lambda a: fake)
    assert sync.run() == []
    msgs.append(("u2", make_raw(msgid="<p2@x>", subject="pop new")))
    new = sync.run()
    assert len(new) == 1 and db.mail(new[0])["subject"] == "pop new"
    assert sync.run() == []


def test_mailbox_name_encoding():
    from mailowl.fetch import _quote_mailbox
    assert _quote_mailbox("已发送") == "&XfJT0ZAB-"
    assert _quote_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'
    assert _quote_mailbox("A&B") == "A&-B"
    assert _quote_mailbox("INBOX") == "INBOX"
    from mailowl.fetch import imap_utf7_decode
    assert imap_utf7_decode("&XfJT0ZAB-") == "已发送" and imap_utf7_decode("A&-B") == "A&B"
