import textwrap
from pathlib import Path

import pytest

from mailowl.config import load_config
from mailowl.db import DB


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    from mailowl import triage
    monkeypatch.setattr(triage, "TRIAGE_RETRY_DELAY", 0)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MAILOWL_CONFIG", raising=False)
    d = tmp_path / "conf"
    d.mkdir()
    (d / "config.toml").write_text(textwrap.dedent(f"""
        [general]
        data_dir = "{tmp_path / 'data'}"
        profile = "测试用户"
        [rules]
        vip_senders = ["boss@uni.edu"]
        mute_senders = ["@spam.com"]
        local_only_keywords = ["审稿"]
        [routing]
        digest_times = ["08:30"]
        [bark]
        quiet_hours = ""
        coalesce_seconds = 1
        [[accounts]]
        name = "work"
        label = "工作邮箱"
        address = "me@uni.edu"
        host = "imap.uni.edu"
        sent_folder = "Sent"
        smtp_host = "smtp.uni.edu"
        [[accounts]]
        name = "old"
        address = "me@old.com"
        protocol = "pop3"
        host = "pop.old.com"
    """), "utf-8")
    sec = d / "secrets.toml"
    sec.write_text('[accounts.work]\npassword = "pw"\n[bark]\ndevice_keys = ["k1"]\n', "utf-8")
    sec.chmod(0o600)
    c = load_config(d / "config.toml")
    for p in (c.data_dir, c.log_dir, c.agent_workdir):
        p.mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def db(cfg):
    return DB(cfg.db_path)


def make_raw(subject="Hello", sender="Alice <alice@x.com>", to="me@uni.edu", body="hi there",
             msgid="<m1@x.com>", in_reply_to=None, refs=None, date="Fri, 30 Oct 2026 10:00:00 +0800",
             extra_headers="") -> bytes:
    h = [f"From: {sender}", f"To: {to}", f"Subject: {subject}", f"Message-ID: {msgid}", f"Date: {date}",
         "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]
    if in_reply_to:
        h.append(f"In-Reply-To: {in_reply_to}")
    if refs:
        h.append(f"References: {refs}")
    if extra_headers:
        h.append(extra_headers)
    return ("\r\n".join(h) + "\r\n\r\n" + body).encode("utf-8")
