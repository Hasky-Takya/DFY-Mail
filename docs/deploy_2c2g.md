# 2 核 2 GB 服务器部署与图谱启用清单

> 本文是部署操作说明，不表示已经操作服务器。当前本地完整离线测试 **444 项通过**；真实 Linux、systemd、邮箱、模型和长期运行资源仍须按本文验收。
>
> 示例统一使用 `/srv/dfy`、专用系统用户 `dfy`、服务名 `dfy-mail.service`。如果你沿用原路径／原服务名，所有路径及权限必须一致修改，尤其模板的 `ProtectHome=true` 会阻止从 `/home/...` 读取项目。

## 0. 这次修复了什么

- 新候选拒绝自环和错误端点：`participates_in/organizes` 为人物到事件／项目，不再允许人物到人物。
- 旧冻结包保持原哈希；坏边被投影隔离，并显示 `rejected_assertions_count`，不因为一个旧坏边而篡改整份日志。
- 人物固定名与别名在配置中管理，同一 domain 内统一显示／查询；原 ID、版本和更新依据仍保留。
- 最终上下文裁剪统一处理图谱源条目和关系端点，使用实际可见目标校验；不把隐藏或旧版本视图当最新事实。
- 故事恢复改为流式前缀核验，不再在内存保留每个历史 revision 的完整状态；迁移检查也复用同一逻辑。
- UTF-8 文件日志支持日期＋体积轮转、总备份数限制、启动实际写入探针；日志故障输出安全固定告警到 journal。
- 非交互服务缺少凭据会明确失败退出，不再等 getpass 或误判为用户正常中断。

**注意：此前预览中第 032、035、094、101 条语义改写错误没有被自动修改。新程序能约束结构和关系类型，不能保证 LLM 的每句自然语言改写都正确。那份预览需修正并重新冻结，或从最新服务器副本重新整理；不要直接当成已修正结果导入。**

---

## 1. 停旧服务并备份最新服务器数据

先确认旧服务名称和项目实际位置：

```bash
sudo systemctl cat 你的旧服务名.service
sudo systemctl stop 你的旧服务名.service
sudo systemctl status 你的旧服务名.service --no-pager
```

确认没有其他 `dfy_clock.py`、`dfy_receive.py` 或手动入口同时运行。不要删除锁文件来绕过正在运行的进程。

停机后备份：

```bash
sudo install -d -m 700 /var/backups/dfy
sudo tar -czpf /var/backups/dfy/before-graph-$(date +%Y%m%d-%H%M%S).tar.gz \
  -C /服务器原项目目录 memory config
```

还需保留旧版本源码／发布包和现有 service 配置。备份包含邮箱配置等私有内容，不要公开上传。

### 必须保留

- `memory/dialog.json`、`event.json`、`memory.json`；
- `tasks.sqlite3`、`tasks.initialized`，以及停机后目录内存在的 SQLite 附属文件；
- 如果已经启用过图谱：`graph_annotations.jsonl`、迁移 manifest、派生库和所有 legacy 文件；
- 实际 `settings.json`、角色提示词及凭据环境配置。

**不清空 memory，不删除任务库，不重新建立收信基线。不要将本地 DFY-Mail/memory 覆盖服务器最新记忆。**

---

## 2. 上传并准备运行环境

上传完整的以下目录／文件，避免遗漏新模块：

```text
src/
tests/
config/settings_example.json
requirements.txt
deploy/
docs/
tools/
README.md
```

`config/prompt.json` 如果服务器已自定义，请合并新增图谱提取规则，保留现有角色。不要用示例配置覆盖真实 `settings.json`。

如果迁到推荐路径 `/srv/dfy`，在旧进程停止后将**服务器最新**记忆和实际配置完整复制过去，然后核对文件数量、大小和校验值。只复制文件并不代表已经切换新 event 格式。

专用用户示例（如果已经存在，不必重新创建）：

```bash
sudo useradd --system --home-dir /srv/dfy --shell /usr/sbin/nologin dfy
sudo install -d -o dfy -g dfy -m 700 /srv/dfy/memory /srv/dfy/log/output_log
sudo chown -R dfy:dfy /srv/dfy/memory /srv/dfy/log
```

核对代码和配置对 `dfy` 可读、目录可遍历；运行时只需 memory 与 log 可写，不需要给整个项目 `chmod 777`。

在服务器新建虚拟环境，**不要上传 Windows 的 .venv**：

```bash
cd /srv/dfy
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m unittest discover -s tests -q
```

使用 Python 3.11+；若系统缺少 venv／pip，按服务器发行版安装相应包。测试使用临时目录，不需要实际邮箱凭据。

---

## 3. 配置图谱和文件日志

把下面两节合并到实际 `/srv/dfy/config/settings.json`，保留原 assistant、user 和时间配置：

```json
{
  "graph": {
    "enabled": true,
    "max_updates": 16,
    "max_text_chars": 200,
    "context_items": 20,
    "context_chars": 12000,
    "query_limit": 30,
    "batch_size": 8,
    "sqlite_cache_kib": 8192,
    "assistant_name": "才羽桃井",
    "assistant_aliases": ["桃井", "助手", "momoi"],
    "user_name": "老师",
    "user_aliases": ["用户"]
  },
  "logging": {
    "file_level": "INFO",
    "console_level": "INFO",
    "backup_count": 14,
    "max_bytes": 10485760,
    "utc": false
  }
}
```

含义：

- `enabled=true` 才抽取新对话图谱；**不代表历史自动整理或正式格式自动切换**。
- SQLite cache 为 8 MiB，不运行本地 embedding／图数据库服务。
- 默认日志为每个入口独立文件，按日期或约 10 MiB 轮转，共保留最多 14 个备份；这不是整个日志目录的全局磁盘配额，单条超大日志也可能超过阈值。
- 默认只写 INFO 及以上，避免每次 IMAP 阶段 DEBUG 大量占盘。排障可临时改 DEBUG，结束后恢复。
- `directory` 可选；模板使用 `DFY_LOG_DIR` 固定到 `/srv/dfy/log/output_log`，环境变量优先。
- 改角色时同时改 assistant_name／aliases，不能让不同角色都映射到“才羽桃井”。

实际配置仍是 JSON，不支持注释、尾逗号或字符串形式的整数。

---

## 4. 配置服务凭据

创建 root 管理的环境文件：

```bash
sudo install -d -m 700 /etc/dfy
sudoedit /etc/dfy/dfy.env
sudo chmod 600 /etc/dfy/dfy.env
```

内容示例（填真实值，但不要发给我或写进 Git）：

```text
DEEPSEEK_API_KEY=你的API密钥
MAIL_AUTH_CODE=你的邮箱客户端授权码
```

如果实际 `assistant.auth_code_env` 不是 `MAIL_AUTH_CODE`，环境文件变量名也要相应改变。

- 不写 `export`；复杂值按 systemd EnvironmentFile 语法正确引用。
- 环境文件由 systemd 读取后传给进程，专用用户不必直接读取 root 的该文件。
- **手动 sudo -u dfy 执行 annotate 不会自动获得 EnvironmentFile 内容。** 迁移命令的终端须另外安全注入 API 环境；不要把密钥直接拼在命令参数中或留在 shell 历史。
- 不建议盲目 `source` 一个不可信环境文件。也不要执行裸 `env`／打印所有环境变量验证密钥。

---

## 5. 从服务器最新记忆构建图谱

服务保持停止，使用同一服务用户，避免 root 创建的数据库导致后续权限错误。

### 5.1 只读检查

```bash
cd /srv/dfy
sudo -u dfy .venv/bin/python -B src/graph_migrate.py dry-run \
  --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

如果存在 `prepared/sending/unknown`，先核实发送结果；不要直接改状态、删除记录或重发。

### 5.2 建立影子投影

```bash
sudo -u dfy .venv/bin/python -B src/graph_migrate.py build \
  --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

输出 `event_v2.json` 和派生 SQLite，正式旧 event 不会被影子构建覆盖。

### 5.3 整理历史内容

建议先在服务器最新记忆的私有副本上整理并审阅，再在停止服务的正式目录验证导入。已有本地预览不是服务器最新副本，而且有已知错误，不建议直接导入。

显式模型整理命令：

```bash
# 需要当前进程安全获得 DEEPSEEK_API_KEY；sudo 默认可能移除该变量。
# 在已验证 sudo 策略允许时，可只保留该指定变量，不使用宽泛 sudo -E。
sudo --preserve-env=DEEPSEEK_API_KEY -u dfy .venv/bin/python -B src/graph_migrate.py annotate \
  --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json \
  --llm --confirm-api --max-batches 1
```

此步骤外发历史文本及证据并产生费用，每批最多 `batch_size` 次请求。按报告分批整理，失败先检查，不使用无限循环强刷。同一批模型输出可能因格式或语义约束失败，未成功提交时应核查进度，不假定每次请求都能缓存成功。

完成后再 dry-run。检查：

- `pending_source_ids`：尚未单句整理的条目；
- `incomplete_dialog_ids`：明确提取未完成的对话；
- `rejected_assertions_count`：被隔离的非法旧关系；
- 单句是否保留人物、否定、条件、计划／完成状态。

**coverage必须如实审阅。旧坏边被隔离是保护，不等于所有旧数据已自动纠正；默认cutover会拒绝partial。不要使用allow-partial隐藏问题。** 如果要先运行新图谱，可以保持影子模式且 enabled=true，后续新发信仍会抽取／读取当前有效图谱；正式文件名切换可以等历史审阅完成再做。

### 5.4 正式切换（完成后再做）

```bash
sudo -u dfy .venv/bin/python -B src/graph_migrate.py cutover \
  --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

切换成功后，新格式位于 event.json，旧canonical投影位于 event_legacy.json；dialog/memory/task ID 和 UID游标继续保留。不要独立恢复旧tasks/dialog备份覆盖切换后已经发送的邮件状态。

---

## 6. 安装统一 service

模板只启动 `dfy_clock.py`，它同时负责收信回复和主动联系；不能再并行启动 receive／manual 入口。

```bash
sudo install -m 644 /srv/dfy/deploy/dfy-mail.service.example /etc/systemd/system/dfy-mail.service
sudo systemd-analyze verify /etc/systemd/system/dfy-mail.service
sudo systemctl daemon-reload
sudo systemctl enable dfy-mail.service
```

先确认旧 service 已停止且不会自动重新启动，避免两个不同 service 启动同一账号。

默认资源起点：

| 设置 | 含义 |
|---|---|
| CPUQuota=100% | 总配额约一个CPU核，给2核主机留余量；不是使用率保证 |
| MemoryHigh=768M | 支持的systemd/cgroup下开始内存压力控制 |
| MemoryMax=1G | 硬上限；超出可能OOM杀进程，不是“永不超过2GB”的应用保证 |
| Restart=on-failure / 30s | 故障退出30秒后重启 |
| StartLimitIntervalSec=300 / Burst=3 | 5分钟快速失败限流，触发后需排查再reset-failed |
| KillSignal=SIGINT / TimeoutStopSec=120s | 尝试正常中断，超时强杀仍需核验未知发送 |
| UMask=0077 | 新文件默认私有 |

这些选项依赖服务器systemd/cgroup支持；verify出现不支持的指令时要处理，不能无视。MemoryMax触发后不要只反复重启，应查RSS、图片大小、历史量与日志。无需启动本地LLM或新增数据库服务。

---

## 7. 启动与验收

启动后可能正常收发真实邮件和主动消息；在确认配置、历史与收件人正确后执行：

```bash
sudo systemctl start dfy-mail.service
sudo systemctl status dfy-mail.service --no-pager -l
sudo journalctl -u dfy-mail.service -n 100 --no-pager
sudo tail -n 60 /srv/dfy/log/output_log/dfy_clock.log
```

日志应同时满足：

1. 入口文件 `dfy_clock.log` 存在、UTF-8中文正常，含“状态日志启动检查”。
2. 统一收信／主动调度已就绪，无缺凭据或权限错误。
3. `dfy.graph` 下有 `code=graph_ready`，revision和coverage可见。
4. 没有持续的 `graph_projection_unavailable`；若有，当前是明确降级，**不能声称正在使用最新完整图谱**。
5. 没有 `DFY_LOGGING_ERROR`。有则看journal中的固定故障标记，检查磁盘、权限及轮转，不把控制台输出误当文件已写成功。

查看资源：

```bash
sudo systemctl show dfy-mail.service \
  -p MainPID -p MemoryCurrent -p MemoryPeak -p CPUUsageNSec -p NRestarts -p Result
free -h
df -h /srv/dfy
```

部分旧systemd没有MemoryPeak，需用系统现有监控查看实际RSS。至少观察一次正常回复、一次主动发送（如启用）、一次服务重启，确认任务不重复发、图谱水位前进、日志持续落盘。不要为验收手动清空游标或重复发送旧任务。

可在停服务时运行dry-run，核对源revision与导出的source_watermark；不要拿整份私有JSON发到公共渠道。

---

## 8. 故障回滚

```bash
sudo systemctl stop dfy-mail.service
cd /srv/dfy
sudo -u dfy .venv/bin/python -B src/graph_migrate.py rollback \
  --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

这会按当前已提交日志恢复旧事件视图，不把投递状态回退。需要恢复旧代码时，先确认其是否兼容新增graph字段和旧视图；不要把旧包一键覆盖全部memory。

启动限流已触发时，修复原因后：

```bash
sudo systemctl reset-failed dfy-mail.service
sudo systemctl start dfy-mail.service
```

日志文件不可写会阻止启动；运行中落盘故障会发安全告警但不会在SMTP中随意抛异常，避免改变邮件结果。不应把这个容错设计误解为可以长期无文件日志运行，应及时修磁盘或权限问题。

---

## 本地资源验证边界

合成200/600个revision的流式回放，额外Python tracemalloc峰值约0.553/1.494 MiB；这是**排除输入生成后的回放分配**，不是总RSS、全服务占用或服务器耗时承诺。输入JSON、任务载荷、Pillow解码、SDK和系统均额外占内存。完整历史规模及图片负载仍需在目标2核2GB主机验收。
