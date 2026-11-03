# MailOwl 接入步骤

[English](SETUP.en.md)

MailOwl 跑在一台常开的 Mac 上（下文叫「运行机」，比如家里的 Mac mini）。配置文件在 `~/.config/mailowl/`，数据在 `~/.local/share/mailowl/`。

## 0. 安装

需要 Python 3.11+；默认用运行机上已登录的 [Claude Code](https://claude.com/claude-code) 调模型（也可以换成其他模型，见第 5 节）。

```bash
uv tool install git+https://github.com/YaoxinBio/mailowl   # 或者 pipx install …；想改代码就 git clone 后 pip install -e .
mailowl init                                               # 生成 ~/.config/mailowl/ 下的配置模板
```

下面四步做完就能用，顺序别乱：**飞书长连接要求保存配置时服务已经在线**，所以要先填凭证、启动服务，再回控制台保存长连接设置。

## 1. 邮箱：拿到授权码

收信用 IMAP（推荐，能读「已发送」来判断你回复过没有）或 POP3；发信用 SMTP。密码一律填**授权码 / 应用专用密码**，不是网页登录密码。

| 邮箱 | 收信 | 发信 | 已发送文件夹 | 备注 |
|---|---|---|---|---|
| Gmail | imap.gmail.com:993 | smtp.gmail.com:465 | `[Gmail]/Sent Mail`（中文界面是 `[Gmail]/已发邮件`） | 先开两步验证，再到 Google 账号「应用专用密码」生成；Gmail 会自动保存已发送，`save_to_sent=false` |
| QQ 邮箱 | imap.qq.com:993 | smtp.qq.com:465 | `Sent Messages` | 设置 → 账号 → 开启 IMAP/SMTP → 生成授权码 |
| 腾讯企业邮 | imap.exmail.qq.com:993 | smtp.exmail.qq.com:465 | `Sent Messages` | 需管理员允许客户端登录 |
| 163 / 126 | imap.163.com:993 | smtp.163.com:465 | `已发送` | 设置里开启 IMAP 并生成授权码；**必须 `imap_id = true`** |
| 其他邮箱（单位、学校、自建等） | 见邮箱网页版的「客户端设置 / IMAP」说明 | 同左 | 用 `mailowl folders` 查 | 有的要先在网页版开启客户端登录；不少邮箱 SMTP 发出的信不会自动存进已发送，这时设 `save_to_sent = true` |
| Outlook / Hotmail / 微软 365 | — | — | — | 微软只允许 OAuth 登录 IMAP，暂不支持；见下方「绕过办法」 |

**邮箱不支持授权码怎么办**（比如用微软 365 的学校、公司邮箱）：在那个邮箱的网页版里设置「自动转发副本」到一个支持的邮箱（Gmail、QQ、163 等），然后用 MailOwl 接入转发到的那个邮箱。代价是：回复会从转发到的邮箱发出，MailOwl 也看不到你在原邮箱里发过什么。

不确定文件夹名：先只配 `folders = ["INBOX"]`、`sent_folder = ""`，运行 `mailowl folders` 列出所有文件夹（已解码中文，标出「已发送」），再填回去。

## 2. 飞书：创建自建应用机器人

1. 打开 <https://open.feishu.cn/app> →「创建企业自建应用」，名字例如「邮件助手」。
2. 「添加应用能力」→ 添加 **机器人**。
3. 「权限管理」开通：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message:send_as_bot`（以应用身份发消息）
   - `im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）
   - `im:resource`（获取消息中的文件，用于你发附件给机器人）
   - `im:message.reactions:write_only`（可选：处理中显示"敲键盘"表情）
4. 「凭证与基础信息」复制 **App ID** 和 **App Secret**，填进运行机的 `~/.config/mailowl/secrets.toml` 的 `[feishu]`。
5. 在运行机上启动服务（见第 4 步）。
6. 回到控制台「事件与回调」：
   - 事件配置 → 订阅方式选 **使用长连接接收事件** → 保存 → 添加事件 **接收消息 `im.message.receive_v1`**；
   - 回调配置 → 订阅方式选 **使用长连接接收回调** → 保存 → 添加回调 **卡片回传交互 `card.action.trigger`**。
7. 「版本管理与发布」→ 创建版本，可用范围选**仅自己** → 发布（自己的组织里自己就是管理员，直接通过）。
8. 在飞书里搜索机器人名字，打开单聊，把 `mailowl pair` 显示的 6 位配对码发给它，看到「绑定成功」即可。

之后改了权限要重新发布一个版本才生效。

## 3. Bark：iPhone 紧急提醒

1. App Store 安装 Bark，打开后首页的示例 URL 形如 `https://api.day.app/XXXXXXXX/...`，`XXXXXXXX` 就是 device key。
2. 填进 `secrets.toml` 的 `[bark] device_keys = ["XXXXXXXX"]`。
3. iPhone 设置 → 通知 → Bark → 打开「时效性通知」，否则紧急邮件在专注模式下不会响。
4. `mailowl bark-test` 发一条测试，点开应跳到飞书的机器人对话。

## 4. 启动与日常运维（都在运行机上）

```bash
nano ~/.config/mailowl/config.toml      # 邮箱、profile、规则、落款模板
nano ~/.config/mailowl/secrets.toml     # 授权码、飞书凭证、Bark key
mailowl check                           # 逐项检查登录
mailowl fetch                           # 首次收信：回填最近 30 天作为历史（每个文件夹最多 500 封），不推送
mailowl pair                            # 显示配对码
mailowl service install                 # 装成后台服务（登录后自动启动，崩溃自动拉起）
mailowl service status                  # 运行状态；restart / logs / uninstall 同理
```

在另一台电脑上开发、运行机上跑的话，可以用 `deploy/deploy.sh --restart [主机名]`：rsync 同步 → 装依赖 → 跑测试 → 重启服务。

## 5. 换用其他模型（可选）

MailOwl 的模型调用分三个用途：**分级**（每封新邮件）、**对话起草**（飞书里聊天、写回信）、**联网研究**（你同意后才上网查）。先在 `[llm.backends.<名字>]` 里定义要用的后端，再给每个用途指一个名字：

```toml
# config.toml
[llm]
triage_backend = "deepseek"
agent_backend = "deepseek"
research_backend = "codex"         # "none" 关闭联网研究

[llm.backends.deepseek]
type = "openai"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
extra_body = { thinking = { type = "disabled" } }   # DeepSeek 必须关思考：开着思考调用工具会报 400

[llm.backends.claude]
type = "claude_cli"
model = "sonnet"

[llm.backends.codex]
type = "codex_cli"
model = "gpt-6.1-sol"      # 不写就用 Codex 的默认模型
effort = "low"             # 推理强度
```

```toml
# secrets.toml
[llm.backends.deepseek]
api_key = "sk-..."
```

换模型只改 `[llm]` 里对应的一行，比如 Claude 额度用完时把三个都指向 `deepseek`，恢复后再改回 `claude`；暂时不用的后端可以留着。改完运行 `mailowl check`：每个用到的后端都会检查一遍，OpenAI 兼容接口会真调一次。

| type | 分级 | 对话起草 | 联网研究 | 说明 |
|---|---|---|---|---|
| `claude_cli` | ✓ | ✓ | ✓ | 本机已登录的 Claude Code；model 写 haiku / sonnet / opus |
| `openai` | ✓ | ✓ | ✓（配了 Tavily 才能搜索，否则只能打开网址） | DeepSeek、通义千问、Kimi、智谱、OpenAI、OpenRouter、本地 Ollama 等；做对话要选支持 function calling 的模型 |
| `codex_cli` | ✓ | — | ✓（Codex 自带的搜索） | 本机已登录的 Codex CLI（`codex login`，ChatGPT 订阅）；没有邮件工具，不能做对话 |

`extra_body` 会原样并进 openai 类型后端的每次请求，用来传各家特有的参数。Tavily 的密钥写在 secrets.toml 的 `[llm]` `tavily_api_key`。

只想让分级在本地完成（每封新邮件都不交给模型厂商），对话起草仍用 Claude：

```toml
# config.toml
[llm]
triage_backend = "local"
agent_backend = "claude"
research_backend = "claude"

[llm.backends.local]
type = "openai"
base_url = "http://127.0.0.1:11434/v1"   # 本地 Ollama
model = "qwen3:8b"

[llm.backends.claude]
type = "claude_cli"
```

MailOwl 给 Codex 单独建一个 `CODEX_HOME`（只软链接 `auth.json`），不读你的 `~/.codex/config.toml` 和 `AGENTS.md`；macOS 上外面再套一层沙箱，Codex 执行不了任何命令，也读不到 MailOwl 的配置和数据目录。每次调用约 20–40 秒（主要是 CLI 启动开销），经代理约 5–6 MB 流量。

0.1.0 的旧写法（`[llm] backend`、`triage_model` / `agent_model`、`research_backend = "auto"`、`[llm.triage]` 表）照旧能加载，会自动换算成命名后端；两种写法不能混用。

其他隐私开关见 README 的「隐私模式」：`body_in_feishu = "never"`、`[bark] content = "minimal"`、`research_backend = "none"`。

## 隐私与安全边界

- 原始邮件只存在运行机的 SQLite（`~/.local/share/mailowl/mailowl.db`），附件不下载，需要时才从服务器取回；草稿要带的附件临时存在本机，发出或取消后即删除。
- 首次回填历史时，超过 300 KB 的邮件只取头部和正文前段（日志里会写最多下载多少 MB），避免大量流量走代理。
- 发到飞书的默认只有发件人、主题和 AI 摘要；点「查看原文」才发正文（`body_in_feishu = "never"` 可彻底关闭）。
- 每封新邮件会经模型分级（默认走运行机上已登录的 Claude Code）。不想外发的邮件用 `[rules] local_only_senders / local_only_keywords` 拦下，只按规则分级。
- 能读邮件的对话 Agent 不能上网；需要网上信息时，它只能提一个问题，你在飞书卡片上点「同意」后，由一个读不到邮箱的研究 Agent 去查。这样即使有人在邮件里藏了诱导指令，也没有办法让 Agent 把邮件内容送到外面的网站。
- 只有绑定的飞书账号能操作；Agent 没有发信工具，只能存草稿；发信只发生在你点「确认发送」（带二次确认）之后，同一草稿只能发出一次，发送结果不确定时不会自动重试。
