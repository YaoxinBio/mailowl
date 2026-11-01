"""把 MailOwl 装成 macOS LaunchAgent：登录后自动启动，崩溃后 30 秒内拉起。

用 `python -m mailowl --config <路径> run` 启动，所以无论是 git clone + venv、pip 还是
`uv tool install` 装的都能用，不依赖项目目录在哪里。
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import Config

LABEL = "local.mailowl"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def build_plist(cfg: Config) -> dict:
    path_dirs = [str(Path(cfg.llm.claude_bin).expanduser().parent), str(Path.home() / ".local" / "bin"),
                 "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "mailowl", "--config", str(cfg.path), "run"],
        "WorkingDirectory": str(cfg.data_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(cfg.log_dir / "launchd.out.log"),
        "StandardErrorPath": str(cfg.log_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": ":".join(dict.fromkeys(path_dirs)),
            "LANG": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "DISABLE_AUTOUPDATER": "1",                         # 别让 claude CLI 在后台自己升级
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
    }


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def parse_print(text: str) -> dict:
    out: dict = {"loaded": bool(text.strip())}
    for key, pat in (("state", r"^\s*state = (\S+)"), ("pid", r"^\s*pid = (\d+)"),
                     ("last_exit", r"^\s*last exit code = (.+)$")):
        m = re.search(pat, text, re.M)
        if m:
            out[key] = m.group(1).strip()
    return out


def status() -> dict:
    r = _launchctl("print", _target())
    info = parse_print(r.stdout) if r.returncode == 0 else {"loaded": False}
    info["plist"] = str(plist_path()) if plist_path().exists() else None
    return info


def install(cfg: Config) -> dict:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    plist_path().parent.mkdir(parents=True, exist_ok=True)
    plist_path().write_bytes(plistlib.dumps(build_plist(cfg)))
    uninstall(remove_file=False)
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path()))
    if r.returncode != 0:                                      # 偶发：旧实例还没完全退出
        time.sleep(3)
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path()), check=True)
    time.sleep(2)
    return status()


def uninstall(remove_file: bool = True) -> None:
    _launchctl("bootout", _target())
    for _ in range(20):                                        # bootout 是异步的，等它真正卸载
        if _launchctl("print", _target()).returncode != 0:
            break
        time.sleep(0.5)
    if remove_file and plist_path().exists():
        plist_path().unlink()


def restart() -> dict:
    _launchctl("kickstart", "-k", _target(), check=True)
    time.sleep(2)
    return status()
