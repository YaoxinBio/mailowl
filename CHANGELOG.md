# Changelog

## Unreleased

- `mailowl demo`: zero-config demo with fictional mail (triage, drafting from history, prompt-injection email).
- `mailowl service install|uninstall|status|restart|logs`; works with `uv tool install` / `pipx`.
- Config templates ship inside the package, so `mailowl init` works from any install.
- OpenAI-compatible backend verified with a local Ollama model; local/LAN model URLs bypass proxies; SOCKS proxies supported.
- Docs: forwarding workaround for mailboxes without app passwords.

## 0.1.0 — 2026-11-01

First public version.

- Read-only IMAP / POP3 fetching for multiple mailboxes; first run stores recent mail as history without alerts.
- LLM triage into four levels with summary, deadline and action items; automatic todos.
- Feishu / Lark bot: mail cards, chat about a thread, drafts with revisions, one-tap signature templates, attachments.
- Confirm-to-send over SMTP, at most once per draft; uncertain outcomes are never retried.
- Bark alerts for important and urgent mail, quiet hours, escalation.
- Isolated research agent: web lookups only after approval, with no mailbox access.
- Model backends: Claude Code CLI, or any OpenAI-compatible API.
