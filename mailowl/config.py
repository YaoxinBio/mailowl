"""配置加载：config.toml（可入库的结构配置）+ secrets.toml（密码、密钥，chmod 600）。

查找顺序：--config 参数 > $MAILOWL_CONFIG > ~/.config/mailowl/config.toml。
secrets.toml 与 config.toml 同目录。任何 secret 也可用环境变量覆盖，见 _secret()。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path("~/.config/mailowl").expanduser()


class ConfigError(Exception):
    pass


LANG_DIRECTIVE = {
    "zh": "所有写给用户的文字（摘要、分析、回答）用简体中文。",
    "en": "Write everything addressed to the user (summaries, analysis, answers) in English.",
}


def lang_directive(cfg: "Config") -> str:
    return LANG_DIRECTIVE.get(cfg.language, LANG_DIRECTIVE["zh"])


@dataclass
class Account:
    name: str
    address: str
    protocol: str = "imap"            # imap | pop3
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    label: str = ""
    display_name: str = ""
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    sent_folder: str = ""             # IMAP：把已发送邮件也收进历史，供"我回复过没有"判断
    poll_seconds: int = 180
    backfill_days: int = 30           # 首次接入时回填多少天作为历史（不推送）
    backfill_max: int = 500
    imap_id: bool = False             # 163/126 等要求登录后发送 IMAP ID
    idle: bool = True                 # IMAP IDLE：新邮件到达时服务器立即通知（不支持的服务器自动退回轮询）
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"        # ssl | starttls
    smtp_username: str = ""
    smtp_password: str = ""
    save_to_sent: bool = False        # 发出后 IMAP APPEND 到 sent_folder（Gmail/QQ 会自动保存，不要开）
    affiliation: str = ""             # 落款里的单位（中文），替换落款模板中的 {affiliation}
    affiliation_en: str = ""          # 英文单位，替换 {affiliation_en}
    enabled: bool = True

    @property
    def title(self) -> str:
        return self.label or self.name

    @property
    def can_send(self) -> bool:
        return bool(self.smtp_host)


CLAUDE_ALIASES = ("haiku", "sonnet", "opus")


@dataclass
class LLMConfig:
    backend: str = "claude_cli"       # claude_cli：本机已登录的 Claude Code；openai：任何 OpenAI 兼容接口
    claude_bin: str = "~/.local/bin/claude"
    base_url: str = ""                # openai 后端：如 https://api.deepseek.com/v1
    api_key: str = ""                 # openai 后端的密钥，写在 secrets.toml 的 [llm]
    triage_model: str = "sonnet"
    agent_model: str = "sonnet"
    triage_concurrency: int = 2
    triage_timeout: int = 180
    agent_timeout: int = 900
    max_body_chars: int = 12000
    max_tool_rounds: int = 12         # openai 后端：一轮对话里最多调用几次工具
    history_max_chars: int = 60000    # openai 后端：保留的对话历史长度
    # 上网查资料由独立的研究 Agent 完成：它读不到邮箱，每次都要用户在飞书卡片上同意
    research_backend: str = "auto"    # auto | claude_cli | openai | none
    research_model: str = ""          # 留空用 agent_model
    tavily_api_key: str = ""          # openai 研究后端的搜索接口（tavily.com），写在 secrets.toml 的 [llm]
    agent_web: bool = False           # 已废弃：对话 Agent 不再直接上网，保留只为兼容旧配置
    # 可选：分级单独用另一个模型，例如本地 Ollama，让每封新邮件都不交给模型厂商；对话起草仍用上面的设置。
    # [llm.triage] backend = "openai" / base_url / model；api_key 写在 secrets.toml 的 [llm.triage]
    triage: dict = field(default_factory=dict)


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    domain: str = "https://open.feishu.cn"
    owner_open_id: str = ""           # 留空则走配对码绑定
    body_in_feishu: str = "on_click"  # on_click：点「查看原文」才发正文；never：永不发正文
    body_max_chars: int = 6000


@dataclass
class BarkConfig:
    server: str = "https://api.day.app"
    device_keys: list[str] = field(default_factory=list)
    group: str = "MailOwl"
    icon: str = ""
    level2_push: str = "active"       # 重要邮件的 Bark 级别：active | timeSensitive | passive | none
    quiet_hours: str = "23:30-07:30"  # 期间只推送最高级（紧急）
    escalate_after_minutes: int = 20  # 紧急邮件无人处理时再次响铃
    max_per_hour: int = 12
    coalesce_seconds: int = 30
    content: str = "full"             # full：标题带主题、正文带摘要；minimal：只提示「有新提醒」，不带任何邮件内容


@dataclass
class RoutingConfig:
    level1: str = "digest"            # digest | immediate
    digest_times: list[str] = field(default_factory=lambda: ["08:30", "18:30"])
    digest_include_level0: bool = True
    urgent_hours: int = 48            # 截止时间在此范围内的重要邮件升为紧急
    todo_remind_before_minutes: int = 120


@dataclass
class Rules:
    vip_senders: list[str] = field(default_factory=list)
    mute_senders: list[str] = field(default_factory=list)
    local_only_senders: list[str] = field(default_factory=list)
    local_only_keywords: list[str] = field(default_factory=list)


@dataclass
class Signature:
    name: str                         # 卡片按钮上显示的名字，如「熟人」「正式」「英文」
    text: str                         # 落款全文，可用 {affiliation} / {affiliation_en}

    def render(self, acct: "Account | None") -> str:
        out = self.text.replace("{affiliation}", acct.affiliation if acct else "")
        out = out.replace("{affiliation_en}", acct.affiliation_en if acct else "")
        return "\n".join(line for line in out.strip("\n").split("\n")).rstrip()


@dataclass
class Config:
    path: Path
    data_dir: Path
    timezone: str = "Asia/Shanghai"
    profile: str = ""
    language: str = "zh"              # zh | en：模型写给用户的摘要、分析、回答用哪种语言（草稿仍跟随原邮件语言）
    signature: str = ""               # 署名习惯（自由文字），对话 Agent 起草时遵守
    max_message_bytes: int = 3_000_000
    llm: LLMConfig = field(default_factory=LLMConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    bark: BarkConfig = field(default_factory=BarkConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    rules: Rules = field(default_factory=Rules)
    accounts: list[Account] = field(default_factory=list)
    signatures: list[Signature] = field(default_factory=list)

    def signature_by_name(self, name: str | None) -> Signature | None:
        return next((s for s in self.signatures if s.name == name), None)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mailowl.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def agent_workdir(self) -> Path:
        return self.data_dir / "agent"

    def account(self, name: str) -> Account:
        for a in self.accounts:
            if a.name == name:
                return a
        raise ConfigError(f"未知邮箱账户: {name}")


def _fill(dc_cls, data: dict[str, Any], where: str):
    known = dc_cls.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{where} 中有未知配置项: {', '.join(sorted(unknown))}")
    return dc_cls(**data)


def _env_key(*parts: str) -> str:
    return "MAILOWL_" + "_".join(p.upper().replace("-", "_") for p in parts)


def resolve_config_path(path: str | os.PathLike | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    if os.environ.get("MAILOWL_CONFIG"):
        return Path(os.environ["MAILOWL_CONFIG"]).expanduser()
    return DEFAULT_CONFIG_DIR / "config.toml"


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        raise ConfigError(f"找不到配置文件 {cfg_path}，先运行 `mailowl init`")
    raw = tomllib.loads(cfg_path.read_text("utf-8"))
    sec_path = cfg_path.with_name("secrets.toml")
    secrets = tomllib.loads(sec_path.read_text("utf-8")) if sec_path.exists() else {}
    if sec_path.exists() and sec_path.stat().st_mode & 0o077:
        raise ConfigError(f"{sec_path} 权限过宽，请执行 chmod 600 {sec_path}")

    general = dict(raw.get("general", {}))
    data_dir = Path(general.pop("data_dir", "~/.local/share/mailowl")).expanduser()

    def section(name, cls):
        data = dict(raw.get(name, {}))
        for k, v in secrets.get(name, {}).items():        # 子表（如 [llm.triage]）按键合并
            data[k] = {**data[k], **v} if isinstance(v, dict) and isinstance(data.get(k), dict) else v
        return _fill(cls, data, f"[{name}]")

    feishu = section("feishu", FeishuConfig)
    bark = section("bark", BarkConfig)
    for attr, env in (("app_id", _env_key("feishu", "app_id")), ("app_secret", _env_key("feishu", "app_secret"))):
        if os.environ.get(env):
            setattr(feishu, attr, os.environ[env])
    llm = section("llm", LLMConfig)
    for attr, env in (("api_key", _env_key("llm", "api_key")), ("tavily_api_key", _env_key("tavily", "api_key"))):
        if os.environ.get(env):
            setattr(llm, attr, os.environ[env])
    if llm.triage and os.environ.get(_env_key("triage", "api_key")):
        llm.triage["api_key"] = os.environ[_env_key("triage", "api_key")]
    if os.environ.get(_env_key("bark", "keys")):
        bark.device_keys = [k for k in os.environ[_env_key("bark", "keys")].split(",") if k]

    accounts = []
    seen = set()
    for item in raw.get("accounts", []):
        item = dict(item)
        name = item.get("name")
        if not name:
            raise ConfigError("[[accounts]] 缺少 name")
        if name in seen:
            raise ConfigError(f"邮箱账户 name 重复: {name}")
        seen.add(name)
        item.update(secrets.get("accounts", {}).get(name, {}))
        acct = _fill(Account, item, f"[[accounts]] {name}")
        acct.protocol = acct.protocol.lower()
        if acct.protocol not in ("imap", "pop3"):
            raise ConfigError(f"{name}: protocol 只能是 imap 或 pop3")
        acct.port = acct.port or (993 if acct.protocol == "imap" else 995)
        acct.username = acct.username or acct.address
        env_pw = os.environ.get(_env_key("acct", name, "password"))
        if env_pw:
            acct.password = env_pw
        acct.smtp_username = acct.smtp_username or acct.username
        acct.smtp_password = acct.smtp_password or acct.password
        if acct.protocol == "pop3" and acct.sent_folder:
            raise ConfigError(f"{name}: POP3 不支持 sent_folder")
        accounts.append(acct)

    sigs = [_fill(Signature, dict(x), "[[signatures]]") for x in raw.get("signatures", [])]
    if len({x.name for x in sigs}) != len(sigs):
        raise ConfigError("[[signatures]] 的 name 有重复")

    general_known = {"timezone", "profile", "language", "signature", "max_message_bytes"}
    unknown = set(general) - general_known
    if unknown:
        raise ConfigError(f"[general] 中有未知配置项: {', '.join(sorted(unknown))}")

    cfg = Config(
        path=cfg_path,
        data_dir=data_dir,
        llm=llm,
        feishu=feishu,
        bark=bark,
        routing=section("routing", RoutingConfig),
        rules=section("rules", Rules),
        accounts=accounts,
        signatures=sigs,
        **general,
    )
    cfg.llm.claude_bin = str(Path(cfg.llm.claude_bin).expanduser())
    if cfg.profile.strip().startswith("（例"):  # 旧模板里的示例文字，没改过就当没填
        cfg.profile = ""
    if cfg.llm.backend not in ("claude_cli", "openai"):
        raise ConfigError("[llm] backend 只能是 claude_cli 或 openai")
    if cfg.llm.research_backend not in ("auto", "claude_cli", "openai", "none"):
        raise ConfigError("[llm] research_backend 只能是 auto / claude_cli / openai / none")
    if cfg.llm.backend == "openai":
        if not cfg.llm.base_url:
            raise ConfigError("[llm] backend = \"openai\" 时必须填 base_url，例如 https://api.deepseek.com/v1")
        bad = [k for k in ("triage_model", "agent_model") if getattr(cfg.llm, k) in CLAUDE_ALIASES]
        if bad:
            raise ConfigError(f"[llm] 用 openai 后端时 {' / '.join(bad)} 要写该服务的模型名（例如 deepseek-chat），"
                              "haiku/sonnet/opus 只适用于 claude_cli")
    t = cfg.llm.triage
    if t:
        extra = set(t) - {"backend", "base_url", "model", "api_key"}
        if extra:
            raise ConfigError(f"[llm.triage] 中有未知配置项: {', '.join(sorted(extra))}")
        t.setdefault("backend", "openai")
        if t["backend"] not in ("claude_cli", "openai"):
            raise ConfigError("[llm.triage] backend 只能是 claude_cli 或 openai")
        if t["backend"] == "openai" and not (t.get("base_url") and t.get("model")):
            raise ConfigError("[llm.triage] 用 openai 时要写 base_url 和 model，例如本地 Ollama：http://127.0.0.1:11434/v1")
        if t["backend"] == "openai" and t["model"] in CLAUDE_ALIASES:
            raise ConfigError("[llm.triage] 的 model 要写该服务的模型名，haiku/sonnet/opus 只适用于 claude_cli")
    if cfg.language not in LANG_DIRECTIVE:
        raise ConfigError("[general] language 只能是 zh 或 en")
    if cfg.bark.content not in ("full", "minimal"):
        raise ConfigError("[bark] content 只能是 full 或 minimal")
    if cfg.routing.level1 not in ("digest", "immediate"):
        raise ConfigError("[routing] level1 只能是 digest 或 immediate")
    if cfg.feishu.body_in_feishu not in ("on_click", "never"):
        raise ConfigError("[feishu] body_in_feishu 只能是 on_click 或 never")
    return cfg


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.data_dir, cfg.log_dir, cfg.agent_workdir):
        d.mkdir(parents=True, exist_ok=True)
