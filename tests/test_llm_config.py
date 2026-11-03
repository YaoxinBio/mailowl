"""[llm.backends.<名字>] 命名后端：三个用途各指一个，换模型只改一行；旧写法（0.1.0）加载时换算。"""
import pytest

from mailowl.codex_cli import CodexCLI
from mailowl.config import ConfigError, load_config
from mailowl.db import DB
from mailowl.llm import ClaudeCLI, make_llm, research_backend
from mailowl.openai_compat import OpenAIChat

NEW = '''[llm]
triage_backend = "deepseek"
agent_backend = "deepseek"
research_backend = "codex"

[llm.backends.deepseek]
type = "openai"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
extra_body = { thinking = { type = "disabled" } }

[llm.backends.claude]
type = "claude_cli"

[llm.backends.codex]
type = "codex_cli"
model = "gpt-6.1-sol"

'''


def write(cfg, llm: str, secrets: str = ""):
    p = cfg.path
    p.write_text(p.read_text("utf-8").replace("[general]", llm + "[general]", 1), "utf-8")
    if secrets:
        sec = p.with_name("secrets.toml")
        sec.write_text(sec.read_text("utf-8") + "\n" + secrets, "utf-8")
    return p


def test_each_role_uses_its_named_backend(cfg):
    p = write(cfg, NEW, '[llm.backends.deepseek]\napi_key = "sk-ds"\n')
    c = load_config(p)
    r = make_llm(c, DB(c.db_path))
    assert isinstance(r.main, OpenAIChat) and r.triager is r.main, "同一个后端只建一个实例"
    assert r.main.api_key == "sk-ds" and r.main.extra_body == {"thinking": {"type": "disabled"}}
    assert isinstance(r.researcher, CodexCLI) and r.researcher.model == "gpt-6.1-sol"
    assert c.llm.backends["claude"].model == "sonnet" and c.llm.backends["codex"].effort == "low"
    text = p.read_text("utf-8")
    p.write_text(text.replace('research_backend = "codex"', 'research_backend = "claude"'), "utf-8")
    assert isinstance(make_llm(load_config(p), None).researcher, ClaudeCLI), "换研究后端只改一行"
    p.write_text(text.replace('research_backend = "codex"', 'research_backend = "none"'), "utf-8")
    c = load_config(p)
    assert research_backend(c) is None and make_llm(c, None).researcher is None


def test_roles_default_to_the_only_backend(cfg):
    c = load_config(write(cfg, '[llm.backends.local]\ntype = "openai"\nbase_url = "http://127.0.0.1:11434/v1"\n'
                               'model = "qwen3:8b"\n'))
    assert (c.llm.triage_backend, c.llm.agent_backend, c.llm.research_backend) == ("local", "local", "local")


@pytest.mark.parametrize("llm, secrets, match", [
    (NEW.replace('agent_backend = "deepseek"', 'agent_backend = "codex"'), "", "Codex"),
    (NEW.replace('research_backend = "codex"', 'research_backend = "nope"'), "", "nope"),
    (NEW.replace("[llm]\n", '[llm]\nbackend = "openai"\n', 1), "", "旧写法"),
    (NEW, '[llm.backends.ghost]\napi_key = "x"\n', "ghost"),
    (NEW, '[llm]\napi_key = "sk-old"\n', "api_key"),
    (NEW.replace('base_url = "https://api.deepseek.com/v1"\n', ""), "", "base_url"),
    (NEW.replace('model = "gpt-6.1-sol"', 'model = "sonnet"'), "", "sonnet"),
    (NEW.replace('type = "claude_cli"', 'type = "claude_cli"\neffort = "low"'), "", "effort"),
    (NEW.replace('type = "codex_cli"', 'type = "gpt"'), "", "type"),
    (NEW.replace('agent_backend = "deepseek"\n', ""), "", "agent_backend"),
])
def test_config_errors_say_what_to_fix(cfg, llm, secrets, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(cfg, llm, secrets))


def test_empty_legacy_secret_is_ignored(cfg):
    load_config(write(cfg, NEW, '[llm]\napi_key = ""\ntavily_api_key = ""\n'))   # 0.1.0 的 secrets 模板


def test_legacy_config_is_translated(cfg):
    c = load_config(write(cfg, '[llm]\ntriage_model = "haiku"\n'))
    assert c.llm.role("agent").model == "sonnet" and c.llm.role("triage").model == "haiku"
    assert c.llm.triage_backend != c.llm.agent_backend
    p = cfg.path
    p.write_text(p.read_text("utf-8").replace('triage_model = "haiku"\n',
                 'backend = "openai"\nbase_url = "https://api.deepseek.com/v1"\ntriage_model = "deepseek-flash"\n'
                 'agent_model = "deepseek-flash"\nresearch_backend = "claude_cli"\n'), "utf-8")
    c = load_config(p)
    assert c.llm.triage_backend == c.llm.agent_backend and c.llm.role("agent").type == "openai"
    assert c.llm.role("research").type == "claude_cli" and c.llm.role("research").model == "sonnet", \
        "对话不用 Claude 时，Claude 研究不能拿 deepseek 的模型名"
