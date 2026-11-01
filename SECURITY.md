# Security policy

MailOwl has access to your mailboxes, so security reports are taken seriously.

**Please do not open a public issue for vulnerabilities.** Report them privately through GitHub: *Security → Report a vulnerability* on this repository. You should get a response within a week.

Particularly relevant:

- any way for email content (prompt injection) to make an agent send mail, reach the network, or leak data;
- any way to send a draft without the owner's explicit confirmation, or to send it twice;
- any way for someone other than the paired account to operate the bot;
- credentials or mail content ending up in logs, the chat app or third-party services unexpectedly.

---

**安全问题请不要公开提 Issue**，请在本仓库的 *Security → Report a vulnerability* 私下报告，一周内会回复。尤其欢迎：邮件内容（提示注入）诱导 Agent 发信、联网或泄露数据；绕过确认发信或重复发信；非绑定账号操作机器人；凭证或邮件内容意外出现在日志、聊天软件或第三方服务里。
