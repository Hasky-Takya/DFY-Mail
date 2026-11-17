# event 简化与知识图谱专项：实现与验证报告

## 完成范围

已实现专项代码与离线验证，尚未部署、启用或处理真实记忆。

- 单句事实视图、实体与别名、人物关系、人物状态、事件关系。
- 受控谓词、来源域、逐字文字证据、目标版本与可见ID校验。
- 与原story v1并存的同级graph_updates冻结包；只处理sent记录。
- 历史标注的独立冻结格式，不伪造旧原邮件或新发信记录。
- SQLite派生投影、原子JSON导出、覆盖状态、字符预算和一跳查询。
- 收信、手动主动、定时主动三个入口共用提取与投影，不固定增加在线模型调用。
- dry-run/build/annotate/cutover/rollback/query命令；历史云端整理须显式确认。
- 旧格式兼容、event_v2影子、event_legacy正式切换、回滚恢复当前状态。
- 可调参数集中在config/settings_example.json的graph节，实际运行从settings.json读取。
- 增加模型响应最低结构验证，将原先可能逃逸的choices属性访问错误变为受控协议错误；保留任务隔离与SMTP不确定结果禁重发。

## 验证结果

执行：

```bash
python -B -X utf8 -m unittest discover -s tests -q
```

最终结果：**409项离线测试全部通过**（约7秒；包括原有311项及新增98项）。

另通过：

- 所有Python源码AST语法检查；
- 修改后的prompt.json和settings_example.json解析检查；
- git diff --check，无空白错误；
- graph_migrate.py --help及迁移命令离线测试；
- DFY-Mail快照63个文件逐一与原ZIP比对，内容均未改变。

测试使用临时目录、合成故事、假LLM和假SMTP。没有连接真实邮箱、调用付费模型、执行真实迁移、重启服务或提交Git。

## 最后补充的回归保护

1. **源事实版本递进**：保留历史图谱版本链用于恢复后续更新；发布时只呈现与当前事实版本有效关联的结果。
2. **对称关系去重**：friend_of、sibling_of、planned_with反向端点映射到同一身份。
3. **状态过期**：确定性导出保留as-of水位；模型请求与查询还按实际查询时间过滤过期状态，不依赖新邮件到达。
4. **来源边界**：图片观察不能冒充用户文字证据；事件不能被图谱分类为用户偏好。
5. **覆盖率真实性**：空图谱或仅生成人物实体不能声称对应原事件已经单句简化。

## 文件与使用

- 配置模板：`config/settings_example.json`
- 使用指南：`docs/event_graph.md`
- 新模块：`src/graph_schema.py`、`graph_memory.py`、`graph_annotations.py`、`graph_store.py`、`graph_migrate.py`
- 集成：`dialog_memory.py`、`context_selection.py`和三个入口。

旧配置默认graph.enabled=false；不会自动覆盖event.json。启用只产生影子投影，正式切换仍需迁移命令及检查。历史单句语义质量、真实模型响应质量和目标2GB服务器资源消耗仍须在副本上实际评估。

## 尚未执行／不在本专项

- 未整理服务器现有128个事件或54条记忆，未人工审阅历史语义。
- 未执行正式cutover，未改动真实settings.json。
- 未验证或修改云服务器systemd设置。
- 未实现三层记忆、长期dialog压缩、双轮RAG。
- 现阶段SQLite仍是可重建派生库，不是全部记忆的唯一写入权威；原dialog完整保存，不做破坏性压缩。
