"""分级的可选后端：本机已登录的 OpenAI Codex CLI（codex exec），用的是 ChatGPT 订阅额度。

只实现 structured()（邮件分级）；对话 Agent 和联网研究仍走 claude_cli / openai 后端。

实测过的几个坑（codex-cli 0.165，别退回去）：
- 要用独立的 CODEX_HOME：~/.codex/AGENTS.md 不受 --ignore-user-config、project_doc_max_bytes 影响，
  本机的全局规则和人设会串进邮件摘要。独立目录里只放一个指向原 auth.json 的软链接——不能复制，
  刷新 token 时副本会和原件分叉，可能把用户自己的 Codex 登录顶掉。
- codex exec 总带 shell 工具，--sandbox read-only 也能读整个磁盘（实测读得到 secrets.toml）。
  macOS 上外面再套一层 sandbox-exec：嵌套时 Codex 自己的沙箱起不来，模型的任何命令都会失败；
  外层规则另外禁止读写 MailOwl 的配置和数据目录，作为第二道保险。
- --ignore-user-config：不加载用户的 config.toml（里面可能有 notify 钩子、很高的推理强度等）。
- 每次调用约 20–30 秒，经代理约 5–6 MB 下行、1 MB 上行，几乎都是 CLI 启动开销，与邮件长短无关。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .llm import LLMError
from .openai_compat import conform, extract_json

log = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SEARCH_PATH = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def _sb_quote(p: Path) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


class CodexCLI:
    def __init__(self, cfg: Config, *, model: str | None = None, effort: str | None = None, bin: str | None = None):
        t = cfg.llm.triage                                # 没传就用 [llm.triage] 里的 model / effort / codex_bin
        self.cfg = cfg
        self.model = model or t.get("model")
        self.effort = effort or t.get("effort") or "low"
        self.bin_hint = bin or t.get("codex_bin")
        self.home = cfg.data_dir / "codex-home"          # 独立的 CODEX_HOME：没有 AGENTS.md
        self.work = cfg.data_dir / "codex-work"          # 每次调用的临时目录（schema、输出、空的工作目录）
        self.src_home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
        if not self.outer_sandbox:
            log.warning("没有 %s（非 macOS）：Codex 只有它自己的只读沙箱，模型仍能读到本机文件", SANDBOX_EXEC)

    @property
    def outer_sandbox(self) -> bool:
        return sys.platform == "darwin" and Path(SANDBOX_EXEC).exists()

    def auth_linked(self) -> bool:
        link = self.home / "auth.json"
        return link.is_symlink() and link.resolve() == (self.src_home / "auth.json").resolve()

    @property
    def bin(self) -> str:
        if self.bin_hint:
            return str(Path(self.bin_hint).expanduser())
        path = os.pathsep.join([str(Path(p).expanduser()) for p in SEARCH_PATH] + [os.environ.get("PATH", "")])
        return shutil.which("codex", path=path) or "codex"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.home)
        env["PATH"] = os.pathsep.join([str(Path(self.bin).parent), *SEARCH_PATH[1:], env.get("PATH", "")])
        return env

    def prepare(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        link, src = self.home / "auth.json", self.src_home / "auth.json"
        if link.is_symlink() or not link.exists():
            if not (link.is_symlink() and link.resolve() == src.resolve()):
                link.unlink(missing_ok=True)
                link.symlink_to(src)
        else:
            log.warning("%s 不是指向 %s 的软链接，Codex 登录可能与本机其他 Codex 分叉", link, src)

    def sandbox_profile(self) -> str:
        """外层沙箱：禁止读写 MailOwl 的配置、数据目录和 ~/.ssh，只放行 Codex 自己要用的两个子目录。

        只禁读内容（file-read-data / xattr）、不禁 file-read-metadata：Codex 启动时要逐级 canonicalize
        CODEX_HOME，禁了元数据会直接退出。放行规则必须写同样的操作名——实测通配的 file-read*
        覆盖不了前面具体的 file-read-data 禁止。"""
        ops = "file-read-data file-read-xattr file-write*"
        deny = [self.cfg.path.parent, self.cfg.data_dir, Path("~/.ssh").expanduser()]
        allow = [self.home, self.work]
        return ("(version 1)\n(allow default)\n"
                f"(deny {ops} {' '.join(f'(subpath {_sb_quote(p)})' for p in deny)})\n"
                f"(allow {ops} {' '.join(f'(subpath {_sb_quote(p)})' for p in allow)})\n")

    def command(self, cwd: Path, schema_file: Path, out_file: Path, model: str | None) -> list[str]:
        args = [self.bin, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
                "-C", str(cwd), "-s", "read-only", "-c", f"model_reasoning_effort={self.effort}",
                "-c", 'web_search="disabled"', "--output-schema", str(schema_file), "-o", str(out_file)]
        if model or self.model:
            args += ["-m", model or self.model]
        args.append("-")                                  # 提示从 stdin 读：邮件正文可能很长
        if self.outer_sandbox:
            args = [SANDBOX_EXEC, "-p", self.sandbox_profile(), *args]
        return args

    async def structured(self, prompt: str, schema: dict, *, system: str, model: str | None = None,
                         timeout: int | None = None) -> tuple[dict[str, Any], float]:
        self.prepare()
        timeout = timeout or self.cfg.llm.triage_timeout
        with tempfile.TemporaryDirectory(dir=self.work) as td:
            td = Path(td)
            cwd, schema_file, out_file = td / "cwd", td / "schema.json", td / "out.json"
            cwd.mkdir()
            schema_file.write_text(json.dumps(schema, ensure_ascii=False))
            proc = await asyncio.create_subprocess_exec(
                *self.command(cwd, schema_file, out_file, model), cwd=str(cwd), env=self._env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(f"{system}\n\n{prompt}".encode("utf-8")), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise LLMError(f"codex 调用超时（{timeout}s）")
            raw = out_file.read_text("utf-8") if out_file.exists() else ""
            if proc.returncode != 0 or not raw.strip():
                msg = err.decode("utf-8", "replace").strip()[-800:]
                raise LLMError(f"codex 调用失败 (rc={proc.returncode}): {msg}")
        return conform(extract_json(raw), schema), 0.0     # 订阅额度，不按 token 计费
