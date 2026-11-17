# 图谱功能部署：如何接续现有记忆

## 先明确

最新的2核2GB服务器、文件日志和systemd完整操作步骤见 [部署清单](deploy_2c2g.md)。下文为记忆接续说明；不要将旧预览中已确认有语义错误的条目直接导入正式数据。

新代码兼容旧记忆，不需要清空任何JSON或重新建立收信基线。服务器仍在运行时，本地快照可能已经落后；**不能将本地整个memory目录或预览event.json覆盖到服务器**。

以下命令供用户部署时执行，本次没有操作服务器。将路径和service名称替换为实际值；service unit尚未检查，不猜测名称。

## 1. 停服务并做一致备份

1. 停止现有service，确认没有dfy_clock/dfy_receive/手动发信进程占用记忆。
2. 整体备份服务器最新memory目录，包括dialog.json、event.json、memory.json、tasks.sqlite3、tasks.initialized，以及存在的SQLite附属文件和所有图谱文件；不要在服务写入时只复制一个SQLite文件。
3. 保留实际settings.json、授权码环境、API密钥环境和service工作目录。
4. 更新src、tests、README/docs与提示词的图谱提取指令。不用settings_example覆盖真实settings，不用本地快照覆盖线上数据。
5. 如果提示词已在服务器自定义，只合并story_extraction_prompt中的新图谱规则，保留角色设定。

## 2. 先保持旧格式启动条件

将示例的graph小节复制到实际settings.json，初次保持enabled=false。未知参数、null及错误类型会被拒绝。服务不必马上启动，建议先在备份副本跑测试与检查。

```bash
python -B -m unittest discover -s tests -q
python -B src/graph_migrate.py dry-run --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

- dry-run验证最新dialog和两份旧投影，不写文件，不联网。
- prepared/sending/unknown必须先核实发送结果；不要为了迁移手工改成sent，也不要盲目重发。
- 旧quarantined任务保持原状态，不因图谱迁移自动释放。
- 保留tasks.sqlite3及初始化标记，保留UID游标，避免重启后跳信或重建基线。

## 3. 生成影子投影

```bash
python -B src/graph_migrate.py build --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

输出graph_store.sqlite3及event_v2.json。正式event.json仍为旧格式。历史条目此时可能仍标待简化，不能据此声称历史已整理。

## 4. 对接旧事件与记忆

有两种路径，选择其一即可。

### A. 直接整理服务器最新历史（推荐）

```bash
python -B src/graph_migrate.py annotate --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json --llm --confirm-api --max-batches 1
```

先配置DEEPSEEK_API_KEY。此命令产生API费用并向模型发送源条目/证据。

- 每源条目一次请求；一次最多batch_size × max_batches请求，默认8次。
- 命令停止时会释放锁，不要与运行中的service并发写记忆。
- 根据dry-run剩余数量分批执行，不直接无限循环。
- 默认整理event和memory源条目；因为新导出含偏好/画像引用，正式完整切换的coverage包含两者。

### B. 复用已经审阅的本地预览标注

预览的graph_annotations.jsonl可作为导入候选，**不是直接复制新event文件**。

```bash
python -B src/graph_migrate.py annotate --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json --input /safe/reviewed_graph_annotations.jsonl
```

导入严格校验源ID、版本、内容哈希和依赖。若服务器事件已改变，整批可能拒绝，应以最新副本重新整理，或谨慎筛选完整依赖闭包；不要删掉校验字段强行导入。预览只整理事件，不整理memory，成功导入也不意味着完整coverage。

## 5. 正式切换

人工核对单句、人物别名、关系和状态；再检查：

```bash
python -B src/graph_migrate.py dry-run --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
python -B src/graph_migrate.py cutover --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

默认要求coverage完整。不要为了绕过异常直接使用allow-partial。

切换后：

- event.json：新紧凑图谱导出；
- event_legacy.json：原canonical事件投影；
- dialog.json：原历史完整保留，新发信追加图谱冻结包；
- memory.json：现有条目和ID保留，不做三层拆分；
- tasks.sqlite3：任务、游标、去重状态继续沿用；
- graph_annotations.jsonl：历史整理记录；
- graph_store.sqlite3：可重建派生；
- graph_migration_manifest.json：格式切换标记。

将graph.enabled设为true，再启动service，使新对话继续生成图谱。检查日志中图谱错误和coverage，同时检查收信游标、任务队列与SMTP结果。

## 6. 出问题时

```bash
python -B src/graph_migrate.py rollback --directory /srv/dfy/memory --settings /srv/dfy/config/settings.json
```

先停服务再执行。回滚按当前dialog生成最新旧格式事件，不还原旧发送状态。**不要恢复旧tasks/dialog备份去覆盖部署后已发出的邮件记录**，否则可能丢失发送依据。

图谱数据库损坏与原始提交日志损坏不同：前者备份诊断后可重建；后者应停机核验。正式切换后不能直接运行不了解新格式的旧代码。

## 临时预览脚本

仅整理本地服务器快照的event，不整理memory、不发信、不调用service：

```bash
python -B tools/preview_snapshot_events.py --source DFY-Mail/memory/event.json --output deliverables/event_preview_20261115 --confirm-api
```

中断后续跑：

```bash
python -B tools/preview_snapshot_events.py --source DFY-Mail/memory/event.json --output deliverables/event_preview_20261115 --confirm-api --resume
```

逐条保存冻结标注，成功项不重复请求。默认每项最多2次尝试（可设1–3），全部128项最多约256次请求；实际依据快照事件数量。失败项保留原文并单列，不能将“有文件”当成全部成功。

主要输出：comparison.md（前后对照）、event_preview.json（图谱）、summary.json（统计/失败）、graph_annotations.jsonl（冻结标注）、preview_manifest.json（源哈希和调用数）。这些都可能含私有故事或用户信息，勿公开上传。
