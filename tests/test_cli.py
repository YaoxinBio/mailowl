import plistlib
import sys

from mailowl import launchd
from mailowl.cli import main


def test_launchd_plist_runs_module_with_explicit_config(cfg):
    pl = launchd.build_plist(cfg)
    assert pl["ProgramArguments"] == [sys.executable, "-m", "mailowl", "--config", str(cfg.path), "run"]
    assert pl["KeepAlive"] is True and pl["WorkingDirectory"] == str(cfg.data_dir)
    assert pl["EnvironmentVariables"]["PATH"].split(":")[0].endswith("bin")
    plistlib.loads(plistlib.dumps(pl))                         # 能序列化成合法 plist


def test_launchctl_print_parsing():
    text = "local.mailowl = {\n\tactive count = 1\n\tstate = running\n\tpid = 4242\n\tlast exit code = (never exited)\n}"
    assert launchd.parse_print(text) == {"loaded": True, "state": "running", "pid": "4242",
                                         "last_exit": "(never exited)"}


def test_service_status_when_not_installed(cfg, monkeypatch, capsys):
    monkeypatch.setattr(launchd, "status", lambda: {"loaded": False, "plist": None})
    monkeypatch.setattr(sys, "platform", "darwin")
    assert main(["--config", str(cfg.path), "service", "status"]) == 0
    assert "未安装" in capsys.readouterr().out


def test_demo_runs_offline_in_both_languages(monkeypatch, tmp_path, capsys):
    import asyncio
    from mailowl.demo import run_demo
    from mailowl.llm import FakeLLM
    monkeypatch.setenv("MAILOWL_CONFIG", str(tmp_path / "none.toml"))     # 不读真实配置
    for lang, word in (("en", "Emails sent: 0"), ("zh", "已发出的邮件：0 封")):
        assert asyncio.run(run_demo(lang=lang, llm=FakeLLM())) == 0
        assert word in capsys.readouterr().out
