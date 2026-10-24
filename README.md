# DeepSeek 邮件对话助手

一个供单人使用的 Python 邮件助手：使用独立邮箱收发邮件，由 DeepSeek 根据来信内容生成回复，并把双方交流提炼成梗概，供后续对话参考。

当前默认对接 **126 助手邮箱**，使用 IMAP 收信、SMTP 发信。用户使用自己的日常邮箱与助手交流，不需要向程序提供日常邮箱的登录权限。

> 本项目是个人脚本，不是全天在线的托管服务。电脑休眠、关机或脚本停止时，不会继续处理邮件。

## 功能与边界

| 功能 | 当前情况 |
| --- | --- |
| DeepSeek 命令行单轮问答 | 支持 |
| 主动生成并发送一封邮件 | 支持，手动运行 `dfy_mail.py` |
| 轮询新邮件并自动回复 | 支持，默认每 1 分钟检查一次 |
| 仅响应一个指定用户 | 支持，按配置的实际 From 地址过滤 |
| 中文及 HTML 正文 | 支持，HTML 提取文字，不加载远程资源 |
| 邮件线程关联 | 支持 `In-Reply-To`、`References` |
| 对话梗概记忆 | 支持，双方摘要写入 `memory/dialog.json` |
| 人设与提示词 | 支持，在 `config/prompt.json` 中配置 |
| 监听日志 | 支持，写入 `log/mail_log/mail.log` |
| 定时／空闲后主动来信 | **尚未实现**，目前主动发送需要手动运行脚本 |
| 附件、图片理解 | **不支持** |
| 多用户、网页管理、向量检索 | **不支持** |
| 停机补信、完整正文历史、持久化待处理队列 | **不支持** |

## 目录结构

```text
.
├─ README.md
├─ 桃桃.txt                     # 人设源文档，不会在每次运行时自动加载
├─ config/
│  ├─ settings.json             # 助手邮箱、唯一用户邮箱、检查间隔
│  └─ prompt.json               # 人设、回复、主动写信与摘要提示词
├─ src/
│  ├─ dfy_test.py               # DeepSeekChat 类与单轮问答入口
│  ├─ mail.py                   # 邮箱收发、连接检查与过滤诊断
│  ├─ dfy_mail.py               # 生成并发送一封主动邮件
│  ├─ dfy_receive.py            # 新邮件监听与自动回复
│  ├─ dialog_memory.py          # 梗概、历史上下文与发送记录
│  └─ mail_logging.py           # 日志轮转及脱敏
├─ memory/
│  ├─ dialog.json               # 实际对话梗概，请保护及备份
│  └─ README.md                 # 记忆字段与人工核验说明
├─ log/mail_log/                # 运行监听后自动创建
│  └─ mail.log
└─ tests/
   ├─ test_receive.py
   └─ test_memory.py
```

## 1. 准备环境

建议使用 **Python 3.11 或更高版本**。以下以项目根目录下的 Windows PowerShell 为例。

已有虚拟环境时直接激活：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install "openai>=1.0.0,<3.0.0"
```

尚未创建虚拟环境时，先执行：

```powershell
python -m venv .venv
```

当前目录未包含 `requirements.txt`，可按上面的命令直接安装所需 SDK；此命令是安装说明，编写文档时未实际执行。项目第三方依赖为 `openai`，用于访问 DeepSeek 的 OpenAI 兼容接口。邮箱、JSON、文件锁和日志使用 Python 标准库；不需要单独安装名为 `mail` 的包。

当前客户端默认使用 `https://api.deepseek.com` 和 `deepseek-chat`。需要其他模型时，在调用 `DeepSeekChat` 时传入 `model`；现有脚本尚未提供模型配置项或命令行选项。

## 2. 配置邮箱

准备两个**不同的邮箱账户**：

- **助手邮箱**：程序登录此邮箱，用于收发邮件。
- **用户邮箱**：你日常使用的邮箱，程序只向它发信、只处理它的来信。

在助手的 126 邮箱网页设置中开启 IMAP／SMTP 服务，取得**客户端授权码**。授权码通常不是网页登录密码，不要将其写进公开文件或发给他人。

编辑 `config/settings.json`，以下地址仅为示例：

```json
{
  "check_interval_minutes": 1,
  "assistant": {
    "email": "assistant@example.com",
    "auth_code_env": "MAIL_AUTH_CODE",
    "imap_host": "imap.126.com",
    "imap_port": 993,
    "smtp_host": "smtp.126.com",
    "smtp_port": 465
  },
  "user": {
    "email": "user@example.net"
  }
}
```

- 把 `assistant.email` 替换为真实的 126 助手邮箱地址，把 `user.email` 替换为实际发信的用户邮箱地址。
- `check_interval_minutes` 单位为**分钟**，默认 `1`，必须为有限正数。
- `auth_code_env` 指定保存授权码的环境变量名称，默认 `MAIL_AUTH_CODE`。
- 当前邮件连接使用 SSL，并验证证书；不要只修改端口就假设支持明文或 STARTTLS。
- 修改邮箱配置、频率或提示词后，需要重启相关脚本。

### 密钥与授权码

可以不配置环境变量：脚本会通过隐藏输入提示你提供 DeepSeek API Key 和／或邮箱授权码，不会把输入保存到配置文件。

也支持在当前 PowerShell 会话中设置：

```powershell
$env:DEEPSEEK_API_KEY = "替换为你的 DeepSeek API Key"
$env:MAIL_AUTH_CODE = "替换为助手邮箱客户端授权码"
```

> 上面的命令可能被终端历史记录保存。更在意本地隐私时，使用脚本的隐藏输入方式。当前代码**不会自动加载 `.env` 文件**。

## 3. 推荐启动顺序

### 检查邮箱连接

```powershell
python src/mail.py
```

默认仅检查 IMAP／SMTP 登录，不读取正文、不发送测试邮件。显式的 `check` 命令等效：

```powershell
python src/mail.py check
```

### 启动自动回复监听

```powershell
python src/dfy_receive.py
```

如果未配置环境变量，按提示输入 API Key 与助手邮箱授权码。看到 **“监听已就绪”** 后，再从用户邮箱向助手邮箱发送一封新邮件。

处理流程：

```text
建立启动基线 → 周期检查 INBOX 新信 → 校验用户与自动邮件标记
    → 读取历史梗概和本次正文 → DeepSeek 生成回复
    → DeepSeek 提炼双方梗概 → 保存发送前记录
    → SMTP 发送 → 确认发送时间和记忆状态
```

正常一轮通常有 **两次模型请求**，所以回复耗时不只取决于一分钟的检查间隔，还包括模型生成、摘要和邮件传输时间。脚本串行处理多封新信，不并发补跑错过的检查点。

按 `Ctrl+C` 停止监听。**不要同时启动多个监听实例，也不要并行运行两个 AI 发信入口。**

### 启动基线的重要含义

- 基线是在输入凭据后、首次成功读取邮箱状态时建立的，不是敲下运行命令的瞬间。
- 只处理基线之后到达的 INBOX 邮件；启动前、停机期间以及建立基线前到达的邮件不会自动补回。
- 使用 UID 游标而非“未读”标记；读取邮件不会主动将其标为已读。
- 去重和待处理队列主要保存在当前进程内，重启不是恢复失败邮件的方法。
- UIDNEXT 缺失时会尝试 STATUS，再回退查询现有 UID 编号；仍保留 UIDVALIDITY 校验，代际改变会停止。

## 4. 其他脚本用法

### 手动让 DeepSeek 主动来信

```powershell
python src/dfy_mail.py
```

每运行一次，读取主动写信提示词及历史记忆，生成、提炼并尝试发送**一封**邮件给配置用户。它不是定时任务；不需要主动来信时不要运行。

### 仅测试 DeepSeek 问答

```powershell
python src/dfy_test.py
```

输入 `exit` 或 `quit` 退出。这个命令行入口每次问题独立，**不读写邮件对话记忆**。

### 手动读取用户最近邮件

```powershell
python src/mail.py read --limit 5
```

包含已读邮件，终端会显示正文；重复调用可能返回同一封邮件，不触发模型或自动回复。`limit` 范围为 `1～50`，监听增量读取不受此最近邮件数量限制。

### 手动发送固定正文

```powershell
python src/mail.py send --subject "测试邮件" --body "你好，这是测试内容。"
```

收件人固定为配置用户。此入口不调用 DeepSeek，也**不会生成梗概记忆**。

### 诊断某封邮件为什么被过滤

从日志取得 UID 后执行，例如：

```powershell
python src/mail.py diagnose --uid 123456
```

该命令只查询必要邮件头，不读正文、不调用模型、不发信。输出 `reply_allowed` 和具体原因；它不会重新排队或回复该邮件。UID 只在对应邮箱文件夹和 UIDVALIDITY 下有意义。

邮箱工具可以指定其他配置文件，选项放在子命令之前：

```powershell
python src/mail.py --config config/settings.json read --limit 5
```

## 5. 提示词与桃井人设

`config/prompt.json` 的键与用途：

| 键 | 用途 |
| --- | --- |
| `reply_prompt` | 自动回复时的 system 指令：人设、语气、根据来信回答及输出格式 |
| `prompt` | 主动写信任务，作为 user 级输入，与历史参考一起交给模型 |
| `subject` | 主动邮件的主题；回复邮件仍使用原主题的 `Re:` |
| `summary_prompt` | 提炼双方梗概，要求返回固定字段的 JSON |

目前 `桃桃.txt` 中的完整设定已经复制到 `reply_prompt` 和 `prompt`。**修改 TXT 不会自动同步到 JSON**；想改变实际表现，需要更新对应提示词并重启监听。

摘要提示词不使用角色扮演，避免把人设中的虚构背景误记成双方真实交流。角色语言不能替代可靠的回答；梗概和模型回答都可能有误，应按实际效果检查。

## 6. 对话记忆

实际记忆位于 `memory/dialog.json`。一轮用户来信和助手回复保存为一条记录；主动发信单独保存，用户相关内容为空。

主要字段：

- `type`：`deepseek自己主动发送`、`用户主动发送`、`用户回复上一封邮件`。
- `received_at`、`received_at_source`：邮箱服务器接收时间，或明确标记的程序观察时间。
- `sent_at`：SMTP 接受成功的时间，不表示最终投递或阅读。
- `user_summary`、`assistant_summary`：双方各一段内容梗概，不保存完整正文。
- `user_message_id`、`assistant_message_id`、`uid`、`uidvalidity`：邮件关联信息。
- `status`：`prepared`、`sending`、`unknown`、`sent`。

“用户回复上一封邮件”按邮件引用头判断：优先使用明确的 In-Reply-To，缺失时才看 References 最后一个 ID，且必须对应来信接收时最近一封已确认助手邮件。回复更早的信或只在主题写 `Re:`，仍归为用户主动发送。

两个 AI 发信入口都会携带**全部已确认 `sent` 梗概**，作为 user 级参考，不提升为系统规则。记忆过大时会明确停止，不静默截断；当前组合请求采用 60000 字符的保守预算，不是精确 token 计算。

### 发送结果不确定时

程序在发信前保存摘要，SMTP 接受后才确认 `sent`。发生异常时，可能留下 `prepared`、`sending` 或 `unknown`，这类记录会阻止后续自动发送，需要先核实，不能简单重跑。

**不要清空 `dialog.json` 试图修复故障，也不要在脚本运行时手工编辑。** 空白文件会被视为空记忆，会丢掉历史和待核验信息。详细核验和恢复步骤见 [memory/README.md](memory/README.md)。

## 7. 日志与故障排查

监听日志路径：

```text
log/mail_log/mail.log
```

日志为 UTF-8，按天轮转，最多保留 14 个历史备份。时间使用运行机器的本地时间；记忆中的时间是带时区的 ISO 8601，默认转为 UTC，二者显示可能不同。

- 终端显示主要状态，文件还包含 DEBUG 级 IMAP 阶段与游标来源。
- 记录进程 ID、时间、UID、处理阶段、模型耗时、重试和异常位置。
- 不主动记录正文、回复、梗概、完整提示词、API Key 或授权码；错误信息会做脱敏。
- 当前持久化日志主要由监听入口初始化，其他独立脚本不保证写入同一日志。

常见现象：

| 现象 | 检查方法 |
| --- | --- |
| 官网收到了，脚本没回复 | 确认是在监听就绪后发出、邮件在 INBOX、实际 From 与 `user.email` 一致；查看日志是否被过滤 |
| 日志显示“跳过 UID” | 使用 `diagnose --uid` 查看具体原因；不要直接取消全部过滤 |
| `From` 解析异常 | 代码已兼容部分非规范显示名，但仅接受安全、唯一且匹配的发件地址 |
| `abort`／网络中断 | 看日志中的阶段和脱敏详情；临时断线保留游标并在后续周期重试 |
| UIDVALIDITY 改变 | 程序安全停止，不重放旧信；核实邮箱状态后再启动 |
| DeepSeek 失败或梗概不合格 | 查看错误类型、状态码及重试次数；检查余额、密钥、网络和摘要输出要求 |
| 有 `prepared/sending/unknown` | 先人工核验是否已经发出，按记忆说明处理，禁止盲目重发 |
| 显示“SMTP 已接受”但没收到 | 查用户垃圾箱、拦截及退信；SMTP 接受不保证最终投递 |
| 记忆格式错误 | 保留并备份原文件后修复 JSON，不要让程序覆盖或清空历史 |

日志只能辅助定位，不保证自动判定所有服务端故障。不要直接开启未过滤的 IMAP 协议调试后公开分享，原始输出可能带认证数据或邮件内容。

## 8. 离线测试

在项目根目录运行：

```powershell
python -B -m unittest discover -s tests -v
```

测试使用模拟 DeepSeek、IMAP、SMTP 和临时记忆文件，**不会连接真实邮箱、调用真实 API、发送邮件或修改实际对话记忆**。离线测试通过不代表当前账号、网络或服务端已完成真实联调。

覆盖收信基线、增量/去重、地址过滤、正文解析、回复内容传递、线程头、摘要校验、历史参考、对话分类、原子保存、发送失败防重发和日志隐私等。

## 安全与隐私

- 邮件 From 地址可以伪造，白名单不是强身份认证；项目只做邮件聊天，不应直接据此执行付款、账号或本机命令。
- 当前正文和历史梗概会通过 API 发送给 DeepSeek；不要用于不允许外传的机密内容。
- 记忆文件和备份默认不加密，日志也含运行元数据；请控制本地访问权限。
- 不要把真实邮箱配置、对话记忆、日志、密钥或授权码发布到公共仓库。
- 修改配置和提示词前建议备份；迁移或操作记忆时先停止所有相关脚本。
