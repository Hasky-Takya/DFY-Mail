"""统一收信回复与随机空闲主动来信；不要与其他 AI 发信入口并行运行。"""

import imaplib
import json
import logging
import math
import os
import random
import time
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from openai import APIConnectionError, APIStatusError, OpenAIError

from dfy_receive import MailReceiver, interval_seconds, load_reply_prompt
from dfy_test import DeepSeekChat
from dialog_memory import (
    MEMORY_PATH, PROMPT_PATH, MemoryError, ContextBudgetError, SummaryError, load_story_extraction_prompt, load_summary_prompt, new_record,
    recent_proactive_subjects, render_prompt, send_recorded, summarize, snapshot_basis,
)
from mail import DEFAULT_CONFIG, MailClient, MailboxStateError, _load_settings
from mail_logging import close_logging, set_log_secrets, start_logging, apply_logging_settings
from runtime_config import require_credential
from retry_policy import classify_chain, classify_error, retry_delay
from task_store import SingleInstanceLock


POSITIVE_PROMPT_PATH = PROMPT_PATH.with_name("positive_prompt.json")
logger = logging.getLogger("dfy.clock")
logger.addHandler(logging.NullHandler())


def idle_range(settings: dict) -> tuple[int, int]:
    low = settings.get("idle_min_minutes", 60)
    high = settings.get("idle_max_minutes", 240)
    if type(low) is not int or type(high) is not int or not 0 < low <= high:
        raise ValueError("idle_min_minutes 和 idle_max_minutes 必须是正整数，且下限不超过上限。")
    try:
        if not math.isfinite(high * 60.0):
            raise ValueError("随机等待范围过大。")
    except OverflowError:
        raise ValueError("随机等待范围过大。") from None
    return low, high


def load_clock_prompts(prompt_path=PROMPT_PATH, positive_path=POSITIVE_PROMPT_PATH) -> dict:
    with Path(prompt_path).open(encoding="utf-8-sig") as file:
        common = json.load(file)
    with Path(positive_path).open(encoding="utf-8-sig") as file:
        positive = json.load(file)
    if not isinstance(common, dict) or not isinstance(positive, dict):
        raise ValueError("提示词配置必须是 JSON 对象。")
    subject = positive.get("subject")
    if not isinstance(subject, str) or not subject.strip() or "\r" in subject or "\n" in subject:
        raise ValueError("positive_prompt.json 的 subject 必须是非空、不含换行的字符串。")
    return {
        "system_prompt": render_prompt(common.get("prompt")),
        "task": render_prompt(positive.get("prompt")),
        "subject": subject.strip(),
        "reply_prompt": load_reply_prompt(prompt_path),
        "summary_prompt": load_summary_prompt(prompt_path),
        "extraction_prompt": load_story_extraction_prompt(prompt_path),
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is not None:
            return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    return None


class IdleMailScheduler:
    """一个实例串行接收、回复、生成及发送；倒计时保存在本进程中。"""

    def __init__(self, receiver, prompts, settings, *, randint=None, now=None, monotonic=None):
        self.receiver = receiver
        self.prompts = prompts
        self.low, self.high = idle_range(settings)
        self.seconds = interval_seconds(settings)
        self.randint = randint or random.randint
        self.now = now or utc_now
        self.monotonic = monotonic or time.monotonic
        self.deadline = None
        self.last_activity = None
        self.wait_minutes = None
        self.activity_version = 0
        self.draft = None
        self.failures = {"body": 0, "summary": 0}
        self.stopped = False
        receiver.on_user_message = self.on_user_message

    def reset(self, activity: datetime, reason: str) -> None:
        now = self.now()
        # 异常未来时间按当前观察时刻处理，不能使等待无限延后。
        activity = min(activity, now)
        if self.last_activity is not None:
            activity = min(max(self.last_activity, activity), now)
        self.last_activity = activity
        self.wait_minutes = self.randint(self.low, self.high)
        remaining = max(0.0, self.wait_minutes * 60 - (now - activity).total_seconds())
        self.deadline = self.monotonic() + remaining
        if self.draft is not None and self.draft.get("task_id"):
            row = self.receiver.store.get(self.draft["task_id"])
            if row and row["status"] in ("pending", "retry"):
                self.receiver.store.finish(row["id"])
        self.draft = None
        self.failures = {"body": 0, "summary": 0}
        self.receiver.store.set_control("clock:timer", {"activity": activity.isoformat(), "wait_minutes": self.wait_minutes})
        logger.info("重置主动计时，reason=%s wait_minutes=%s remaining_seconds=%.1f。",
                    reason, self.wait_minutes, remaining)

    def on_user_message(self, message: dict) -> None:
        self.activity_version += 1
        received = parse_time(message.get("received_at")) or self.now()
        self.reset(received, "用户新来信")

    def start(self) -> None:
        if self.deadline is not None:
            raise RuntimeError("随机主动来信实例已经启动。")
        self.receiver.start()
        snapshot = self.receiver.recover_memory() or {"dialogs": []}
        activity = None if self.receiver.paused else self.receiver.mailbox.latest_user_activity(self.receiver.cursor)
        candidates = []
        if activity is not None:
            self.receiver.observe_user_activity(activity)
            received = parse_time(activity.get("received_at"))
            if received is None:
                logger.warning("最近用户来信缺少可靠接收时间，按启动时间保守计时。")
                received = self.now()
            candidates.append(received)
        for record in snapshot["dialogs"]:
            if record["status"] != "sent":
                continue
            field = "sent_at" if record["type"] == "deepseek自己主动发送" else "received_at"
            value = parse_time(record.get(field))
            if value is not None:
                candidates.append(value)
        activity_at = max(candidates) if candidates else self.now()
        saved_timer = self.receiver.store.get_control("clock:timer")
        drafts = self.receiver.store.list_tasks(account=self.receiver.account, kind="proactive")
        self.reset(activity_at, "启动历史基线")
        if saved_timer:
            saved_activity = parse_time(saved_timer.get("activity"))
            if saved_activity is not None and saved_activity >= min(activity_at, self.now()):
                self.last_activity = min(saved_activity, self.now())
                self.wait_minutes = saved_timer["wait_minutes"]
                remaining = max(0.0, self.wait_minutes * 60 - (self.now() - self.last_activity).total_seconds())
                self.deadline = self.monotonic() + remaining
                self.receiver.store.set_control("clock:timer", saved_timer)
        if drafts:
            row = drafts[0]
            self.draft = row["payload"]
            self.draft["task_id"] = row["id"]
            self.draft["version"] = self.activity_version
            self.draft["next_attempt_at"] = row["next_attempt_at"]

    def _current_draft(self, snapshot):
        if self.draft is None:
            restored = self.receiver.store.list_tasks(account=self.receiver.account, kind="proactive")
            if restored:
                row = restored[0]
                self.draft = row["payload"]
                self.draft["task_id"] = row["id"]
                self.draft["version"] = self.activity_version
                self.draft["next_attempt_at"] = row["next_attempt_at"]
        if self.draft is not None and "snapshot" not in self.draft:
            self.draft["snapshot"] = snapshot
            self.draft["version"] = self.activity_version
            self.draft["body_attempts"] = 0
            self.draft["summary_attempts"] = 0
        if (self.draft is None or self.draft["version"] != self.activity_version
                or snapshot_basis(self.draft["snapshot"]) != snapshot_basis(snapshot)):
            if self.draft is not None and self.draft.get("task_id"):
                self.receiver.store.finish(self.draft["task_id"])
            self.draft = {
                "snapshot": snapshot, "version": self.activity_version,
                "body_attempts": 0, "summary_attempts": 0,
            }
            row = self.receiver.store.create(self.receiver.account, self.draft, kind="proactive")
            self.draft["task_id"] = row["id"]
        return self.draft

    def tick(self) -> None:
        if self.stopped:
            task_id = self.draft.get("task_id") if self.draft else None
            if self.receiver.recover_memory() is None:
                self.receiver.poll_once()
                return
            self.stopped = False
            row = self.receiver.store.get(task_id) if task_id else None
            self.draft = None
            if row and row["status"] == "done":
                self.reset(self.now(), "发送后本地记忆恢复成功")
                return
            if row and row["status"] == "pending":
                self.draft = row["payload"]
                self.draft["task_id"] = row["id"]
        if self.receiver.store.get_control("service:api:paused"):
            self.receiver.poll_once()
            return
        if self.deadline is None:
            raise RuntimeError("请先启动随机计时。")
        if not self.receiver.poll_once() or self.receiver.pending:
            logger.info("本轮未完成收信同步或仍有待回复任务，不主动发信。")
            return
        if self.monotonic() < self.deadline:
            return
        if self.receiver.blocked_reason:
            return
        if self.draft is not None and self.receiver.clock() < self.draft.get("next_attempt_at", 0):
            return
        draft = self._current_draft(self.receiver.memory.snapshot(
            include_story=self.receiver.extraction_prompt is not None,
        ))
        if self.receiver.clock() < draft.get("next_attempt_at", 0):
            return
        stage = "summary" if "body" in draft else "body"
        try:
            if "body" not in draft:
                draft["body_attempts"] += 1
                logger.info("空闲到期，开始生成主动邮件，attempt=%s。", draft["body_attempts"])
                question = self.receiver.memory.question(
                    draft["snapshot"], self.prompts["task"], mode="scheduled_proactive",
                    observed_user_activity=self.receiver.observed_user_activity,
                    observation=self.receiver.observation, as_of=self.now().isoformat(),
                    story_context=draft["snapshot"].get("story_context"), system_prompt=self.prompts["system_prompt"],
                )
                body = self.receiver.bot.ask(question, system_prompt=self.prompts["system_prompt"])
                if not isinstance(body, str) or not body.strip():
                    raise ValueError("主动正文为空。")
                draft["body"] = body
                draft["network_failures"] = 0
                self.receiver.store.save(draft["task_id"], draft)
            stage = "summary"
            if "summaries" not in draft:
                draft["summary_attempts"] += 1
                draft["summaries"] = summarize(
                    self.receiver.bot, self.prompts["summary_prompt"], None, draft["body"],
                    secrets=self.receiver.memory.secrets,
                    story_context=draft["snapshot"].get("story_context"),
                    extraction_prompt=self.receiver.extraction_prompt,
                    history=[{key: item[key] for key in ("type", "received_at", "sent_at", "user_summary", "assistant_summary")}
                             for item in draft["snapshot"]["dialogs"]],
                    anchors={"as_of": self.now().isoformat(), "received_at": None},
                    validation_feedback=draft.get("validation_feedback"),
                    generate_subject=True, recent_subjects=recent_proactive_subjects(draft["snapshot"]),
                    graph_context=draft["snapshot"].get("graph_context"),
                    graph_canonical=draft["snapshot"].get("graph_canonical"),
                    graph_settings=self.receiver.memory.graph_settings,
                )
        except (OpenAIError, ValueError) as exc:
            if isinstance(exc, SummaryError):
                draft["validation_feedback"] = exc.repair_feedback()
                logger.warning("主动联合提取校验失败，code=%s field=%s。",
                               exc.details["code"], exc.details["path"])
            self.failures[stage] += 1
            attempts = self.failures[stage]
            temporary = (
                isinstance(exc, (APIConnectionError, ValueError))
                or isinstance(exc, APIStatusError)
                and (exc.status_code in {408, 409, 429} or exc.status_code >= 500)
            )
            logger.warning("主动邮件生成失败，stage=%s type=%s attempt=%s。",
                           stage, type(exc).__name__, attempts, exc_info=True)
            severity, code = classify_chain(exc)
            if severity == "transient":
                draft["network_failures"] = draft.get("network_failures", 0) + 1
                delay = retry_delay(draft["network_failures"])
                draft["next_attempt_at"] = self.receiver.clock() + delay
                self.receiver.store.save(draft["task_id"], draft, status="retry", next_attempt_at=draft["next_attempt_at"], error_code=code)
                logger.warning("主动模型等待重传，code=%s retry=%s delay_seconds=%s。", code, draft["network_failures"], delay)
                return
            if severity == "fatal":
                self.receiver.store.set_control("service:api:paused", code)
                self.receiver.store.save(draft["task_id"], draft, error_code=code)
                logger.critical("主动模型服务暂停，code=%s；任务保留。", code)
                return
            draft["invalid_failures"] = draft.get("invalid_failures", 0) + 1
            if draft["invalid_failures"] >= 3:
                self.receiver.store.quarantine(draft["task_id"], draft, error_code=code)
                logger.error("主动任务已隔离，task_id=%s code=%s。", draft["task_id"], code)
                self.draft = None
                self.reset(self.now(), "主动任务隔离后的下一周期")
            else:
                self.receiver.store.save(draft["task_id"], draft, error_code=code)
            return
        except ContextBudgetError:
            self.receiver.store.quarantine(draft["task_id"], draft, error_code="current_message_too_large")
            self.draft = None
            self.reset(self.now(), "主动任务超限已隔离")
            return

        # 模型生成可能耗时较长：复查用户活动，再持久化并发送。
        imap_ok = self.receiver.poll_once()
        row = self.receiver.store.get(draft["task_id"])
        if (self.draft is not draft or draft["version"] != self.activity_version
                or not row or row["status"] not in ("pending", "retry")):
            return
        if self.receiver.blocked_reason:
            return
        current = self.receiver.memory.snapshot()
        if not imap_ok:
            self.receiver.store.save(draft["task_id"], draft)
            return
        if (self.receiver.pending or draft["version"] != self.activity_version
                or snapshot_basis(current) != snapshot_basis(draft["snapshot"])):
            row = self.receiver.store.get(draft["task_id"])
            if row and row["status"] in ("pending", "retry"):
                self.receiver.store.finish(row["id"])
            self.draft = None
            logger.info("主动草稿已取消：用户活动、记忆或邮箱同步状态发生变化。")
            return
        if self.receiver.store.get_control("service:smtp:paused"):
            self.receiver.store.save(draft["task_id"], draft, error_code="smtp_service_paused")
            return
        subject = draft["summaries"]["mail_subject"]
        record = new_record(draft["snapshot"], None, draft["summaries"], subject=subject)
        self.stopped = True  # 任何发送异常都不得再次 tick 重发。
        logger.info("发送主动邮件，结果未确认前不重入发送。")
        self.receiver.store.save(draft["task_id"], draft, status="sending", dialog_id=record["id"])
        try:
            send_recorded(
                self.receiver.memory, draft["snapshot"], record, self.receiver.mailbox,
                subject, draft["body"],
            )
        except (MemoryError, OSError, ValueError) as exc:
            severity, code = classify_chain(exc, "smtp")
            if code in ("smtp_auth_failed", "tls_verification_failed"):
                self.receiver.store.set_control("service:smtp:paused", code)
                logger.critical("SMTP服务暂停，code=%s；发送结果待核验，不自动重发。", code)
            self.receiver.store.save(draft["task_id"], draft, status="blocked_unknown",
                                     dialog_id=record["id"], error_code="send_or_projection_unconfirmed")
            logger.critical("主动发送已暂停，code=send_or_projection_unconfirmed type=%s；不自动重发。", type(exc).__name__)
            return
        self.receiver.store.finish(draft["task_id"])
        logger.info("主动邮件已由 SMTP 接受且记忆确认，不保证最终投递。")
        self.stopped = False
        self.reset(self.now(), "主动发送成功")

    def run(self) -> None:
        failures = 0
        while self.deadline is None:
            try:
                self.start()
            except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
                severity, code = classify_chain(exc, "imap")
                if isinstance(exc, MemoryError):
                    raise
                if severity == "fatal":
                    self.receiver.store.set_control("service:imap:paused", code)
                    logger.critical("启动收信已暂停，code=%s type=%s；每256秒检查是否人工恢复。", code, type(exc).__name__)
                    while self.receiver.store.get_control("service:imap:paused"):
                        time.sleep(256)
                else:
                    failures += 1
                    delay = retry_delay(failures)
                    logger.warning("启动连接重传，code=%s retry=%s delay_seconds=%s。", code, failures, delay)
                    time.sleep(delay)
                self.receiver.cursor = None
                self.deadline = None
        logger.info("统一收信与随机主动来信已就绪，每 %g 分钟检查。", self.seconds / 60)
        logger.info("不要并行运行 dfy_receive.py 或其他 AI 发信入口。")
        next_poll = self.monotonic()
        while True:
            time.sleep(max(0.0, next_poll - self.monotonic()))
            try:
                self.tick()
            except MemoryError as exc:
                self.receiver.blocked_reason = "memory_recovery_required"
                logger.critical("写信暂停，code=memory_recovery_required type=%s；保留队列，下轮恢复本地记忆。", type(exc).__name__)
            now = self.monotonic()
            waits = [self.seconds]
            wall = self.receiver.clock()
            if self.receiver.sync_retry_at > wall:
                waits.append(self.receiver.sync_retry_at - wall)
            for task in self.receiver.pending:
                if task.get("next_attempt_at", 0) > wall:
                    waits.append(task["next_attempt_at"] - wall)
            if self.draft is not None and self.draft.get("next_attempt_at", 0) > wall:
                waits.append(self.draft["next_attempt_at"] - wall)
            next_poll = now + max(0.05, min(waits))


def main() -> None:
    start_logging("dfy_clock.py")
    logger.info("随机主动来信进程启动。")
    instance_lock = None
    try:
        instance_lock = SingleInstanceLock(MEMORY_PATH.parent / "service.lock")
        instance_lock.acquire()
        settings = _load_settings(DEFAULT_CONFIG)
        apply_logging_settings(settings.get("logging"))
        idle_range(settings)
        interval_seconds(settings)
        prompts = load_clock_prompts()
        set_log_secrets(os.getenv(settings["assistant"]["auth_code_env"], ""))
        api_key = require_credential("DEEPSEEK_API_KEY", "请输入 DeepSeek API Key（不显示）：", input_func=getpass)
        set_log_secrets(api_key)
        auth_code = require_credential(settings["assistant"]["auth_code_env"], "请输入助手邮箱客户端授权码（不显示）：", input_func=getpass)
        set_log_secrets(auth_code)
        mailbox = MailClient(config_path=DEFAULT_CONFIG, auth_code=auth_code)
        bot = DeepSeekChat(api_key=api_key)
        receiver = MailReceiver(
            mailbox, bot, prompts["reply_prompt"], summary_prompt=prompts["summary_prompt"],
            memory_path=MEMORY_PATH, secrets=(api_key, auth_code),
            extraction_prompt=prompts.get("extraction_prompt"), graph_settings=settings.get("graph"),
        )
        IdleMailScheduler(receiver, prompts, settings).run()
    except (KeyboardInterrupt, EOFError):
        logger.info("用户中断；如正在发信请核验收件箱及记忆，勿盲目重跑。")
    except Exception as exc:
        logger.critical("随机主动来信停止，type=%s；如已进入发送请先核验收件箱及记忆。",
                        type(exc).__name__, exc_info=True)
        raise SystemExit(1) from None
    finally:
        logger.info("随机主动来信进程结束。")
        if instance_lock is not None:
            instance_lock.release()
        close_logging()


if __name__ == "__main__":
    main()
