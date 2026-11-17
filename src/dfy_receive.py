"""持久收信任务与游标，调用DeepSeek并安全恢复重试。"""

import imaplib
import json
import logging
import math
import os
import re
import smtplib
import time
from collections import deque
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from openai import APIConnectionError, APIStatusError, OpenAIError

from dfy_test import DeepSeekChat
from dialog_memory import (
    DialogMemory, MEMORY_PATH, MemoryError, SummaryError, ContextBudgetError, load_story_extraction_prompt, load_summary_prompt, new_record,
    now_iso, redact, render_email_prompt, resolve_reply, send_recorded, summarize, snapshot_basis,
)
from mail import MailboxStateError, MailClient, _load_settings
from mail_logging import close_logging, set_log_secrets, start_logging, apply_logging_settings
from runtime_config import require_credential
from vision import normalize_user_content, observe_images
from retry_policy import classify_chain, classify_error, retry_delay
from task_store import TaskStore, UidValidityChanged, SingleInstanceLock


logger = logging.getLogger("dfy.receive")
logger.addHandler(logging.NullHandler())


PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prompt.json"


def interval_seconds(settings: dict) -> float:
    minutes = settings.get("check_interval_minutes", 1)
    if (
        type(minutes) not in (int, float) or not math.isfinite(minutes)
        or minutes <= 0 or not math.isfinite(minutes * 60)
    ):
        raise ValueError("check_interval_minutes 必须是有限正数，单位为分钟。")
    return minutes * 60


def load_reply_prompt(prompt_path: str | Path = PROMPT_PATH) -> str:
    with Path(prompt_path).open(encoding="utf-8-sig") as file:
        settings = json.load(file)
    if not isinstance(settings, dict):
        raise ValueError("prompt.json 必须是 JSON 对象。")
    try:
        return render_email_prompt(settings, "reply_task").strip()
    except ValueError as exc:
        raise ValueError(f"config/prompt.json 中的 prompt 或 reply_task 无效：{exc}") from None


def reply_subject(subject: str) -> str:
    subject = re.sub(r"[\r\n]+", " ", subject).strip() or "你的来信"
    return subject if re.match(r"(?i)^re\s*:", subject) else f"Re: {subject}"


class MailReceiver:
    """一个实例用于一次监听；不要同时启动多个脚本实例。

    首次建立基线，之后恢复持久游标及任务。来信正文会传给 DeepSeek。
    模型坏输出隔离，网络无限退避；发送不确定仍需人工核验。
    """

    def __init__(
        self, mailbox: MailClient, bot: DeepSeekChat, reply_prompt: str, *,
        memory_path: str | Path = MEMORY_PATH, summary_prompt: str | None = None, secrets=(),
        on_user_message=None, extraction_prompt: str | None = None,
        task_store=None, clock=None, graph_settings=None,
    ):
        self.on_user_message = on_user_message
        self.clock = clock or time.time
        queue_path = Path(memory_path).parent / "tasks.sqlite3"
        queue_marker = queue_path.with_suffix(".initialized")
        if task_store is None and queue_marker.exists() and not queue_path.exists():
            raise MemoryError("持久任务库缺失，code=queue_missing；请恢复备份，禁止重新建立基线。")
        self.store = task_store or TaskStore(queue_path)
        if task_store is None and not queue_marker.exists():
            queue_marker.write_text("durable-queue-v1\n", encoding="ascii")
        self.account = "default-mailbox"  # 数据库按记忆目录隔离，迁移路径不改变账户游标。
        self.paused = None
        self.sync_failures = 0
        self.sync_retry_at = 0
        self.blocked_reason = None
        self.extraction_prompt = extraction_prompt
        self.mailbox = mailbox
        self.bot = bot
        self.reply_prompt = reply_prompt
        self.memory = DialogMemory(memory_path, secrets=secrets, graph_settings=graph_settings)
        self.summary_prompt = summary_prompt if summary_prompt is not None else load_summary_prompt()
        self.stopped = False
        self.cursor = None
        self.pending = deque()
        self.seen_uids = set()
        self.seen_ids = set()
        self.observed_user_activity = []
        self.observation = {
            "started_at": None, "baseline": None, "sync_ok": None,
            "last_successful_sync": None, "coverage_through": None,
            "history_may_be_incomplete": True,
        }

    def observe_user_activity(self, message: dict) -> None:
        """只保留活动元数据，绝不把其他来信的主题或正文放进当前上下文。"""
        keys = ("uid", "uidvalidity", "message_id", "received_at", "received_at_source",
                "in_reply_to", "references")
        activity = {key: message[key] for key in keys if key in message}
        identity = (activity.get("uidvalidity"), activity.get("uid"))
        if identity != (None, None) and any(
            (item.get("uidvalidity"), item.get("uid")) == identity
            for item in self.observed_user_activity
        ):
            return
        if activity.get("message_id") and any(
            item.get("message_id") == activity["message_id"]
            for item in self.observed_user_activity
        ):
            return
        self.observed_user_activity.append(activity)

    def prune_observed_activity(self) -> None:
        """已确认往来由历史提供；内存只保留待处理事件及最近活动，避免重复堆积。"""
        if not self.observed_user_activity:
            return
        def timestamp(item):
            try:
                value = datetime.fromisoformat(item.get("received_at") or "")
                if value.tzinfo is not None:
                    return value.astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass
            return datetime.min.replace(tzinfo=timezone.utc)
        latest = max(self.observed_user_activity, key=timestamp)
        unknown = next((item for item in reversed(self.observed_user_activity)
                        if item.get("received_at") is None), None)
        pending_ids = {(task["message"].get("uidvalidity"), task["message"].get("uid"))
                       for task in self.pending}
        history = self.memory.snapshot()["dialogs"]
        recovered_targets = {item.get("reply_to_assistant_message_id") for item in history}
        witnesses = {}
        for item in self.observed_user_activity:
            if item.get("received_at") is None:
                continue
            target, _ = resolve_reply(history, item)
            if target is not None and target not in recovered_targets:
                witnesses[target] = item
        witness_ids = {id(item) for item in witnesses.values()}
        self.observed_user_activity = [
            item for item in self.observed_user_activity
            if item is latest or item is unknown or id(item) in witness_ids
            or (item.get("uidvalidity"), item.get("uid")) in pending_ids
        ]

    def _load_pending(self):
        self.pending = deque()
        for row in self.store.list_tasks(account=self.account):
            task = row["payload"]
            task["task_id"] = row["id"]
            task["next_attempt_at"] = row["next_attempt_at"]
            self.pending.append(task)

    def _save_task(self, task, status="pending", *, code=None, when=0, dialog_id=None):
        self.store.save(task["task_id"], task, status=status, next_attempt_at=when,
                        error_code=code, dialog_id=dialog_id)

    def recover_memory(self):
        try:
            bound = self.store.list_tasks(statuses=("sending", "unknown", "blocked_unknown", "done"), kind=None, account=self.account)
            if any(row["dialog_id"] for row in bound):
                if not self.memory.path.exists() or not self.memory.path.read_text(encoding="utf-8-sig").strip():
                    raise MemoryError("存在发送计划但对话日志缺失，禁止将其当空历史恢复。")
            snapshot = self.memory.snapshot(include_story=self.extraction_prompt is not None)
            self.store.reconcile(snapshot["dialogs"])
            self.blocked_reason = None
            self.stopped = False
            return snapshot
        except MemoryError:
            self.blocked_reason = "memory_recovery_required"
            logger.error("记忆暂不可提交，code=memory_recovery_required；继续接收持久化，不自动重发。")
            return None

    def start(self) -> None:
        if self.cursor is not None:
            raise RuntimeError("该监听实例已经启动。")
        self.recover_memory()
        saved = self.store.cursor(self.account)
        logger.info("正在恢复收信游标；首次运行才建立基线。")
        self.paused = self.store.get_control("service:imap:paused")
        if self.paused and saved is not None:
            self.cursor = saved
            self._load_pending()
            self.observation.update(started_at=now_iso(), baseline=list(self.cursor))
            return
        if self.paused:
            logger.critical("启动收信暂停，code=%s；等待人工恢复。", self.paused)
            raise RuntimeError("imap_service_paused")
        remote = self.mailbox.get_mailbox_cursor()
        if saved is not None and saved[0] != remote[0]:
            self.paused = "uidvalidity_changed"
            self.store.set_control("service:imap:paused", self.paused)
            logger.critical("收信暂停，code=uidvalidity_changed；需人工核验，原游标保留。")
            self.cursor = saved
        else:
            self.cursor = saved if saved is not None else remote
            if saved is None:
                self.store.set_cursor(self.account, self.cursor)
        self._load_pending()
        # 基线建立过程可能略过同期来信，观测区间从建立完成后算起。
        self.observation.update(started_at=now_iso(), baseline=list(self.cursor))
        logger.info("基线建立完成：UIDVALIDITY=%s UID=%s", *self.cursor)

    def poll_once(self) -> bool:
        """返回本轮 IMAP 是否同步成功；API 临时失败仍保留队首。"""
        if self.paused and not self.store.get_control("service:imap:paused"):
            self.paused = None
        imap_due = not self.paused and self.clock() >= self.sync_retry_at
        if self.cursor is None:
            raise RuntimeError("请先启动监听并建立基线。")
        logger.info("正在检查 INBOX 新邮件，当前 UID=%s pending=%s", self.cursor[1], len(self.pending))
        imap_ok = imap_due
        sync_started_at = now_iso()
        self.observation["sync_ok"] = False
        cursor, messages = self.cursor, []
        try:
            if imap_due:
                cursor, messages = self.mailbox.read_user_emails_since(self.cursor)
                self.observation.update(
                    sync_ok=True, last_successful_sync=now_iso(), coverage_through=sync_started_at,
                )
        except (imaplib.IMAP4.abort, OSError) as exc:
            severity, code = classify_chain(exc, "imap")
            if severity == "fatal":
                self.paused = code
                self.store.set_control("service:imap:paused", code)
                logger.critical("收信服务暂停，code=%s type=%s。", code, type(exc).__name__)
            else:
                self.sync_failures += 1
                self.sync_retry_at = self.clock() + retry_delay(self.sync_failures)
            imap_ok = False
            logger.warning(
                "收信暂时中断，type=%s；游标及队列保留，下一周期重试。",
                type(exc).__name__, exc_info=True,
            )
            if not self.pending:
                return False
            cursor, messages = self.cursor, []
            logger.info("本轮无法获取新增邮件，继续处理已经完整入队的任务。")
        except (imaplib.IMAP4.error, MailboxStateError) as exc:
            severity, code = classify_error(exc, "imap")
            if isinstance(exc, MailboxStateError):
                severity, code = "fatal", "uid_state_invalid"
            if severity == "fatal":
                self.paused = code
                self.store.set_control("service:imap:paused", code)
                logger.critical("收信暂停，code=%s type=%s；请修复后显式恢复。", code, type(exc).__name__)
            else:
                self.sync_failures += 1
                self.sync_retry_at = self.clock() + retry_delay(self.sync_failures)
                logger.warning("收信等待重传，code=%s retry=%s delay_seconds=%s。", code,
                               self.sync_failures, retry_delay(self.sync_failures))
            imap_ok = False
            cursor, messages = self.cursor, []

        if messages:
            logger.info("检查完成，发现 %s 封符合条件的用户来信。", len(messages))
        else:
            logger.info("检查完成，未发现可回复的用户新邮件，扫描到 UID=%s。", cursor[1])
        if imap_ok:
            self.sync_failures = 0
            self.sync_retry_at = 0
        # 接收任务和扫描游标同一事务提交，崩溃后不会只前进游标却丢信。
        for message in messages:
            message.setdefault("received_at", now_iso())
            message.setdefault("received_at_source", "observed_at")
        inserted = self.store.ingest(self.account, cursor, messages)
        new_ids = {(row["payload"]["message"]["uidvalidity"], row["payload"]["message"]["uid"])
                   for row in inserted}
        for message in messages:
            uid = (message["uidvalidity"], message["uid"])
            identifier = message["message_id"].strip()
            if uid not in new_ids or uid in self.seen_uids or (identifier and identifier in self.seen_ids):
                logger.debug("跳过重复邮件，UID=%s", message["uid"])
                continue
            # 去重同时覆盖已经入队和已经回复的邮件。
            self.seen_uids.add(uid)
            if identifier:
                self.seen_ids.add(identifier)
            message.setdefault("received_at", now_iso())
            message.setdefault("received_at_source", "observed_at")
            self.observe_user_activity(message)
            if not message["body"].strip() and not message.get("images") and not message.get("image_warnings"):
                logger.info("跳过空正文邮件，UID=%s", message["uid"])
                row = next(row for row in inserted if row["payload"]["message"]["uid"] == message["uid"])
                self.store.finish(row["id"])
                continue
            if self.on_user_message is not None:
                self.on_user_message(message)
        self.cursor = cursor
        self._load_pending()
        if self.recover_memory() is None:
            return False
        self._load_pending()
        self.prune_observed_activity()

        for _ in range(len(self.pending)):
            task = self.pending[0]
            message = task["message"]
            if self.clock() < task.get("next_attempt_at", 0):
                self.pending.rotate(-1)
                continue
            if self.store.get_control("service:api:paused"):
                return imap_ok
            current_snapshot = self.memory.snapshot(include_story=self.extraction_prompt is not None)
            if "snapshot" in task and snapshot_basis(task["snapshot"]) != snapshot_basis(current_snapshot):
                for key in ("snapshot", "body", "summaries", "validation_feedback"):
                    task.pop(key, None)
            if "snapshot" not in task:
                task["snapshot"] = current_snapshot
            logger.info(
                "正在为 UID=%s 调用 DeepSeek，attempt=%s body_chars=%s",
                message["uid"], task["attempts"] + 1, len(message["body"]),
            )
            started = time.perf_counter()
            try:
                if "user_content" not in task:
                    observations = []
                    if message.get("images"):
                        observations = observe_images(self.bot, message["body"], message["images"])
                    observations = json.loads(redact(json.dumps(observations, ensure_ascii=False), self.memory.secrets))
                    task["image_observations"] = observations
                    task["user_content"], task["user_evidence"] = normalize_user_content(
                        message["body"], observations, message.get("image_warnings", []),
                    )
                    message.pop("images", None)
                    task["attempts"] = 0
                    task["network_failures"] = 0
                    self._save_task(task)
                question = self.memory.question(task["snapshot"], (
                    f"邮件主题：{message['subject']}\n\n用户邮件正文与附图观察：\n{task['user_content']}"
                ), mode="reply", current_message=message,
                    observed_user_activity=self.observed_user_activity,
                    observation=self.observation, as_of=now_iso(),
                    story_context=task["snapshot"].get("story_context"), system_prompt=self.reply_prompt)
                if "body" not in task:
                    task["body"] = self.bot.ask(question, system_prompt=self.reply_prompt)
                    task["network_failures"] = 0
                    self._save_task(task)
                    logger.info("DeepSeek 生成完成，UID=%s duration=%.2fs", message["uid"], time.perf_counter() - started)
                    task["attempts"] = 0
                if "summaries" not in task:
                    logger.info("正在提炼双方梗概，UID=%s", message["uid"])
                    task["summaries"] = summarize(
                        self.bot, self.summary_prompt, task["user_content"], task["body"], secrets=self.memory.secrets,
                        user_evidence=task["user_evidence"],
                        story_context=task["snapshot"].get("story_context"), extraction_prompt=self.extraction_prompt,
                        history=[{key: item[key] for key in ("type", "received_at", "sent_at", "user_summary", "assistant_summary")}
                                 for item in task["snapshot"]["dialogs"]],
                        anchors={"as_of": now_iso(), "received_at": message["received_at"],
                                 "received_at_source": message["received_at_source"]},
                        validation_feedback=task.get("validation_feedback"),
                        graph_context=task["snapshot"].get("graph_context"),
                        graph_canonical=task["snapshot"].get("graph_canonical"),
                        graph_settings=self.memory.graph_settings,
                    )
                body = task["body"]
            except (OpenAIError, ValueError) as exc:
                if isinstance(exc, SummaryError):
                    task["validation_feedback"] = exc.repair_feedback()
                    logger.warning("联合提取校验失败，UID=%s code=%s field=%s。",
                                   message["uid"], exc.details["code"], exc.details["path"])
                task["attempts"] += 1
                logger.warning(
                    "DeepSeek 请求异常，UID=%s type=%s status=%s attempt=%s duration=%.2fs",
                    message["uid"], type(exc).__name__, getattr(exc, "status_code", None),
                    task["attempts"], time.perf_counter() - started, exc_info=True,
                )
                severity, code = classify_chain(exc)
                if severity == "transient":
                    task["network_failures"] = task.get("network_failures", 0) + 1
                    delay = retry_delay(task["network_failures"])
                    task["next_attempt_at"] = self.clock() + delay
                    self._save_task(task, "retry", code=code, when=task["next_attempt_at"])
                    logger.warning("模型等待重传，code=%s retry=%s delay_seconds=%s。", code, task["network_failures"], delay)
                    self.pending.rotate(-1)
                    continue
                if severity == "fatal":
                    self.store.set_control("service:api:paused", code)
                    self._save_task(task, code=code)
                    logger.critical("模型服务暂停，code=%s；任务保留，修复后显式恢复。", code)
                    return imap_ok
                task["invalid_failures"] = task.get("invalid_failures", 0) + 1
                if task["invalid_failures"] >= 3:
                    self.store.quarantine(task["task_id"], task, error_code=code)
                    logger.error("任务已隔离，task_id=%s code=%s；其他任务继续。", task["task_id"], code)
                    self.pending.popleft()
                    continue
                self._save_task(task, code=code)
                self.pending.rotate(-1)
                continue
            except ContextBudgetError:
                self.store.quarantine(task["task_id"], task, error_code="current_message_too_large")
                logger.error("任务已隔离，task_id=%s code=current_message_too_large。", task["task_id"])
                self.pending.popleft()
                continue

            if self.store.get_control("service:smtp:paused"):
                self._save_task(task, code="smtp_service_paused")
                self.pending.rotate(-1)
                continue
            logger.info("开始保存梗概并发送回复，原信 UID=%s", message["uid"])
            # 从此处起任何中断都不得重新进入 SMTP；磁盘上保留可核验状态。
            self.stopped = True
            record = None
            try:
                record = new_record(task["snapshot"], message, task["summaries"],
                                    image_observations=task.get("image_observations"))
                self._save_task(task, "sending", dialog_id=record["id"])
                send_recorded(
                    self.memory, task["snapshot"], record, self.mailbox,
                    reply_subject(message["subject"]), body,
                    in_reply_to=message["message_id"], references=message.get("references", ""),
                    auto_replied=True,
                )
            except (MemoryError, smtplib.SMTPException, OSError, ValueError) as exc:
                logger.error(
                    "发信步骤异常，UID=%s type=%s smtp_code=%s；结果可能不确定，停止监听。",
                    message["uid"], type(exc).__name__, getattr(exc, "smtp_code", None), exc_info=True,
                )
                if record is None:
                    self.store.quarantine(task["task_id"], task, error_code="record_validation_failed")
                    self.pending.popleft()
                    self.stopped = False
                    continue
                severity, code = classify_chain(exc, "smtp")
                if code in ("smtp_auth_failed", "tls_verification_failed"):
                    self.store.set_control("service:smtp:paused", code)
                    logger.critical("SMTP服务暂停，code=%s；原发送任务待核验。", code)
                self._save_task(task, "blocked_unknown", code="send_or_projection_unconfirmed", dialog_id=record["id"])
                self.blocked_reason = "send_or_projection_unconfirmed"
                logger.critical("发送已暂停，code=send_or_projection_unconfirmed；继续收信，不自动重发。")
                return False
            self.store.finish(task["task_id"])
            self.pending.popleft()
            self.prune_observed_activity()
            self.stopped = False
            logger.info("SMTP 已接受回复且记忆已确认，原信 UID=%s（不保证最终投递）。", message["uid"])
        return imap_ok

    def run(self, seconds: float) -> None:
        failures = 0
        while self.cursor is None:
            try:
                self.start()
            except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
                severity, code = classify_chain(exc, "imap")
                if severity == "fatal":
                    self.store.set_control("service:imap:paused", code)
                    logger.critical("启动服务暂停，code=%s type=%s。", code, type(exc).__name__)
                    while self.store.get_control("service:imap:paused"):
                        time.sleep(256)
                else:
                    failures += 1
                    delay = retry_delay(failures)
                    logger.warning("启动连接重传，code=%s delay_seconds=%s。", code, delay)
                    time.sleep(delay)
                self.cursor = None
        logger.info("监听已就绪，每 %g 分钟检查一次；只处理基线之后的用户新邮件。", seconds / 60)
        logger.info("按Ctrl+C停止。不要同时运行多个实例；重启恢复持久游标及待处理任务。")
        deadline = time.monotonic() + seconds
        while True:
            time.sleep(max(0, deadline - time.monotonic()))
            try:
                self.poll_once()
            except MemoryError as exc:
                self.blocked_reason = "memory_recovery_required"
                logger.critical("写信暂停，code=memory_recovery_required type=%s。", type(exc).__name__)
            now = time.monotonic()
            waits = [seconds]
            wall = self.clock()
            if self.sync_retry_at > wall:
                waits.append(self.sync_retry_at - wall)
            waits.extend(task["next_attempt_at"] - wall for task in self.pending
                         if task.get("next_attempt_at", 0) > wall)
            deadline = now + max(0.05, min(waits))


def main() -> None:
    log_path = start_logging("dfy_receive.py")
    mailbox = None
    instance_lock = SingleInstanceLock(MEMORY_PATH.parent / "service.lock")
    try:
        instance_lock.acquire()
        logger.info("监听进程启动，日志文件：%s", log_path)
        settings = _load_settings()
        apply_logging_settings(settings.get("logging"))
        set_log_secrets(os.getenv(settings["assistant"]["auth_code_env"], ""))
        logger.info("邮箱配置加载完成。")
        seconds = interval_seconds(settings)
        prompt = load_reply_prompt()
        summary_prompt = load_summary_prompt()
        extraction_prompt = load_story_extraction_prompt()
        api_key = require_credential("DEEPSEEK_API_KEY", "请输入 DeepSeek API Key（不显示）：", input_func=getpass)
        set_log_secrets(api_key)
        auth_code = require_credential(settings["assistant"]["auth_code_env"],
                                       "请输入助手邮箱客户端授权码（不显示）：", input_func=getpass)
        set_log_secrets(api_key, auth_code)
        mailbox = MailClient(auth_code=auth_code)
        bot = DeepSeekChat(api_key=api_key)
        MailReceiver(mailbox, bot, prompt, summary_prompt=summary_prompt, secrets=(api_key, auth_code),
                     extraction_prompt=extraction_prompt, graph_settings=settings.get("graph")).run(seconds)
    except (KeyboardInterrupt, EOFError):
        logger.info("用户中断，监听已停止。若正在发信，请核验收件箱；未处理任务已保存在本地队列。")
    except (MailboxStateError, RuntimeError, ValueError) as exc:
        logger.error("配置或运行状态无效，type=%s，监听停止；若已发信请先核验收件箱，勿盲目重试。",
                     type(exc).__name__, exc_info=True)
        raise SystemExit(1) from None
    except imaplib.IMAP4.error as exc:
        logger.error("IMAP 操作失败，type=%s，监听停止。", type(exc).__name__, exc_info=True)
        raise SystemExit(1) from None
    except (OpenAIError, OSError) as exc:
        logger.error("启动或网络错误，type=%s，监听停止。", type(exc).__name__, exc_info=True)
        raise SystemExit(1) from None
    except Exception as exc:
        logger.critical("未预期错误，type=%s，监听停止。", type(exc).__name__, exc_info=True)
        raise SystemExit(1) from None
    finally:
        logger.info("监听进程结束。")
        instance_lock.release()
        close_logging()


if __name__ == "__main__":
    main()
