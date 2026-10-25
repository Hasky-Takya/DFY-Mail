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
| 动态事件与长期故事记忆 | 支持，LLM 联合提取并更新 `memory/event.json`、`memory/memory.json` |
| 人设与提示词 | 支持，在 `config/prompt.json` 中配置 |
| 运行状态日志 | 所有入口支持，分别写入 `log/output_log/*.log` |
| 随机空闲主动来信 | 支持，`dfy_clock.py` 统一收信回复与随机等待后主动发信 |
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
│  ├─ prompt.json               # 共享人设、回复、首次写信与摘要任务
│  └─ positive_prompt.json      # 随机主动邮件的话题任务及主题
├─ src/
│  ├─ dfy_test.py               # DeepSeekChat 类与单轮问答入口
│  ├─ mail.py                   # 邮箱收发、连接检查与过滤诊断
│  ├─ dfy_mail.py               # 生成并发送一封主动邮件
│  ├─ dfy_receive.py            # 新邮件监听与自动回复
│  ├─ dfy_clock.py              # 统一收信回复及随机主动来信
│  ├─ dialog_memory.py          # 梗概、历史上下文与发送记录
│  ├─ story_memory.py           # 事件／长期记忆校验与可恢复投影
│  └─ mail_logging.py           # 日志轮转及脱敏
├─ memory/
│  ├─ dialog.json               # 对话梗概及已验证的故事更新包
│  ├─ event.json                # 计划／进行／结束事件与人物、时态
│  ├─ memory.json               # 长期设定、偏好、关系与约定
│  └─ README.md                 # 记忆字段与人工核验说明
├─ log/output_log/              # 各入口自动创建状态日志
│  ├─ dfy_test.log
│  ├─ dfy_mail.log
│  ├─ dfy_receive.log
│  ├─ dfy_clock.log
│  └─ mail.log
└─ tests/
   ├─ test_receive.py
   ├─ test_memory.py
   ├─ test_prompt.py
   ├─ test_logging.py
   ├─ test_activity.py
   ├─ test_clock.py
   ├─ test_conversation_state.py
   ├─ test_memory_observation.py
   ├─ test_story_memory.py
   ├─ test_story_integration.py
   └─ test_extraction_repair.py
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

### 统一运行：随机空闲后主动来信

```powershell
python src/dfy_clock.py
```

这个入口**已经包含正常收信和回复**，不要再同时运行 `dfy_receive.py`、`dfy_mail.py` 或其他 AI 发信入口。

在 `config/settings.json` 调整（示例和个人配置文件已同步添加）：

```json
"idle_min_minutes": 60,
"idle_max_minutes": 240
```

单位是分钟，必须为正整数且下限不超过上限，抽样包含两个端点。检查频率沿用 `check_interval_minutes`，因此到期发送存在至多一个检查周期及模型／邮件处理耗时的延迟。

例如抽到 150：

1. 启动时查询 INBOX 中最近有效用户来信的服务器接收时间，并参考记忆中已确认的用户接收时间、最近成功的主动发信时间，取最近者作为基线；没有历史才从启动时计时。启动时已经超过阈值，可在首次成功检查新信后主动发送。
2. 距基线达到 150 分钟且没有待回复消息时，用共同人设、已确认历史梗概及 `positive_prompt.json` 生成主动邮件。
3. 主动生成完成后再查新信；用户刚好来信则取消草稿，先回复用户。
4. 主动发送被 SMTP 接受且记忆确认后，重新抽取（如 210），从发送成功时重新计时。用户始终不回也会继续按新随机间隔主动来信，本版没有每日上限或免打扰。
5. 等待期间收到有效用户新邮件，重新抽取并从服务器接收时间计时；普通回复的完成时间不会额外重置。

`positive_prompt.json` 的 `prompt` 是持续主动找话题的任务，`subject` 是主动邮件主题。这里只会依据历史和人设生成话题，**没有联网搜索功能**，不会调用 `prompt.json` 中仅用于第一封问候的 `send_task`。

计时使用单调时钟；重启会重新抽样，但仍参考最近用户来信与成功主动发送时间，不会无条件立刻再发。休眠恢复不补发多封。最近历史邮件按 UID 倒序选择，只读必要头部和 INTERNALDATE；无可靠接收时间时保守从启动观察时间开始。历史查询不检查正文是否为空；实时来信沿用非空正文才接纳的规则，自动回复、退信、重复信和空正文不重置。

邮箱检查失败或有待回复消息时不主动发送；生成和摘要暂时失败分阶段最多尝试三次，摘要重试可复用正文。记忆有未核验状态、UIDVALIDITY 改变、SMTP 结果不确定时停止，禁止盲目重发。最后检查与 SMTP 之间仍有很短的到信窗口，普通轮询不能实现绝对原子判断。启动前旧邮件只用于判断空闲，不自动补回复。

状态日志在 `log/output_log/dfy_clock.log`，包含抽中的分钟数与重置原因，不保存正文、话题、提示词或密钥。

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
| `prompt` | 唯一的共享人设，按标签分组、逐行数组排列 |
| `reply_task` | 正常回复的任务要求，与共同人设组合 |
| `send_task` | 手动 `dfy_mail.py` 主动任务，无历史才首次问候，有历史自然延续 |
| `story_extraction_prompt` | 联合提取事件／长期记忆的可编辑四字段协议；存在时启用 |
| `subject` | 手动主动邮件主题；正常回复仍使用原主题的 `Re:` |
| `summary_prompt` | 提炼双方梗概，要求返回固定字段的 JSON |

`positive_prompt.json` 单独保存周期主动任务及主题；`dfy_clock.py` 复用共同人设，不再复制一整份角色设定。**修改 TXT 不会自动同步到 JSON**；配置和提示词修改后需重启相应脚本。

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

所有 AI 邮件入口都会携带**全部已确认 `sent` 梗概**，作为 user 级参考，不提升为系统规则。记忆过大时会明确停止，不静默截断；历史、当前任务和状态共同采用 60000 字符的保守预算，不是精确 token 计算。

此外每次提供程序计算的 `conversation_state`：现在时间、谁主动触发、最近用户／助手往来、发信后已知用户联系、精确回信证据及时间间隔。当前来信未落盘也参与计算。新记录可保存可选的严格回复目标，旧 `dialog.json` 无需迁移或清空；回复旧邮件与后来主动联系不等同于没有回复。

“暂未观察到后续来信”和“无法确定”会明确区分。停机缺口、未同步、同秒或未来时间不能支持绝对的未回复结论；SMTP 接受也不是已送达或已读。程序不会推测用户沉默的原因。`positive_prompt.json` 可以调整如何自然承接上文和偶尔轻轻撒娇，不会固定报时、责备或要求回复；状态与时长只临时计算，不写回历史、不增加模型调用。

### 动态事件与长期故事记忆

`event.json` 保存事件的内容、人物、来源（user/llm）、重要程度1–5、planned/ongoing/completed状态、past/present/future/unknown时态、原始时间表达和可空的规范化时间。`memory.json` 保存重要设定、经历、偏好、关系和约定，包括由来、人物、重要程度及active/superseded/retracted状态。

LLM 根据本轮用户和助手原文以及已有条目判断新增或更新，更新原ID并保留版本和来源轨迹，不每轮重复添加。共同角色故事可持续发展，但shared_story不等于用户现实经历；助手说过的话只证明助手表达过，不自动成为用户事实。时间流逝不自动结束事件。

通常仍是**两次模型请求**：正文生成一次，梗概与事件／长期记忆联合提取一次。旧两字段摘要API仍兼容；配置中的 `story_extraction_prompt` 启用四字段模式。当前邮件、历史、对话状态及两个记忆库完整提供，超预算停止，不静默删历史。模型的语义判断仍可能出错。

联合提取请求还包含程序生成的 `json_templates`：根输出模板、事件条目模板、长期记忆条目模板。模型仅填写需要的内容，不回传模板说明；没有新信息时保留空数组，仍由本地严格校验。

校验失败日志额外记录受控 `code` 与 `field`，不记录原文。例如 `code=evidence_not_in_source field=event_updates[0].evidence` 表示证据没有逐字出自所选来源邮件。后续重试会把安全错误、脱敏的上次输出和原任务交给模型，要求针对字段修正并重新返回完整JSON；仍复用正文、最多三次，不在提取函数内再套重试。反馈仅在内存和API请求中使用，不写日志或记忆。坏本地故事上下文属于需核验的运行错误，不再误当模型输出错误重试。更新已有长期记忆时必须保留初始 `origin`。

只提取新版本之后成功确认的邮件往来，不批量回填旧梗概；没有值得记住的内容可返回空更新。未发送或被取消的草稿不进入正式事件。用户信息也随该轮成功对话提交；生成或发送失败不会另建入站事件。

`dialog.json` 保存已校验的 `story_updates` 作为权威提交，两个新JSON是可恢复投影。只有sent记录应用；每个文件原子更新已应用对话ID和条目。发送成功后若一份投影写入失败，程序停止，下次启动只补齐本地投影，**不重新发信**。非空损坏或内容与权威记录不符时拒绝覆盖，请先停机备份、排查，不手动清空或随意修改这些文件。详见 [memory/README.md](memory/README.md)。

### 发送结果不确定时

程序在发信前保存摘要，SMTP 接受后才确认 `sent`。发生异常时，可能留下 `prepared`、`sending` 或 `unknown`，这类记录会阻止后续自动发送，需要先核实，不能简单重跑。

**不要清空 `dialog.json` 试图修复故障，也不要在脚本运行时手工编辑。** 空白文件会被视为空记忆，会丢掉历史和待核验信息。详细核验和恢复步骤见 [memory/README.md](memory/README.md)。

## 7. 日志与故障排查

所有命令行入口的状态日志统一保存在项目的 `log/output_log`，不受启动时所在目录影响：

| 启动脚本 | 日志文件 |
| --- | --- |
| `src/dfy_test.py` | `log/output_log/dfy_test.log` |
| `src/dfy_mail.py` | `log/output_log/dfy_mail.log` |
| `src/dfy_receive.py` | `log/output_log/dfy_receive.log` |
| `src/dfy_clock.py` | `log/output_log/dfy_clock.log` |
| `src/mail.py` | `log/output_log/mail.log` |

子模块的日志随当前入口写入对应文件。每行格式示例：

```text
2026-10-25 14:00:00,123 | INFO | pid=1234 | entry=dfy_receive.py | dfy_test.py:55 | dfy.chat | DeepSeek 请求完成，duration=1.20s。
```

日志为 UTF-8，按天轮转，最多保留 14 个历史备份。时间使用运行机器的本地时间；记忆中的时间是带时区的 ISO 8601，默认转为 UTC，二者显示可能不同。

- 终端显示主要状态，文件还包含 DEBUG 级 IMAP 阶段与游标来源；异常栈的每一行也带时间和来源。
- 记录进程 ID、入口、实际源文件及行号、UID、处理阶段、模型耗时、重试和异常类型。
- **只记录运行状态**，不记录问题、模型回答、邮件主题／正文、梗概、完整提示词、API Key 或授权码。异常仅保留调用位置和类型，已知密钥及邮箱地址额外脱敏。
- 交互问答、`mail.py read`／`diagnose` 的完整结果及命令帮助仍只显示在终端，不会被整体转存。凭据输入仍隐藏。
- 启动时无法创建日志会停止，不继续联网或发信。导入模块不会创建日志；依赖缺失、语法错误等初始化前的解释器错误不在覆盖范围内。
- 不支持同一入口同时启动多个进程进行安全轮转；旧 `log/mail_log` 不自动迁移或删除。

常见现象：

| 现象 | 检查方法 |
| --- | --- |
| 官网收到了，脚本没回复 | 确认是在监听就绪后发出、邮件在 INBOX、实际 From 与 `user.email` 一致；查看日志是否被过滤 |
| 日志显示“跳过 UID” | 使用 `diagnose --uid` 查看具体原因；不要直接取消全部过滤 |
| `From` 解析异常 | 代码已兼容部分非规范显示名，但仅接受安全、唯一且匹配的发件地址 |
| `abort`／网络中断 | 看日志中的阶段、异常类型和调用位置；不记录服务器原始异常消息，临时断线保留游标并在后续周期重试 |
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
