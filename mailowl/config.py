"""配置加载：config.toml（可入库的结构配置）+ secrets.toml（密码、密钥，chmod 600）。

查找顺序：--config 参数 > $MAILOWL_CONFIG > ~/.config/mailowl/config.toml。
secrets.toml 与 config.toml 同目录。任何 secret 也可用环境变量覆盖，见 _secret()。
"""
from __future__ import annotations

import os
import shutil
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
    watch: bool = True                # 保持一条连接每 20 秒查一次有无新邮件（比定时整轮收信快得多；POP3 不适用）
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
BACKEND_TYPES = ("claude_cli", "openai", "codex_cli")
ROLES = {"triage": "分级", "agent": "对话起草", "research": "联网研究"}
DEFAULT_CLAUDE_BIN = "~/.local/bin/claude"
DEEPSEEK_HINT = '例如 DeepSeek 关闭思考：extra_body = { thinking = { type = "disabled" } }'


@dataclass
class Backend:
    """[llm.backends.<名字>]：一个模型后端。分级、对话、研究三个用途各指一个名字，可以相同。"""
    name: str
    type: str                         # claude_cli：本机已登录的 Claude Code；openai：OpenAI 兼容接口；codex_cli：本机已登录的 Codex
    model: str = ""                   # claude_cli 留空为 sonnet；codex_cli 留空用 Codex 的默认模型；openai 必填
    base_url: str = ""                # openai：如 https://api.deepseek.com/v1
    api_key: str = ""                 # openai：写在 secrets.toml 的 [llm.backends.<名字>]
    # openai：厂商特有的请求参数，原样并进请求体。DeepSeek 必须关思考：extra_body = { thinking = { type = "disabled" } }
    # （开着思考调用工具，要把思考内容逐轮传回，否则 400）
    extra_body: dict = field(default_factory=dict)
    bin: str = ""                     # claude_cli / codex_cli：可执行文件路径，留空自动查找
    effort: str = ""                  # codex_cli：推理强度，留空为 low


@dataclass
class LLMConfig:
    # 三个用途各用哪个 [llm.backends.<名字>]
    triage_backend: str = ""          # 每封新邮件分级；留空同 agent_backend
    agent_backend: str = ""           # 飞书里的对话、起草回信；只定义了一个后端时可以不写
    # 联网研究：独立的研究 Agent，读不到邮箱，每个问题都要用户在飞书卡片上同意；"none" 关闭；留空同 agent_backend
    research_backend: str = ""
    backends: dict[str, Backend] = field(default_factory=dict)
    triage_concurrency: int = 2
    triage_timeout: int = 180
    agent_timeout: int = 900
    max_body_chars: int = 12000
    max_tool_rounds: int = 12         # openai 后端：一轮对话里最多调用几次工具
    history_max_chars: int = 60000    # openai 后端：保留的对话历史长度
    tavily_api_key: str = ""          # openai 后端做研究时的搜索接口（tavily.com），写在 secrets.toml 的 [llm]

    def role(self, role: str) -> Backend | None:
        """triage / agent / research 实际用的后端；研究关闭时返回 None。"""
        name = getattr(self, f"{role}_backend")
        return None if name == "none" else self.backends[name]


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


def resolve_claude_bin(hint: str = "") -> str:
    if hint:
        return str(Path(hint).expanduser())
    p = Path(DEFAULT_CLAUDE_BIN).expanduser()
    return str(p) if p.exists() else (shutil.which("claude") or str(p))


def _backend(name: str, spec: Any, where: str) -> Backend:
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} 要写成表")
    if name == "none":
        raise ConfigError('后端不能叫 "none"：research_backend = "none" 表示关闭联网研究')
    if "name" in spec:
        raise ConfigError(f"{where} 里不用写 name，表名就是后端的名字")
    if spec.get("type") not in BACKEND_TYPES:
        raise ConfigError(f"{where} 的 type 只能是 claude_cli、openai 或 codex_cli")
    b = _fill(Backend, {"name": name, **spec}, where)
    for key, only in (("base_url", ("openai",)), ("api_key", ("openai",)), ("extra_body", ("openai",)),
                      ("effort", ("codex_cli",)), ("bin", ("claude_cli", "codex_cli"))):
        if getattr(b, key) and b.type not in only:
            raise ConfigError(f"{where} 的 {key} 只用于 type = {' / '.join(only)}")
    if not isinstance(b.extra_body, dict):
        raise ConfigError(f"{where} 的 extra_body 要写成表。{DEEPSEEK_HINT}")
    if b.type == "openai" and not (b.base_url and b.model):
        raise ConfigError(f"{where} 用 openai 时要写 base_url 和 model，例如 DeepSeek：https://api.deepseek.com/v1 + "
                          "deepseek-flash；本地 Ollama：http://127.0.0.1:11434/v1")
    if b.type != "claude_cli" and b.model in CLAUDE_ALIASES:
        raise ConfigError(f"{where} 的 model 要写该服务的模型名，haiku/sonnet/opus 只适用于 claude_cli")
    if b.type == "claude_cli":
        b.model, b.bin = b.model or "sonnet", resolve_claude_bin(b.bin)
    if b.type == "codex_cli":
        b.effort = b.effort or "low"
    return b


# 0.1.0 的写法：[llm] backend / claude_bin / base_url / *_model / research_backend / research_model，
# 外加可选的 [llm.triage] 表。加载时换算成命名后端，照旧能用。
LEGACY_KEYS = {"backend", "claude_bin", "base_url", "api_key", "triage_model", "agent_model", "extra_body",
               "research_model", "triage"}


def _legacy_backends(old: dict[str, Any], tavily: bool) -> tuple[dict[str, dict], dict[str, str], str, str, str]:
    """返回 (后端设置, 报错时用的出处, triage, agent, research)。"""
    main = old.get("backend", "claude_cli")
    if main not in ("claude_cli", "openai"):
        raise ConfigError("[llm] backend 只能是 claude_cli 或 openai")
    claude_bin = old.get("claude_bin", "")
    agent_model, triage_model = old.get("agent_model", "sonnet"), old.get("triage_model", "sonnet")
    if main == "openai":
        if not old.get("base_url"):
            raise ConfigError('[llm] backend = "openai" 时必须填 base_url，例如 https://api.deepseek.com/v1')
        bad = [k for k in ("triage_model", "agent_model") if old.get(k, "sonnet") in CLAUDE_ALIASES]
        if bad:
            raise ConfigError(f"[llm] 用 openai 后端时 {' / '.join(bad)} 要写该服务的模型名（例如 deepseek-flash），"
                              "haiku/sonnet/opus 只适用于 claude_cli")
    elif old.get("extra_body"):
        raise ConfigError(f'[llm] extra_body 只用于 backend = "openai"。{DEEPSEEK_HINT}')

    def like_main(model: str) -> dict:
        if main == "claude_cli":
            return {"type": "claude_cli", "model": model, "bin": claude_bin}
        return {"type": "openai", "model": model, "base_url": old["base_url"], "api_key": old.get("api_key", ""),
                "extra_body": old.get("extra_body", {})}

    specs: dict[str, dict] = {}
    where: dict[str, str] = {}

    def add(name: str, spec: dict, origin: str) -> str:   # 设置完全相同的后端只留一个
        spec = {k: v for k, v in spec.items() if v not in ("", {}, None)}
        for n, s in specs.items():
            if s == spec:
                return n
        specs[name], where[name] = spec, origin
        return name

    main_name = "claude" if main == "claude_cli" else "openai"
    agent = add(main_name, like_main(agent_model), "[llm]")
    t = dict(old.get("triage") or {})
    if t:
        extra = set(t) - {"backend", "base_url", "model", "api_key", "effort", "codex_bin", "extra_body"}
        if extra:
            raise ConfigError(f"[llm.triage] 中有未知配置项: {', '.join(sorted(extra))}")
        spec = {"type": t.pop("backend", "openai"), **t}
        if spec["type"] not in BACKEND_TYPES:
            raise ConfigError("[llm.triage] backend 只能是 claude_cli、openai 或 codex_cli")
        if "codex_bin" in spec:
            spec["bin"] = spec.pop("codex_bin")
        if spec["type"] == "claude_cli":
            spec = {"model": triage_model, "bin": claude_bin, **spec}
        triage = add("triage", spec, "[llm.triage]")
    else:
        triage = add(f"{main_name}-triage", like_main(triage_model), "[llm]")

    rb, rm = old.get("research_backend", "auto"), old.get("research_model", "")
    if rb == "auto":
        if main == "openai" and tavily:
            rb = "openai"
        elif Path(resolve_claude_bin(claude_bin)).exists():
            rb = "claude_cli"
        else:
            rb = "openai" if main == "openai" else "none"
    if rb == "none":
        research = "none"
    elif rb == "claude_cli":
        research = add("claude-research", {"type": "claude_cli", "bin": claude_bin,
                                           "model": rm or (agent_model if main == "claude_cli" else "sonnet")}, "[llm]")
    elif rb == "openai":
        if main != "openai":
            raise ConfigError('[llm] research_backend = "openai" 要求 backend = "openai"')
        research = add("openai-research", like_main(rm or agent_model), "[llm]")
    elif rb == "codex_cli":
        if rm in CLAUDE_ALIASES:
            raise ConfigError('[llm] research_backend = "codex_cli" 时 research_model 要写 Codex 的模型名（或留空用默认），'
                              "haiku/sonnet/opus 只适用于 claude_cli")
        tri = specs[triage]
        base = {k: v for k, v in tri.items() if k in ("model", "effort", "bin")} if tri["type"] == "codex_cli" else {}
        research = add("codex", {**base, "type": "codex_cli", **({"model": rm} if rm else {})}, "[llm]")
    else:
        raise ConfigError("[llm] research_backend 只能是 auto / claude_cli / openai / codex_cli / none")
    return specs, where, triage, agent, research


def _load_llm(raw: dict[str, Any], sec: dict[str, Any]) -> LLMConfig:
    raw, sec = dict(raw), dict(sec)
    raw.pop("agent_web", None)                            # 已废弃：对话 Agent 不再直接上网
    sec_backends = sec.pop("backends", {})
    legacy = sorted(LEGACY_KEYS & set(raw))
    if "backends" in raw:
        if legacy:
            raise ConfigError(f"[llm] 用了 [llm.backends] 就不要再写旧写法的 {', '.join(legacy)}：模型、地址都写进各个后端")
        if sec.pop("api_key", ""):
            raise ConfigError("secrets.toml 的 [llm] api_key 是旧写法：请移到对应的 [llm.backends.<名字>] 下")
        if "triage" in sec:
            raise ConfigError("secrets.toml 的 [llm.triage] 是旧写法：api_key 请移到对应的 [llm.backends.<名字>] 下")
        specs = raw.pop("backends")
        if not isinstance(specs, dict) or not specs:
            raise ConfigError("[llm.backends] 里至少要定义一个后端，例如 [llm.backends.claude] type = \"claude_cli\"")
        specs = {n: dict(v) if isinstance(v, dict) else v for n, v in specs.items()}
        where = {n: f"[llm.backends.{n}]" for n in specs}
    else:
        old = {k: raw.pop(k) for k in legacy}
        if "research_backend" in raw:
            old["research_backend"] = raw.pop("research_backend")
        for k in ("triage_backend", "agent_backend"):
            if k in raw:
                raise ConfigError(f"[llm] 写了 {k} 就要用 [llm.backends.<名字>] 定义后端")
        if "api_key" in sec:
            old["api_key"] = sec.pop("api_key")
        if isinstance(sec.get("triage"), dict):
            old["triage"] = {**old.get("triage", {}), **sec.pop("triage")}
        if os.environ.get(_env_key("llm", "api_key")):
            old["api_key"] = os.environ[_env_key("llm", "api_key")]
        if old.get("triage") and os.environ.get(_env_key("triage", "api_key")):
            old["triage"]["api_key"] = os.environ[_env_key("triage", "api_key")]
        tavily = bool(os.environ.get(_env_key("tavily", "api_key")) or sec.get("tavily_api_key"))
        specs, where, t, a, r = _legacy_backends(old, tavily)
        raw.update(triage_backend=t, agent_backend=a, research_backend=r)
    for name, v in sec_backends.items():
        if name not in specs:
            raise ConfigError(f"secrets.toml 里有 [llm.backends.{name}]，但 config.toml 里没有定义这个后端")
        specs[name] = {**specs[name], **v}
    for name in specs:
        if os.environ.get(_env_key("backend", name, "api_key")) and isinstance(specs[name], dict):
            specs[name]["api_key"] = os.environ[_env_key("backend", name, "api_key")]
    llm = _fill(LLMConfig, {**raw, **sec}, "[llm]")
    if os.environ.get(_env_key("tavily", "api_key")):
        llm.tavily_api_key = os.environ[_env_key("tavily", "api_key")]
    llm.backends = {n: _backend(n, s, where[n]) for n, s in specs.items()}
    names = ", ".join(llm.backends)
    if not llm.agent_backend:
        if len(llm.backends) != 1:
            raise ConfigError(f"[llm] 要写 agent_backend，指定对话起草用哪个后端（已定义：{names}）")
        llm.agent_backend = next(iter(llm.backends))
    llm.triage_backend = llm.triage_backend or llm.agent_backend
    llm.research_backend = llm.research_backend or llm.agent_backend
    for role in ROLES:
        n = getattr(llm, f"{role}_backend")
        if n == "auto" and n not in llm.backends:
            off = '，或 "none" 关闭' if role == "research" else ""
            raise ConfigError(f'[llm] {role}_backend = "auto" 是 0.1.0 的写法：用了 [llm.backends] 就写一个后端的名字'
                              f"{off}（已定义：{names}）")
        if n not in llm.backends and not (role == "research" and n == "none"):
            raise ConfigError(f'[llm] {role}_backend = "{n}" 没有对应的 [llm.backends.{n}]（已定义：{names}）')
    if llm.backends[llm.agent_backend].type == "codex_cli":
        raise ConfigError("Codex 没有读邮件、存草稿的工具，不能做对话和起草：agent_backend 请选 claude_cli 或 openai 类型的后端")
    return llm


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
    llm = _load_llm(raw.get("llm", {}), secrets.get("llm", {}))
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
    if cfg.profile.strip().startswith("（例"):  # 旧模板里的示例文字，没改过就当没填
        cfg.profile = ""
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
