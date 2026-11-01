# Changelog

## 0.1.0 — 2026-11-01

First public version.

- Read-only IMAP / POP3 fetching for multiple mailboxes; first run stores recent mail as history without alerts.
- LLM triage into four levels with summary, deadline and action items; automatic todos.
- Feishu / Lark bot: mail cards, chat about a thread, drafts with revisions, one-tap signature templates, attachments.
- Confirm-to-send over SMTP, at most once per draft; uncertain outcomes are never retried.
- Bark alerts for important and urgent mail, quiet hours, escalation.
- Isolated research agent: web lookups only after approval, with no mailbox access.
- Model backends: Claude Code CLI, or any OpenAI-compatible API.
- `mailowl demo`: zero-config demo with fictional mail (triage, drafting from history, prompt-injection email).
- `mailowl service install|uninstall|status|restart|logs`; works with `uv tool install` / `pipx`.
- Config templates ship inside the package, so `mailowl init` works from any install.
- OpenAI-compatible backend verified with a local Ollama model; local/LAN model URLs bypass proxies; SOCKS proxies supported.
- Docs: forwarding workaround for mailboxes without app passwords.
- `language = "en" | "zh"` for summaries, analysis and answers; `mailowl demo --lang en|zh` with everyday, non-domain-specific examples.
- New mail is noticed within ~20 s (a STATUS check every 20 s on a kept-open connection; IMAP IDLE was tried and dropped because QQ Mail does not push reliably and Coremail lacks it); a failed triage retries once and then pushes the mail immediately.
- Cards whose mail, draft, todo or research request no longer exists are marked stale instead of erroring.
- Privacy mode: `[llm.triage]` routes triage to a separate (e.g. local) model; `[bark] content = "minimal"` keeps mail content out of push notifications.
