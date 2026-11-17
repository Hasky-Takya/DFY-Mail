# 单句事件与知识图谱使用指南

本功能不实现三层记忆、dialog压缩或双轮RAG；它们仍是独立后续工作。图谱使用标准库SQLite，不需要Neo4j或向量服务。

## 1. 数据职责

- `dialog.json` 中的 `story_updates` 仍是事件/memory权威；所有旧ID与版本保留。
- 新记录可有同级 `graph_updates`，冻结单句、实体、关系和状态候选。只有 `sent` 记录应用。
- `graph_annotations.jsonl` 保存历史整理的冻结结果及哈希，标明历史条目/引用来源，不伪造旧邮件全文。
- `graph_store.sqlite3` 是可重建投影；`event_v2.json` 是影子导出。
- 显式切换后，旧格式事件投影位于 `event_legacy.json`；新 `event.json` 是 `dfy.compact_event_graph` 格式。旧memory引用的事件ID不变。
- 不要手工修改投影或清空dialog。图谱派生错误不触发SMTP重发，权威日志损坏仍需停机核验。

## 2. 参数

`config/settings_example.json` 展示全部可调参数。它是模板，不是运行时配置；把 `graph` 一节复制到实际 `config/settings.json`，然后按需修改。

```json
{
  "graph": {
    "enabled": false,
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
  }
}
```

| 参数 | 含义 | 允许值 |
|---|---|---|
| enabled | 启用在线候选抽取与影子投影 | true/false；旧配置默认false |
| max_updates | 单次候选五类操作合计上限 | 1–64 |
| max_text_chars | 单句等受控文本上限；超限拒绝，不截断 | 1–4000 |
| context_items | 请求视图每类候选上限 | 1–200 |
| context_chars | 图谱请求视图JSON字符预算 | 256–100000 |
| query_limit | 一跳查询默认结果数 | 1–200（查询还受内部安全上限约束） |
| batch_size | 历史整理每批源条目数 | 1–64 |
| sqlite_cache_kib | SQLite页缓存KiB | 256–262144，默认8192 |
| assistant_name / user_name | 角色与用户的规范人物名 | 非空单行字符串 |
| assistant_aliases / user_aliases | 明确固定称呼，不自动映射我/你 | 每组最多16个不同别名 |

缺失字段补程序默认值；未知字段、单项null、字符串数字、bool冒充整数均拒绝。enabled不控制正式文件格式：切换后即使关闭新抽取，也不能让旧校验器覆盖compact event。

启用 `enabled: true` 不会自动进行历史付费整理，也不会自动切换正式event文件。尚未简化的条目保留原文本并标记待整理，不通过截断伪装成功。

## 3. 在线行为

收信回复、定时主动和手动入口共享同一套图谱流程。正常文字邮件仍是正文一次、联合提取一次，不固定增加第三次API。

- 程序提供图谱模板，模型返回 `graph`，含 facts/entities/relations/states/event_relations 五数组。
- E1/M1对应本轮第一个新事件/记忆；已有目标必须是本轮展示的ID与version。
- 程序验证证据、来源域、端点和版本，生成ID及冻结包。
- 事实单句只改展示，不改变原事实状态。
- 核心JSON/摘要/story非法仍按原规则修复或隔离；仅图谱子对象非法时记录incomplete，不能污染图谱。
- 未发送与unknown记录不应用。sent后新派生故障只降级/待恢复，不重新发信。
- 当前图谱仅接受当前文字证据；图片独立观察不直接扩展成图谱现实事实。

不要把 `complete` 理解为语义全部正确：逐字证据只能证明引用存在，关系判断仍需人工审阅。

## 4. 迁移前准备

**先停运行服务，整套备份记忆目录与任务SQLite，在副本上验证；不要直接对 `DFY-Mail` 只读快照运行写命令。**

以下命令是供用户将来执行的操作说明，本次实现不会自动执行这些命令。

```bash
python src/graph_migrate.py --help
python src/graph_migrate.py dry-run --directory /path/to/memory --settings config/settings.json
```

dry-run默认只读，不创建SQLite、锁文件或投影。`query` 同样只读。其他命令拿service/dialog锁；若服务仍占锁应拒绝操作，不通过删除锁文件绕过。

## 5. 建立影子投影

```bash
python src/graph_migrate.py build --directory /path/to/memory --settings config/settings.json
```

build会验证已有dialog和投影，再生成派生库、影子导出。默认不会替换正式event；历史源条目仍会标记待简化。

## 6. 历史单句和语义图谱整理

### 离线导入已冻结标注

```bash
python src/graph_migrate.py annotate --directory /path/to/memory --settings config/settings.json --input reviewed_annotations.jsonl
```

输入每行是 `{ "package": <冻结标注>, "hash": <digest(package)> }`。应通过 `graph_annotations.freeze_annotation()` 生成包，而非人工拼出哈希。导入验证源版本/哈希、引用与证据；坏输入不替换已有标注日志。

### 显式使用DeepSeek整理

```bash
python src/graph_migrate.py annotate --directory /path/to/memory --settings config/settings.json --llm --confirm-api --max-batches 1
```

该命令会把历史源条目/已有证据发送至模型并产生费用。必须显式提供 `--llm --confirm-api`，并预先设置 `DEEPSEEK_API_KEY`；不会交互索要或打印密钥。默认只处理有限批次，不自动循环全库。每个源条目一次模型请求，每个维护批最多 `batch_size` 个条目；因此最大请求数为 `batch_size × max_batches`（默认最多8次），不会把“1批”误当成只有1次付费调用。逐项整理会展示此前已冻结的实体，避免同名人物反复新建。

历史整理保留否定、期限、状态与来源域；源是旧摘要/事件内容时明确标注历史来源，不能当作完整原邮件。模型不生成关系时保留空数组，不能强行凑图谱。

完成后再次dry-run，检查coverage、待整理项与错误；人工审阅单句与重要关系，再决定正式切换。

## 7. 正式切换与回滚

```bash
python src/graph_migrate.py cutover --directory /path/to/memory --settings config/settings.json
```

默认要求影子与当前输入一致、旧事件完整且coverage完成。切换保留原文件备份、写迁移manifest，将旧canonical事件放到event_legacy，再发布新event。`--allow-partial` 是显式接受未完成覆盖，不应拿它掩盖未知错误。

切换时崩溃可重新执行cutover恢复；不要运行旧版程序覆盖新格式。需要回滚时：

```bash
python src/graph_migrate.py rollback --directory /path/to/memory --settings config/settings.json
```

回滚从**当前**已提交dialog重建旧格式，不把邮件任务和发送状态还原到旧备份；这样不会将已发邮件变回待发。新graph字段保留可审计，只切换视图/功能。

## 8. 查询

```bash
python src/graph_migrate.py query --directory /path/to/memory --name 桃井 --limit 10
python src/graph_migrate.py query --directory /path/to/memory --kind events --id EVENT_ID
```

Python接口：`GraphStore.get_fact()`、`resolve_entity()`、`get_entity_context()`、`get_event_context()`。图查询限制一跳；`request_view()`限制项目数和序列化字符，不能将其当完整canonical更新依据。

关系采用受控谓词：friend_of、sibling_of、planned_with为人物—人物，member_of为人物—团体，works_on为人物—项目，participates_in/organizes为人物—事件或项目；事件关系采用follows、part_of、caused_by、fulfills、cancels。人物关系及事件关系禁止自环。旧冻结包保持hash，但非法旧断言在投影隔离，并通过coverage.rejected_assertions_count/rejected_assertion_ids报告，不等于自动纠正原始事实。

临时人物状态包括working_on、located_at、busy_with。来源域必须保留，不从沉默推断情绪，不从先后推断因果。当前有效图谱状态会写入dfy.graph日志；持续graph_projection_unavailable表示降级，应修复后再声称已使用最新视图。服务器完整步骤见[2核2GB部署清单](deploy_2c2g.md)。

## 9. 故障与重建

- JSON导出失败：保留已提交状态，后续sync补导出。
- SQLite损坏：报错并保留原文件；先停服务备份，再将损坏派生库移到诊断位置，用build重建。不要移动任务库或权威日志。
- 源事件版本变化：先保留旧图谱版本链以恢复后续修订，再按当前源版本筛选发布；旧简句/状态不得继续冒充最新，coverage会重新反映待整理项。
- 临时状态的导出是带水位的确定性快照；模型请求和一跳查询会额外按实际查询时间过滤过期状态，即使没有新邮件也不会一直沿用过期状态。
- 标注JSONL损坏：停止使用并核验备份，不能随意跳过坏行。
- 不确定SMTP：保持人工核验规则；迁移工具不解除unknown/quarantined。

## 10. 验证范围

自动化测试使用临时目录、合成邮件与假LLM/SMTP。新增测试覆盖schema、证据、稳定ID、跨源版本、投影事务、重建、切换回滚和三入口。测试通过不代表历史语义已经人工审阅，或真实服务配置、模型质量和2GB服务器性能已经验证。
