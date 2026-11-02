# MailOwl setup

[简体中文](SETUP.md)

MailOwl runs on an always-on Mac (called "the host" below, e.g. a Mac mini at home). Configuration lives in `~/.config/mailowl/`, data in `~/.local/share/mailowl/`.

## 0. Install

Requires Python 3.11+. By default MailOwl calls models through the [Claude Code](https://claude.com/claude-code) CLI that is already logged in on the host; any OpenAI-compatible API works too (see section 5).

```bash
uv tool install git+https://github.com/YaoxinBio/mailowl   # or pipx install …; to hack on it, git clone and pip install -e .
mailowl init                                               # writes config templates to ~/.config/mailowl/
```

Do the next four steps in order: **Feishu only lets you save the long-connection setting while the service is online**, so fill in the credentials and start the service before going back to the console.

## 1. Mailboxes: get an app password

Mail is fetched over IMAP (recommended — MailOwl reads your Sent folder to tell whether you already replied) or POP3, and sent over SMTP. Always use an **app password / authorization code**, not your web login password.

| Mailbox | Incoming | Outgoing | Sent folder | Notes |
|---|---|---|---|---|
| Gmail | imap.gmail.com:993 | smtp.gmail.com:465 | `[Gmail]/Sent Mail` (localized, e.g. `[Gmail]/已发邮件` in Chinese) | Turn on 2-Step Verification, then create an app password; Gmail saves sent mail itself, keep `save_to_sent = false` |
| QQ Mail | imap.qq.com:993 | smtp.qq.com:465 | `Sent Messages` | Settings → Account → enable IMAP/SMTP → generate an authorization code |
| Tencent Exmail | imap.exmail.qq.com:993 | smtp.exmail.qq.com:465 | `Sent Messages` | The admin must allow client login |
| 163 / 126 | imap.163.com:993 | smtp.163.com:465 | `已发送` | Enable IMAP and generate an authorization code; **requires `imap_id = true`** |
| Other (work, university, self-hosted) | see the provider's IMAP/client settings page | same | find it with `mailowl folders` | Some require enabling client login in webmail first; many do not save SMTP-sent mail, set `save_to_sent = true` |
| Outlook / Hotmail / Microsoft 365 | — | — | — | Microsoft only allows OAuth for IMAP; not supported yet — see the workaround below |

**Mailbox without app passwords** (e.g. a university or company Microsoft 365 account): in its webmail, set up automatic forwarding of a copy to a supported mailbox (Gmail, QQ, 163 …) and connect that one to MailOwl. Trade-off: replies are sent from the forwarding target, and MailOwl cannot see what you sent from the original mailbox.

Not sure about folder names? Start with `folders = ["INBOX"]` and `sent_folder = ""`, run `mailowl folders` (names are decoded, the Sent folder is marked), then fill them in.

## 2. Feishu / Lark: create a custom app bot

1. Open <https://open.feishu.cn/app> → "Create custom app", e.g. "MailOwl". (Lark users: <https://open.larksuite.com/app> and set `[feishu] domain = "https://open.larksuite.com"` — not tested yet.)
2. "Add features" → **Bot**.
3. "Permissions & Scopes" → enable:
   - `im:message` (read and send messages)
   - `im:message:send_as_bot` (send as the app)
   - `im:message.p2p_msg:readonly` (read direct messages sent to the bot)
   - `im:resource` (download files you send to the bot, used as draft attachments)
   - `im:message.reactions:write_only` (optional: shows a "typing" reaction while working)
4. "Credentials & Basic Info" → copy the **App ID** and **App Secret** into `[feishu]` in `~/.config/mailowl/secrets.toml` on the host.
5. Start the service on the host (step 4).
6. Back in the console, "Events & Callbacks":
   - Event configuration → subscription mode **Receive events through persistent connection** → save → add event **`im.message.receive_v1`** (receive messages);
   - Callback configuration → **Receive callbacks through persistent connection** → save → add callback **`card.action.trigger`** (card interactions).
7. "Version Management & Release" → create a version, availability **only me** → publish (you are the admin of your own organization, so it is approved immediately).
8. Search for the bot in Feishu, open a direct chat and send the 6-digit code shown by `mailowl pair`. You should see a confirmation.

After changing permissions, publish a new version for them to take effect.

## 3. Bark: urgent alerts on iPhone

1. Install Bark from the App Store. The sample URL on its home screen looks like `https://api.day.app/XXXXXXXX/...`; `XXXXXXXX` is your device key.
2. Put it in `secrets.toml`: `[bark] device_keys = ["XXXXXXXX"]`.
3. iPhone Settings → Notifications → Bark → enable **Time Sensitive Notifications**, otherwise urgent mail stays silent in Focus modes.
4. `mailowl bark-test` sends a test; tapping it should open the bot chat in Feishu.

## 4. Run and operate (on the host)

```bash
nano ~/.config/mailowl/config.toml      # mailboxes, profile, rules, signatures
nano ~/.config/mailowl/secrets.toml     # app passwords, Feishu credentials, Bark key
mailowl check                           # checks every login
mailowl fetch                           # first fetch: last 30 days stored as history (max 500 per folder), no alerts
mailowl pair                            # pairing code
mailowl service install                 # run as a LaunchAgent (starts at login, restarts on crash)
mailowl service status                  # likewise: restart / logs / uninstall
```

If you develop on another machine, `deploy/deploy.sh --restart [host]` rsyncs the code, installs dependencies, runs the tests and restarts the service.

## 5. Other models (optional)

The default is the Claude Code CLI logged in on the host. Any OpenAI-compatible API works as well (DeepSeek, Qwen, Kimi, Zhipu, OpenAI, Grok, OpenRouter, local Ollama):

```toml
# config.toml
[llm]
backend = "openai"
base_url = "https://api.deepseek.com/v1"
triage_model = "deepseek-chat"
agent_model = "deepseek-chat"
```

```toml
# secrets.toml
[llm]
api_key = "sk-..."
```

Run `mailowl check` afterwards; it makes one real call to confirm the model works. The chat agent calls tools, so pick a model with function calling (pure reasoning models such as deepseek-reasoner will not work).


To keep triage on your machine (no new message is sent to a model vendor) while drafting still uses Claude:

```toml
# config.toml
[llm.triage]
backend = "openai"
base_url = "http://127.0.0.1:11434/v1"   # local Ollama
model = "qwen3:8b"
```

Triage can also run on the Codex CLI logged in on the host (`codex login`), using your ChatGPT plan — handy when Claude usage is tight:

```toml
[llm.triage]
backend = "codex_cli"
model = "gpt-6.1-sol"      # omit to use Codex's default model
effort = "low"             # reasoning effort; low is enough for triage
```

MailOwl gives Codex its own `CODEX_HOME` (only `auth.json` is symlinked), so your `~/.codex/config.toml` and `AGENTS.md` are not loaded. On macOS it also wraps Codex in an outer sandbox: Codex cannot run any command and cannot read MailOwl's config or data directories. Each message takes about 20–30 s, mostly CLI start-up, and about 5–6 MB of traffic.

Other privacy switches are listed under "Privacy mode" in the README: `body_in_feishu = "never"`, `[bark] content = "minimal"`, `research_backend = "none"`.

## Privacy and safety boundaries

- Raw mail is stored only in the host's SQLite database (`~/.local/share/mailowl/mailowl.db`). Attachments are not downloaded; they are fetched from the server only when you attach them to a draft, kept temporarily, and deleted once the draft is sent or discarded.
- On the first backfill, messages larger than 300 KB are fetched as headers plus the beginning of the body (the log reports the maximum download size).
- Feishu receives only sender, subject and an AI summary by default; the body is sent only when you tap "View original" (`body_in_feishu = "never"` turns that off).
- Every new message is triaged by the model. Keep sensitive mail away from it with `[rules] local_only_senders / local_only_keywords`; those messages are classified by rules only.
- The chat agent that can read your mail cannot browse the web. When it needs online information it can only propose a question; after you approve it on a card, a separate research agent that cannot read your mailbox looks it up. A prompt hidden in an email therefore has no path to send your mail to a website.
- Only the paired Feishu account can operate the bot. The agent has no send tool and can only save drafts. Mail is sent only after you tap "Confirm send" (with a second confirmation); each draft is sent at most once, and if the outcome is uncertain it is never retried automatically.
