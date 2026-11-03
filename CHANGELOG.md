# Changelog

## Unreleased

- `[llm] extra_body` does the same for the main OpenAI-compatible backend, so chat and drafting can run on DeepSeek with thinking off (with thinking on, DeepSeek rejects tool calls unless the reasoning is passed back). When chat is not on Claude, Claude-based research defaults to sonnet instead of reusing the chat model name.
- `[llm.triage] extra_body` passes vendor-specific request fields to an OpenAI-compatible triage model, e.g. `{ thinking = { type = "disabled" } }` to turn off DeepSeek's thinking mode. DeepSeek examples now use `deepseek-flash`.
- Triage can run on the Codex CLI (`[llm.triage] backend = "codex_cli"`), using the ChatGPT plan on the host. Codex gets its own `CODEX_HOME` (so the user's `AGENTS.md` and config are not loaded) and, on macOS, an outer `sandbox-exec` profile: it cannot run commands or read MailOwl's config and data directories.
- Digests are now logged: sent (with counts and the message id), skipped because nothing is new, or not sent because no chat user is paired.

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
