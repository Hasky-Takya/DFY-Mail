"""监听就绪后收到的用户邮件，交给 DeepSeek 回答；仅保留本次进程内状态。"""

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
    DialogMemory, MEMORY_PATH, MemoryError, SummaryError, load_story_extraction_prompt, load_summary_prompt, new_record,
    now_iso, render_email_prompt, resolve_reply, send_recorded, summarize,
)
from mail import MailboxStateError, MailClient, _load_settings
from mail_logging import close_logging, set_log_secrets, start_logging


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

    启动重新建立基线，不补发旧信或停机期间的信。来信正文会传给 DeepSeek。
    待处理队列不落盘；梗概与发送状态持久化，未完成邮件仍需人工核验。
    """

    def __init__(
        self, mailbox: MailClient, bot: DeepSeekChat, reply_prompt: str, *,
        memory_path: str | Path = MEMORY_PATH, summary_prompt: str | None = None, secrets=(),
        on_user_message=None, extraction_prompt: str | None = None,
    ):
        self.on_user_message = on_user_message
        self.extraction_prompt = extraction_prompt
        self.mailbox = mailbox
        self.bot = bot
        self.reply_prompt = reply_prompt
        self.memory = DialogMemory(memory_path, secrets=secrets)
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

    def start(self) -> None:
        if self.cursor is not None:
            raise RuntimeError("该监听实例已经启动。")
        self.memory.snapshot(include_story=self.extraction_prompt is not None)  # 先恢复一致记忆。
        logger.info("正在建立监听基线，不处理旧邮件。")
        self.cursor = self.mailbox.get_mailbox_cursor()
        # 基线建立过程可能略过同期来信，观测区间从建立完成后算起。
        self.observation.update(started_at=now_iso(), baseline=list(self.cursor))
        logger.info("基线建立完成：UIDVALIDITY=%s UID=%s", *self.cursor)

    def poll_once(self) -> bool:
        """返回本轮 IMAP 是否同步成功；API 临时失败仍保留队首。"""
        if self.stopped:
            raise MemoryError("监听已在发信阶段停止，禁止再次处理队列；请先核验发送与记忆。")
        if self.cursor is None:
            raise RuntimeError("请先启动监听并建立基线。")
        logger.info("正在检查 INBOX 新邮件，当前 UID=%s pending=%s", self.cursor[1], len(self.pending))
        imap_ok = True
        sync_started_at = now_iso()
        self.observation["sync_ok"] = False
        try:
            cursor, messages = self.mailbox.read_user_emails_since(self.cursor)
            self.observation.update(
                sync_ok=True, last_successful_sync=now_iso(), coverage_through=sync_started_at,
            )
        except (imaplib.IMAP4.abort, OSError) as exc:
            imap_ok = False
            logger.warning(
                "收信暂时中断，type=%s；游标及队列保留，下一周期重试。",
                type(exc).__name__, exc_info=True,
            )
            if not self.pending:
                return False
            cursor, messages = self.cursor, []
            logger.info("本轮无法获取新增邮件，继续处理已经完整入队的任务。")
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(f"IMAP 操作失败；{self.mailbox.describe_error(exc)}") from None

        if messages:
            logger.info("检查完成，发现 %s 封符合条件的用户来信。", len(messages))
        else:
            logger.info("检查完成，未发现可回复的用户新邮件，扫描到 UID=%s。", cursor[1])
        for message in messages:
            uid = (message["uidvalidity"], message["uid"])
            identifier = message["message_id"].strip()
            if uid in self.seen_uids or (identifier and identifier in self.seen_ids):
                logger.debug("跳过重复邮件，UID=%s", message["uid"])
                continue
            # 去重同时覆盖已经入队和已经回复的邮件。
            self.seen_uids.add(uid)
            if identifier:
                self.seen_ids.add(identifier)
            message.setdefault("received_at", now_iso())
            message.setdefault("received_at_source", "observed_at")
            self.observe_user_activity(message)
            if not message["body"].strip():
                logger.info("跳过空正文邮件，UID=%s", message["uid"])
                continue
            self.pending.append({"message": message, "attempts": 0})
            if self.on_user_message is not None:
                self.on_user_message(message)
        self.cursor = cursor
        self.prune_observed_activity()

        while self.pending:
            task = self.pending[0]
            message = task["message"]
            if "snapshot" not in task:
                task["snapshot"] = self.memory.snapshot(include_story=self.extraction_prompt is not None)
            question = self.memory.question(task["snapshot"], (
                f"邮件主题：{message['subject']}\n\n用户邮件正文：\n{message['body']}"
            ), mode="reply", current_message=message,
                observed_user_activity=self.observed_user_activity,
                observation=self.observation, as_of=now_iso(),
                story_context=task["snapshot"].get("story_context"))
            logger.info(
                "正在为 UID=%s 调用 DeepSeek，attempt=%s body_chars=%s",
                message["uid"], task["attempts"] + 1, len(message["body"]),
            )
            started = time.perf_counter()
            try:
                if "body" not in task:
                    task["body"] = self.bot.ask(question, system_prompt=self.reply_prompt)
                    logger.info("DeepSeek 生成完成，UID=%s duration=%.2fs", message["uid"], time.perf_counter() - started)
                    task["attempts"] = 0
                if "summaries" not in task:
                    logger.info("正在提炼双方梗概，UID=%s", message["uid"])
                    task["summaries"] = summarize(
                        self.bot, self.summary_prompt, message["body"], task["body"], secrets=self.memory.secrets,
                        story_context=task["snapshot"].get("story_context"), extraction_prompt=self.extraction_prompt,
                        history=[{key: item[key] for key in ("type", "received_at", "sent_at", "user_summary", "assistant_summary")}
                                 for item in task["snapshot"]["dialogs"]],
                        anchors={"as_of": now_iso(), "received_at": message["received_at"],
                                 "received_at_source": message["received_at_source"]},
                        validation_feedback=task.get("validation_feedback"),
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
                temporary = (
                    isinstance(exc, (APIConnectionError, ValueError))
                    or isinstance(exc, APIStatusError)
                    and (exc.status_code in {408, 409, 429} or exc.status_code >= 500)
                )
                if not temporary or task["attempts"] >= 3:
                    raise RuntimeError(
                        f"DeepSeek 调用停止（{type(exc).__name__}），UID：{message['uid']}。"
                        "请检查密钥、额度或网络；待处理邮件仅保存在本进程内。"
                    ) from None
                logger.warning("DeepSeek 暂时失败，保留 UID=%s，下轮重试（%s/3）。", message["uid"], task["attempts"])
                return imap_ok

            logger.info("开始保存梗概并发送回复，原信 UID=%s", message["uid"])
            # 从此处起任何中断都不得重新进入 SMTP；磁盘上保留可核验状态。
            self.stopped = True
            try:
                record = new_record(task["snapshot"], message, task["summaries"])
                send_recorded(
                    self.memory, task["snapshot"], record, self.mailbox,
                    reply_subject(message["subject"]), body,
                    in_reply_to=message["message_id"], references=message.get("references", ""),
                    auto_replied=True,
                )
            except (smtplib.SMTPException, OSError, ValueError) as exc:
                logger.error(
                    "发信步骤异常，UID=%s type=%s smtp_code=%s；结果可能不确定，停止监听。",
                    message["uid"], type(exc).__name__, getattr(exc, "smtp_code", None), exc_info=True,
                )
                raise RuntimeError(
                    f"发信步骤异常（{type(exc).__name__}），UID：{message['uid']}。"
                    "监听已停止；发送结果可能不确定，请核实是否收到回复，勿盲目重发。"
                ) from None
            self.pending.popleft()
            self.prune_observed_activity()
            self.stopped = False
            logger.info("SMTP 已接受回复且记忆已确认，原信 UID=%s（不保证最终投递）。", message["uid"])
        return imap_ok

    def run(self, seconds: float) -> None:
        self.start()
        logger.info("监听已就绪，每 %g 分钟检查一次；只处理基线之后的用户新邮件。", seconds / 60)
        logger.info("按 Ctrl+C 停止。不要同时运行多个监听实例；重启不补回停机期间的邮件。")
        deadline = time.monotonic() + seconds
        while True:
            time.sleep(max(0, deadline - time.monotonic()))
            self.poll_once()
            now = time.monotonic()
            # 处理过久时跳过已错过的检查点，不并发、不密集补查。
            deadline += (max(0, int((now - deadline) // seconds)) + 1) * seconds


def main() -> None:
    log_path = start_logging("dfy_receive.py")
    mailbox = None
    try:
        logger.info("监听进程启动，日志文件：%s", log_path)
        settings = _load_settings()
        set_log_secrets(os.getenv(settings["assistant"]["auth_code_env"], ""))
        logger.info("邮箱配置加载完成。")
        seconds = interval_seconds(settings)
        prompt = load_reply_prompt()
        summary_prompt = load_summary_prompt()
        extraction_prompt = load_story_extraction_prompt()
        api_key = os.getenv("DEEPSEEK_API_KEY") or getpass("请输入 DeepSeek API Key（不显示）：")
        set_log_secrets(api_key)
        auth_code = os.getenv(settings["assistant"]["auth_code_env"]) or getpass(
            "请输入助手邮箱客户端授权码（不显示）："
        )
        set_log_secrets(api_key, auth_code)
        mailbox = MailClient(auth_code=auth_code)
        bot = DeepSeekChat(api_key=api_key)
        MailReceiver(mailbox, bot, prompt, summary_prompt=summary_prompt, secrets=(api_key, auth_code),
                     extraction_prompt=extraction_prompt).run(seconds)
    except (KeyboardInterrupt, EOFError):
        logger.info("用户中断，监听已停止。若正在发信，请核验收件箱；未处理队列不保留。")
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
        close_logging()


if __name__ == "__main__":
    main()
