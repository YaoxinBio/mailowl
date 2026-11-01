"""IMAP IDLE：用一对本地 socket 模拟服务器，不联网。"""
import asyncio
import imaplib
import socket
import threading
import time

from mailowl.idle import idle_once
from tests.test_service import FakeLLM, add_mail, bind_owner, make_service, run


class PairIMAP(imaplib.IMAP4):
    def __init__(self, sock):
        self._pair = sock
        super().__init__()

    def open(self, host="", port=imaplib.IMAP4_PORT, timeout=None):
        self.host, self.port = host, port
        self.sock = self._pair
        self.file = self.sock.makefile("rb")


def fake_server(sock, *, idle=True, push_after=0.1, exists=True):
    """最小 IMAP 服务器：问候、CAPABILITY、EXAMINE、IDLE。"""
    state = {"noops": 0}

    def run_server():
        f = sock.makefile("rb")
        sock.sendall(b"* OK fake ready\r\n")
        while True:
            line = f.readline()
            if not line:
                return
            tag, cmd = line.split(b" ", 2)[:2]
            cmd = cmd.strip().upper()
            if cmd == b"CAPABILITY":
                sock.sendall(b"* CAPABILITY IMAP4rev1" + (b" IDLE" if idle else b"") + b"\r\n" + tag + b" OK done\r\n")
            elif cmd == b"LOGIN":
                sock.sendall(tag + b" OK logged in\r\n")
            elif cmd in (b"EXAMINE", b"SELECT"):
                sock.sendall(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 7]\r\n" + tag + b" OK [READ-ONLY] done\r\n")
            elif cmd == b"IDLE":
                sock.sendall(b"+ idling\r\n")
                time.sleep(push_after)
                if exists:
                    sock.sendall(b"* 2 EXISTS\r\n")
            elif cmd == b"NOOP":
                state["noops"] += 1
                extra = b"* 2 EXISTS\r\n" if exists and state["noops"] >= 2 else b""
                sock.sendall(extra + tag + b" OK NOOP completed\r\n")
            elif cmd == b"LOGOUT":
                sock.sendall(b"* BYE\r\n" + tag + b" OK bye\r\n")
                return
    def safe():
        try:
            run_server()
        except OSError:          # 客户端先断开（超时、stop）时服务器侧写失败，正常
            pass
    threading.Thread(target=safe, daemon=True).start()


def connect_pair(**kw):
    def connect(acct):
        a, b = socket.socketpair()
        fake_server(b, **kw)
        M = PairIMAP(a)
        M.login("user", "pass")
        return M
    return connect


def test_idle_reports_new_mail(cfg):
    assert idle_once(cfg.account("work"), 5, connect=connect_pair()) is True


def test_idle_times_out_and_honors_stop(cfg):
    t0 = time.monotonic()
    assert idle_once(cfg.account("work"), 0.5, connect=connect_pair(exists=False)) is False
    assert time.monotonic() - t0 < 3
    stop = threading.Event()
    stop.set()
    assert idle_once(cfg.account("work"), 60, stop, connect=connect_pair(exists=False)) is False


def test_noop_watch_when_server_has_no_idle(cfg, monkeypatch):
    from mailowl import idle
    monkeypatch.setattr(idle, "NOOP_SECONDS", 0.1)
    assert idle_once(cfg.account("work"), 5, connect=connect_pair(idle=False)) is True
    assert idle_once(cfg.account("work"), 0.5, connect=connect_pair(idle=False, exists=False)) is False


def test_wake_event_triggers_immediate_poll(cfg, db):
    calls = []
    svc, bot = make_service(cfg, db)
    svc.fetch_fn = lambda acct, db_, mx: calls.append(time.monotonic()) or []
    acct = cfg.account("work")
    acct.poll_seconds = 3600

    async def go():
        task = asyncio.create_task(svc.poll_loop(acct))
        await asyncio.sleep(0.2)
        svc.wake[acct.name].set()
        await asyncio.sleep(0.3)
        task.cancel()

    run(go())
    assert len(calls) == 2, "IDLE 唤醒后应立即再收一次信，不用等 poll_seconds"


def test_failed_triage_is_pushed_immediately(cfg, db):
    class Broken(FakeLLM):
        async def structured(self, *a, **k):
            raise RuntimeError("model down")
    bind_owner(db)
    svc, bot = make_service(cfg, db, Broken())
    mid = add_mail(db, "1", sender="someone@other.org")
    run(svc.process_mail(mid))
    m = db.mail(mid)
    assert m["level"] == 1 and m["route"] == "immediate" and m["notified_at"]
    assert [s["kind"] for s in bot.sent] == ["card"]
