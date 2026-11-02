"""Codex CLI 分级后端：隔离（独立 CODEX_HOME、外层沙箱、不读用户配置）与输出解析。"""
import json
from pathlib import Path

import pytest

from mailowl import codex_cli
from mailowl.codex_cli import CodexCLI
from mailowl.config import ConfigError, load_config
from mailowl.llm import LLMError
from mailowl.triage import SCHEMA
from tests.test_service import run


def with_codex_triage(cfg, extra=""):
    p = cfg.path
    p.write_text(p.read_text("utf-8").replace(
        "[general]", f'[llm.triage]\nbackend = "codex_cli"\nmodel = "gpt-6.1-sol"\n{extra}[general]', 1), "utf-8")
    return load_config(p)


class FakeProc:
    def __init__(self, rc=0, err=b""):
        self.returncode, self.err, self.stdin = rc, err, None

    async def communicate(self, data):
        self.stdin = data
        return b"", self.err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def fake_exec(calls, *, output=None, rc=0, err=b""):
    async def create(*args, **kw):
        proc = FakeProc(rc, err)
        calls.append((list(args), kw, proc))
        if output is not None:
            Path(args[args.index("-o") + 1]).write_text(output, "utf-8")
        return proc
    return create


def test_codex_triage_config_and_router(cfg):
    c = with_codex_triage(cfg, 'effort = "medium"\n')
    from mailowl.db import DB
    from mailowl.llm import ClaudeCLI, make_llm
    r = make_llm(c, DB(c.db_path))
    assert isinstance(r.main, ClaudeCLI) and isinstance(r.triager, CodexCLI)
    assert r.triager.model == "gpt-6.1-sol" and r.triager.effort == "medium"
    p = c.path
    p.write_text(p.read_text("utf-8").replace('model = "gpt-6.1-sol"', 'model = "sonnet"'), "utf-8")
    with pytest.raises(ConfigError, match="sonnet"):
        load_config(p)
    p.write_text(p.read_text("utf-8").replace('model = "sonnet"', 'temperature = 0'), "utf-8")
    with pytest.raises(ConfigError, match="temperature"):
        load_config(p)


def test_codex_structured_is_isolated(cfg, tmp_path, monkeypatch):
    src = tmp_path / "user-codex"
    src.mkdir()
    (src / "auth.json").write_text("{}")
    (src / "AGENTS.md").write_text("# 全局规则：回答末尾加「汪」")
    monkeypatch.setenv("CODEX_HOME", str(src))
    monkeypatch.setattr(codex_cli.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(codex_cli.asyncio, "create_subprocess_exec", fake_exec(calls, output=json.dumps(
        {"level": "5", "category": "newsletter", "summary": "周报", "analysis": "无需处理", "deadline": None,
         "needs_reply": "false", "action_items": [], "tags": []})))
    cx = CodexCLI(with_codex_triage(cfg))
    out, cost = run(cx.structured("邮件正文", SCHEMA, system="分级规则"))
    assert out["level"] == 3 and out["needs_reply"] is False and cost == 0.0     # 按 schema 纠正
    args, kw, proc = calls[-1]
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--output-schema"):
        assert flag in args
    assert args[args.index("-s") + 1] == "read-only" and 'web_search="disabled"' in args
    assert args[args.index("-m") + 1] == "gpt-6.1-sol" and args[-1] == "-"
    home = Path(kw["env"]["CODEX_HOME"])
    assert home == cx.home and not (home / "AGENTS.md").exists()                  # 用户的全局规则不进来
    assert (home / "auth.json").is_symlink() and (home / "auth.json").resolve() == (src / "auth.json").resolve()
    assert "分级规则".encode() in proc.stdin and "邮件正文".encode() in proc.stdin
    assert not list(cx.work.iterdir()), "临时目录用完即删"
    assert cx.auth_linked()
    (home / "auth.json").unlink()
    (home / "auth.json").write_text("{}")                                       # 被替换成普通文件：要能看出来
    assert not cx.auth_linked()


def test_codex_wrapped_in_outer_sandbox_on_macos(cfg, tmp_path, monkeypatch):
    sb = tmp_path / "sandbox-exec"
    sb.write_text("")
    monkeypatch.setattr(codex_cli, "SANDBOX_EXEC", str(sb))
    monkeypatch.setattr(codex_cli.sys, "platform", "darwin")
    cx = CodexCLI(with_codex_triage(cfg))
    args = cx.command(tmp_path, tmp_path / "s.json", tmp_path / "o.json", None)
    assert args[0] == str(sb) and args[1] == "-p" and args[3] == cx.bin
    prof = args[2]
    deny, allow = prof.index("(deny"), prof.index("(allow file-read-data")
    assert "file-read-metadata" not in prof and "(deny file-read-data file-read-xattr file-write*" in prof
    assert deny < allow, "放行规则写在后面才会覆盖禁止规则"
    for p in (cfg.path.parent, cfg.data_dir):
        assert f'(subpath "{p}")' in prof[deny:allow]
    for p in (cx.home, cx.work):
        assert f'(subpath "{p}")' in prof[allow:]


def test_codex_failure_raises_with_stderr(cfg, monkeypatch):
    monkeypatch.setattr(codex_cli.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(codex_cli.asyncio, "create_subprocess_exec",
                        fake_exec(calls, rc=1, err=b"You've hit your usage limit."))
    cx = CodexCLI(with_codex_triage(cfg))
    with pytest.raises(LLMError, match="usage limit"):
        run(cx.structured("x", SCHEMA, system="s"))
