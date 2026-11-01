# Contributing to MailOwl

[简体中文见下方](#参与贡献)

Thanks for your interest! MailOwl handles people's private mail, so changes are reviewed with safety first.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Tests run offline: mailboxes, the chat bot, SMTP and models are all faked. Please add a test for every behavior change.

## Ground rules

- **Never give the agent a way to send mail.** Sending happens only from the *Confirm send* card action, through `db.claim_draft_for_send`.
- **Keep the mail-reading agent offline.** Web access belongs to the separate research agent, which must not see mailbox tools.
- **No personal data** in code, tests, fixtures or screenshots. Use example.org addresses and made-up names.
- Keep dependencies minimal (currently `lark-channel-sdk` and `httpx`).
- Match the surrounding style; comments explain *why*, not *what*.

## Pull requests

Small, focused PRs are easiest to review. Describe how you tested, including any real-device testing.

---

## 参与贡献

感谢关注！MailOwl 处理的是用户的私人邮件，所以每个改动都以安全为先。

- 开发环境与测试见上方命令；测试完全离线（邮箱、聊天机器人、SMTP、模型都是模拟的），改了行为请补测试。
- **永远不要给 Agent 发信的途径**：发信只能来自「确认发送」卡片操作，并经过 `db.claim_draft_for_send`。
- **能读邮件的 Agent 不能联网**：联网只属于单独的研究 Agent，它不能拿到邮箱工具。
- 代码、测试、截图里**不能有个人信息**，地址用 example.org，名字用虚构的。
- 依赖尽量少；风格跟随周围代码，注释写「为什么」。
