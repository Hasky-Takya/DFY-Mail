"""Offline v1 → v2 export; the running mail service still requires v1 files.

Use a static copy of all three input files. With no --output-dir this command
only validates and reports counts. Never install the export into the v1 service.
"""

import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from dialogueManager.dialog_memory import DialogMemory, MemoryError as DialogMemoryError
from memoryManager import story_memory as story


PROJECTIONS = (
    ("event.json", "events", "event_updates"),
    ("memory.json", "memories", "memory_updates"),
)


class MigrationError(Exception):
    """An input, validation, or export error safe to report without record data."""


def read_v1(path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise MigrationError(f"{path.name}：文件不存在。") from None
    except UnicodeError:
        raise MigrationError(f"{path.name}：文件不是有效的 UTF-8。") from None
    except OSError as exc:
        raise MigrationError(f"{path.name}：读取失败（{exc.strerror}）。") from None
    try:
        data = story._json_loads(text)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path.name}：JSON 无效（第 {exc.lineno} 行，第 {exc.colno} 列）。") from None
    except (ValueError, RecursionError):
        raise MigrationError(f"{path.name}：JSON 含重复键、非有限数字或超出解析限制。") from None
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise MigrationError(f"{path.name}：只接受 schema_version 为整数 1 的旧版文件。")
    return data


def validate_dialogs(data):
    try:
        DialogMemory._validate(data)
    except DialogMemoryError as exc:
        raise MigrationError(f"dialog.json：旧版记录校验失败：{exc}") from None
    if not data["dialogs"]:
        raise MigrationError("dialog.json：没有对话，请核对是否选中了正确的快照目录。")
    for index, record in enumerate(data["dialogs"]):
        if record["status"] != "sent":
            raise MigrationError(f"dialog.json：dialogs[{index}] 发送状态未确认；请先核验，不要重发。")


def split(source):
    dialog = read_v1(source / "dialog.json")
    validate_dialogs(dialog)
    old_projections = {name: read_v1(source / name) for name, _, _ in PROJECTIONS}
    requested_revisions = set()
    for name, data in old_projections.items():
        revision = data.get("revision")
        if type(revision) is not int or revision < 0:
            raise MigrationError(f"{name}：投影 revision 必须是非负整数。")
        requested_revisions.add(revision)

    context = story.empty_context()
    checkpoints = {0: context} if 0 in requested_revisions else {}
    batches = {name: [] for name, _, _ in PROJECTIONS}
    for index, record in enumerate(dialog["dialogs"]):
        if "story_updates" not in record:
            continue
        package = record["story_updates"]
        where = f"dialog.json：dialogs[{index}].story_updates"
        for _, _, key in PROJECTIONS:
            for item in package[key]:
                ref = item["source_refs"][-1]
                is_user = ref["source"] == "user"
                message_key = "user_message_id" if is_user else "assistant_message_id"
                if (ref["message_id"] != record[message_key]
                        or (is_user and record["type"] == "deepseek自己主动发送")):
                    raise MigrationError(f"{where}：{key} 的证据与所属邮件不匹配。")
        try:
            context = story._apply(context, package)
        except story.StoryValidationError as exc:
            raise MigrationError(f"{where}：旧批次回放失败：{exc}") from None
        if context["revision"] in requested_revisions:
            checkpoints[context["revision"]] = context
        for name, _, key in PROJECTIONS:
            updates = package[key]
            if key == "memory_updates":
                updates = [dict(item, source="dialogue", evidence_source=item["source"]) for item in updates]
            batch = {key: updates}
            batch.update((field, package[field]) for field in ("base_revision", "dialog_id", "observed_at"))
            batches[name].append(batch)

    for name, kind, _ in PROJECTIONS:
        old = old_projections[name]
        if not story._same(old, story._projection(context, kind)):
            prior = checkpoints.get(old["revision"])
            if prior is not None and story._same(old, story._projection(prior, kind)):
                raise MigrationError(f"{name}：旧投影落后于对话记录；请提供同一时点的完整快照。")
            raise MigrationError(f"{name}：旧投影与对话回放结果不符；请核对三份文件是否来自同一套快照。")

    outputs = {"dialog.json": dict(dialog, schema_version=2, dialogs=[
        {key: value for key, value in record.items() if key != "story_updates"}
        for record in dialog["dialogs"]
    ])}
    for name, _, _ in PROJECTIONS:
        outputs[name] = {"schema_version": 2, "revision": context["revision"], "batches": batches[name]}
    counts = {
        "dialogs": len(dialog["dialogs"]),
        "batches_per_file": context["revision"],
        "events": len(context["events"]),
        "memories": len(context["memories"]),
    }
    for name, kind, key in PROJECTIONS:
        counts[f"{kind}_versions"] = sum(len(batch[key]) for batch in batches[name])
    return outputs, counts


def serialize(outputs):
    encoded = {}
    for name, data in outputs.items():
        try:
            encoded[name] = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, RecursionError):
            raise MigrationError(f"{name}：无法将新版数据序列化为 UTF-8 JSON。") from None
    return encoded


def output_path(path, source):
    path = path.absolute()
    if not path.name or path.name == "..":
        raise MigrationError("输出路径必须指定一个新建目录名。")
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        raise MigrationError("输出目录的父目录不存在。") from None
    if not parent.is_dir():
        raise MigrationError("输出目录的父路径不是目录。")
    target = parent / path.name
    if target.is_relative_to(source):
        raise MigrationError("不能将输出放在源目录或它的子目录中。")
    return target


def export(target, encoded):
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise MigrationError(f"输出路径已存在，不覆盖：{target}") from None
    except OSError as exc:
        raise MigrationError(f"无法创建输出目录（{exc.strerror}）：{target}") from None
    created = []
    name = ""
    try:
        for name, payload in encoded.items():
            path = target / name
            with path.open("xb") as file:
                created.append(path)
                file.write(payload)
    except (OSError, KeyboardInterrupt) as exc:
        cleanup_failed = False
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        try:
            target.rmdir()
        except OSError:
            cleanup_failed = True
        reason = exc.strerror if isinstance(exc, OSError) else "操作中断"
        suffix = f"可能有残留，请勿使用：{target}" if cleanup_failed else "已清理本次输出。"
        raise MigrationError(f"{name}：导出未完成（{reason}）。{suffix}") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线拆分 v1 记忆；导出的 v2 不能给当前服务使用。")
    parser.add_argument("--source-dir", required=True, type=Path, help="存有三份 v1 JSON 的静态快照目录")
    parser.add_argument("--output-dir", type=Path, help="尚不存在的导出目录；不传则只校验")
    args = parser.parse_args(argv)
    try:
        try:
            source = args.source_dir.resolve(strict=True)
        except FileNotFoundError:
            raise MigrationError("源快照目录不存在，请核对 --source-dir。") from None
        if not source.is_dir():
            raise MigrationError("--source-dir 不是目录；请指定同时含三份 JSON 的静态快照。")
        target = output_path(args.output_dir, source) if args.output_dir is not None else None
        outputs, counts = split(source)
        encoded = serialize(outputs)
        if target is not None:
            export(target, encoded)
    except MigrationError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        # Path resolution failures can otherwise show a traceback for symlink loops.
        print(f"迁移失败：无法解析输入或输出路径（{exc}）。", file=sys.stderr)
        return 1
    if target is None:
        print("校验通过（未写入文件）。")
    else:
        print(f"离线导出完成：{target}（v2，当前服务不可读取）。")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
