"""命令行入口：mailowl <命令>。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .config import Config, ConfigError, ensure_dirs, load_config, resolve_config_path

log = logging.getLogger("mailowl")


def setup_logging(cfg: Config | None, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else (logging.INFO if sys.stderr.isatty() else logging.WARNING))
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if cfg is not None:
        fh = logging.handlers.RotatingFileHandler(cfg.log_dir / "mailowl.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "websockets", "Lark", "lark_channel"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _cfg(args) -> Config:
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    return cfg


def cmd_init(args) -> int:
    path = resolve_config_path(args.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    for name, src in (("config.toml", "config.example.toml"), ("secrets.toml", "secrets.example.toml")):
        dst = path.parent / name if name == "secrets.toml" else path
        if dst.exists():
            print(f"已存在，跳过：{dst}")
            continue
        dst.write_bytes((Path(__file__).parent / "templates" / src).read_bytes())
        if name == "secrets.toml":
            os.chmod(dst, 0o600)
        print(f"已创建：{dst}")
    print("下一步：编辑这两个文件，然后运行 `mailowl check`")
    return 0


def cmd_check(args) -> int:
    import httpx
    from .fetch import IMAPSync, POP3Sync
    import smtplib
    import ssl
    import subprocess

    cfg = _cfg(args)
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"{'✅' if good else '❌'} {name}{('：' + detail) if detail else ''}")

    report("配置文件", True, str(cfg.path))
    for a in cfg.accounts:
        if not a.enabled:
            print(f"⏸  {a.name} 已停用")
            continue
        try:
            if a.protocol == "imap":
                M = IMAPSync.connect(a)
                typ, boxes = M.list()
                M.logout()
                report(f"{a.name} IMAP 登录", True, f"{a.host}:{a.port}，{len(boxes or [])} 个文件夹")
            else:
                M = POP3Sync.connect(a)
                n, size = M.stat()
                M.quit()
                report(f"{a.name} POP3 登录", True, f"{a.host}:{a.port}，服务器上 {n} 封")
        except Exception as e:
            report(f"{a.name} 收信登录", False, f"{type(e).__name__}: {e}")
        if a.can_send:
            try:
                ctx = ssl.create_default_context()
                if a.smtp_security == "ssl":
                    s = smtplib.SMTP_SSL(a.smtp_host, a.smtp_port, timeout=30, context=ctx)
                else:
                    s = smtplib.SMTP(a.smtp_host, a.smtp_port, timeout=30)
                    s.starttls(context=ctx)
                s.login(a.smtp_username, a.smtp_password)
                s.quit()
                report(f"{a.name} SMTP 登录", True, f"{a.smtp_host}:{a.smtp_port}")
            except Exception as e:
                report(f"{a.name} SMTP 登录", False, f"{type(e).__name__}: {e}")
        else:
            print(f"➖ {a.name} 未配置 SMTP（只收不发）")
    from .llm import research_backend
    if cfg.llm.backend == "claude_cli" or research_backend(cfg) == "claude_cli":
        try:
            out = subprocess.run([cfg.llm.claude_bin, "--version"], capture_output=True, text=True, timeout=30)
            report("Claude CLI", out.returncode == 0, out.stdout.strip() or out.stderr.strip()[:200])
        except Exception as e:
            report("Claude CLI", False, str(e))
    if cfg.llm.backend == "openai":
        from .db import DB
        from .openai_compat import OpenAIChat
        for role in ("triage_model", "agent_model"):
            model = getattr(cfg.llm, role)
            try:
                msg = asyncio.run(OpenAIChat(cfg, DB(cfg.db_path)).chat(
                    model=model, messages=[{"role": "user", "content": "只回复 ok"}], timeout=60, kind="check"))
                report(f"模型接口 {model}", True, f"{cfg.llm.base_url} → {(msg.get('content') or '').strip()[:20]}")
            except Exception as e:
                report(f"模型接口 {model}", False, str(e)[:300])
    t = cfg.llm.triage
    if t.get("backend") == "openai":
        from .db import DB
        from .openai_compat import OpenAIChat
        try:
            msg = asyncio.run(OpenAIChat(cfg, DB(cfg.db_path), base_url=t["base_url"], api_key=t.get("api_key", "")).chat(
                model=t["model"], messages=[{"role": "user", "content": "只回复 ok"}], timeout=120, kind="check"))
            report(f"分级模型 {t['model']}", True, f"{t['base_url']} → {(msg.get('content') or '').strip()[:20]}")
        except Exception as e:
            report(f"分级模型 {t['model']}", False, str(e)[:300])
    rb = research_backend(cfg)
    hint = {"claude_cli": "Claude CLI（WebSearch / WebFetch）",
            "openai": "OpenAI 兼容接口 + " + ("Tavily 搜索" if cfg.llm.tavily_api_key else "只能打开网址（没配 Tavily，不能搜索）"),
            None: "未配置：Agent 不能上网查资料"}[rb]
    print(f"{'✅' if rb else '➖'} 联网研究：{hint}")
    if cfg.feishu.app_id and cfg.feishu.app_secret:
        try:
            r = httpx.post(cfg.feishu.domain.rstrip("/") + "/open-apis/auth/v3/tenant_access_token/internal",
                           json={"app_id": cfg.feishu.app_id, "app_secret": cfg.feishu.app_secret}, timeout=15)
            d = r.json()
            report("飞书应用凭证", d.get("code") == 0, d.get("msg", ""))
        except Exception as e:
            report("飞书应用凭证", False, str(e))
    else:
        report("飞书应用凭证", False, "secrets.toml 里还没填 [feishu] app_id / app_secret")
    print(f"{'✅' if cfg.bark.device_keys else '➖'} Bark：{len(cfg.bark.device_keys)} 个设备")
    return 0 if ok else 1


def _service(cfg: Config, dry_run: bool):
    from .bark import Bark
    from .config import BarkConfig
    from .db import DB
    from .feishu import FakeBot, FeishuBot
    from .llm import make_llm
    from .service import Service

    db = DB(cfg.db_path)
    if dry_run:
        bot = FakeBot(cfg.data_dir / "dryrun-outbox.jsonl")

        def no_smtp(acct, msg):
            log.warning("[dry-run] 不会真的发信：%s → %s", msg["Subject"], msg["To"])

        return Service(cfg, db, make_llm(cfg, db), bot, Bark(BarkConfig()), smtp_fn=no_smtp)
    if not (cfg.feishu.app_id and cfg.feishu.app_secret):
        raise ConfigError("还没配置飞书 app_id / app_secret；可以先用 `mailowl run --dry-run` 试跑")
    return Service(cfg, db, make_llm(cfg, db), FeishuBot(cfg.feishu))


def cmd_run(args) -> int:
    cfg = _cfg(args)
    setup_logging(cfg, args.verbose)
    svc = _service(cfg, args.dry_run)
    try:
        asyncio.run(svc.run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_pair(args) -> int:
    from .db import DB
    cfg = _cfg(args)
    db = DB(cfg.db_path)
    owner = cfg.feishu.owner_open_id or db.kv_get("owner_open_id")
    if owner and not args.reset:
        print(f"已绑定飞书用户：{owner}（如需重新绑定：mailowl pair --reset）")
        return 0
    if args.reset:
        for k in ("owner_open_id", "owner_chat_id", "pairing_code"):
            db.kv_set(k, None)
    from .service import pairing_code
    code = pairing_code(db)
    print(f"配对码：{code}\n在飞书里打开机器人单聊，发送这 6 位数字即可绑定。")
    return 0


def cmd_fetch(args) -> int:
    from .db import DB
    from .fetch import sync_account
    cfg = _cfg(args)
    setup_logging(cfg, True)
    db = DB(cfg.db_path)
    for a in cfg.accounts:
        if args.account and a.name != args.account:
            continue
        ids = sync_account(a, db, cfg.max_message_bytes)
        total = db.q1("SELECT COUNT(*) n FROM mails WHERE account=?", (a.name,))["n"]
        print(f"{a.name}: 新邮件 {len(ids)} 封（待分级），库中共 {total} 封")
    return 0


def cmd_triage(args) -> int:
    from .db import DB
    from .llm import make_llm
    from .triage import triage_mail
    cfg = _cfg(args)
    setup_logging(cfg, True)
    db = DB(cfg.db_path)
    ids = [args.mail_id] if args.mail_id else [r["id"] for r in db.q(
        "SELECT id FROM mails WHERE direction='in' ORDER BY date_ts DESC LIMIT ?", (args.latest,))]
    for i in ids:
        res = asyncio.run(triage_mail(cfg, db, make_llm(cfg, db), i))
        m = db.mail(i)
        print(f"#{i} {m['subject']!r}\n  {json.dumps(res, ensure_ascii=False)}")
    return 0


def cmd_agent(args) -> int:
    """在终端里跑一轮对话 Agent（不经飞书），用于验证查库/起草草稿。"""
    from .db import DB
    from .feishu import FakeBot
    from .llm import make_llm
    from .service import Service
    cfg = _cfg(args)
    setup_logging(cfg, args.verbose)
    db = DB(cfg.db_path)
    bot = FakeBot()
    svc = Service(cfg, db, make_llm(cfg, db), bot)
    scope = f"mail:{args.mail}" if args.mail else "cli:main"

    async def go():
        svc.owner_target = lambda: "cli"  # type: ignore[method-assign]
        await svc.run_agent(scope, args.text, None, args.mail)

    asyncio.run(go())
    for item in bot.sent:
        if item["kind"] == "markdown":
            print(item["text"])
        elif item["kind"] == "card":
            c = item["card"]
            print(f"\n[卡片] {c['header']['title']['content']}")
            for e in c["body"]["elements"]:
                if e.get("tag") == "div":
                    print("  " + e["text"]["content"].replace("\n", "\n  "))
    return 0


def cmd_folders(args) -> int:
    """列出 IMAP 邮箱的文件夹名（已解码），用于填写 folders / sent_folder。"""
    import re as _re
    from .fetch import IMAPSync, imap_utf7_decode
    cfg = _cfg(args)
    for a in cfg.accounts:
        if a.protocol != "imap" or (args.account and a.name != args.account):
            continue
        M = IMAPSync.connect(a)
        typ, boxes = M.list()
        M.logout()
        print(f"== {a.name}")
        for b in boxes or []:
            line = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
            m = _re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not m:
                print("  ", line)
                continue
            name = m.group("name").strip().strip('"')
            flags = m.group("flags")
            hint = "  ← 已发送" if "\\Sent" in flags else ""
            print(f"  {imap_utf7_decode(name)}{hint}")
    return 0


def cmd_bark_test(args) -> int:
    from .bark import Bark
    cfg = _cfg(args)
    b = Bark(cfg.bark, click_url=f"https://applink.feishu.cn/client/bot/open?appId={cfg.feishu.app_id}" if cfg.feishu.app_id else "")
    ok = asyncio.run(b.push("MailOwl 测试", "点开这条通知应跳到飞书机器人对话", level=args.level))
    print("已推送" if ok else "推送失败（看上面的日志）")
    return 0 if ok else 1


def cmd_service(args) -> int:
    """安装 / 卸载 / 查看 / 重启后台服务（macOS LaunchAgent）。"""
    from . import launchd
    if sys.platform != "darwin":
        print("后台服务目前只支持 macOS（launchd）。其他系统可以用 `mailowl run` 配合 systemd 等自行托管。")
        return 2
    if args.action == "uninstall":
        launchd.uninstall()
        print("已卸载后台服务")
        return 0
    cfg = _cfg(args)
    if args.action == "install":
        if not args.no_check and cmd_check(args) != 0:
            print("\n检查没有全部通过，先修好配置再安装服务（确实要装可以加 --no-check）")
            return 1
        info = launchd.install(cfg)
    elif args.action == "restart":
        info = launchd.restart()
    elif args.action == "logs":
        log_file = cfg.log_dir / "mailowl.log"
        lines = log_file.read_text("utf-8", "replace").splitlines()[-args.lines:] if log_file.exists() else []
        print("\n".join(lines) or f"还没有日志：{log_file}")
        return 0
    else:
        info = launchd.status()
    if not info.get("loaded"):
        print("后台服务未运行" + ("（已安装但没有加载）" if info.get("plist") else "（未安装，运行 mailowl service install）"))
        return 1 if args.action != "status" else 0
    print(f"后台服务：{info.get('state', '?')}" + (f"，pid {info['pid']}" if info.get("pid") else "")
          + (f"，上次退出码 {info['last_exit']}" if info.get("last_exit") else ""))
    print(f"日志：{cfg.log_dir / 'mailowl.log'}")
    return 0


def cmd_demo(args) -> int:
    """不接邮箱、不配飞书，用虚构邮件演示分级、起草和恶意邮件拦截。"""
    from .demo import run_demo
    return asyncio.run(run_demo(args.base_url, args.model, args.keep, args.lang))


def cmd_cards(args) -> int:
    """导出示例卡片 JSON，可粘贴到飞书「卡片搭建工具」里预览。"""
    from . import cards
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mail = {"id": 42, "level": 2, "status": "open", "subject": "Additional Information Required",
            "from_name": "Research Office", "from_addr": "research-office@uni.edu", "to_addrs": "me@uni.edu",
            "summary": "科研处请你补交更新后的 CV 和论文列表，用于基金申请。",
            "analysis": "需要准备材料并回复；邮件提到截止日期，建议先核对之前是否已提交。",
            "deadline": "2026-11-07", "attachments": json.dumps([{"filename": "form.pdf", "size": 120000}]),
            "triage": json.dumps({"tags": ["涉及附件", "待确认截止日期"], "action_items": ["更新 CV", "回复确认"]}),
            "body_text": "Dear Dr. ...", "body_truncated": 0}
    draft = {"id": 7, "version": 2, "mail_id": 42, "status": "draft", "account": "work",
             "to_addrs": "research-office@uni.edu", "cc_addrs": "", "subject": "Re: Additional Information Required",
             "body": "Dear Research Office,\n\nThank you for your reminder. ...\n\nBest regards,\n...", "note": "缩短了第二段",
             "error": None}
    samples = {
        "mail_card.json": cards.mail_card(mail, "工作邮箱"),
        "draft_card.json": cards.draft_card(draft, account_title="工作邮箱"),
        "digest_card.json": cards.digest_card("邮件摘要 · 08:30", [("待阅读", ["#40 张三 · 组会通知 — 周五下午"]),
                                                               ("通知 / 订阅", ["#41 GitHub · [repo] New issue"])],
                                              ["☐ 提交 CV（2026-11-07 18:00）"]),
    }
    for name, c in samples.items():
        (out / name).write_text(json.dumps(c, ensure_ascii=False, indent=2), "utf-8")
        print(out / name)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mailowl", description="Mac mini 邮件 Agent")
    p.add_argument("--config", help="config.toml 路径（默认 ~/.config/mailowl/config.toml）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="生成配置模板")
    sub.add_parser("check", help="检查邮箱登录、Claude CLI、飞书凭证")
    r = sub.add_parser("run", help="运行服务")
    r.add_argument("--dry-run", action="store_true", help="不连飞书、不发信、不推 Bark，卡片写到 dryrun-outbox.jsonl")
    r.add_argument("-v", "--verbose", action="store_true")
    pr = sub.add_parser("pair", help="显示飞书绑定配对码")
    pr.add_argument("--reset", action="store_true")
    f = sub.add_parser("fetch", help="立即收一次信（首次即回填历史）")
    f.add_argument("--account")
    t = sub.add_parser("triage", help="对库里的邮件跑一次分级并打印")
    t.add_argument("--mail-id", type=int)
    t.add_argument("--latest", type=int, default=3)
    ag = sub.add_parser("agent", help="在终端里和 Agent 对话一轮")
    ag.add_argument("text")
    ag.add_argument("--mail", type=int)
    ag.add_argument("-v", "--verbose", action="store_true")
    fo = sub.add_parser("folders", help="列出 IMAP 文件夹名")
    fo.add_argument("--account")
    b = sub.add_parser("bark-test", help="发一条 Bark 测试推送")
    b.add_argument("--level", default="timeSensitive")
    c = sub.add_parser("cards", help="导出示例卡片 JSON")
    c.add_argument("--out", default="tmp/cards")
    dm = sub.add_parser("demo", help="不接邮箱、不配飞书，用虚构邮件演示一遍")
    dm.add_argument("--base-url", help="OpenAI 兼容接口，例如 http://127.0.0.1:11434/v1（Ollama）；密钥用环境变量 MAILOWL_LLM_API_KEY")
    dm.add_argument("--model", help="模型名（配合 --base-url；用 Claude Code 时可写 haiku/sonnet/opus）")
    dm.add_argument("--keep", action="store_true", help="保留演示用的临时目录")
    dm.add_argument("--lang", choices=["en", "zh"], help="演示语言（默认跟随配置里的 language，没有配置则英文）")
    sv = sub.add_parser("service", help="后台服务：install / uninstall / status / restart / logs")
    sv.add_argument("action", choices=["install", "uninstall", "status", "restart", "logs"])
    sv.add_argument("--no-check", action="store_true", help="install 时跳过 mailowl check")
    sv.add_argument("-n", "--lines", type=int, default=40, help="logs 显示的行数")
    sub.add_parser("mcp", help="（供 claude 调用）运行 MCP 邮件工具服务器")
    args = p.parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        if args.config:
            os.environ["MAILOWL_CONFIG"] = args.config
        mcp_main()
        return 0
    if args.cmd not in ("run", "fetch", "triage", "agent"):
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    handlers = {"init": cmd_init, "check": cmd_check, "run": cmd_run, "pair": cmd_pair, "fetch": cmd_fetch,
                "triage": cmd_triage, "agent": cmd_agent, "bark-test": cmd_bark_test, "cards": cmd_cards,
                "folders": cmd_folders, "service": cmd_service,
                "demo": cmd_demo}
    try:
        return handlers[args.cmd](args)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
