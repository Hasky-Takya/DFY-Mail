"""统一收信回复与随机空闲主动来信；不要与其他 AI 发信入口并行运行。"""

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
    MEMORY_PATH, PROMPT_PATH, MemoryError, load_story_extraction_prompt, load_summary_prompt, new_record,
    render_prompt, send_recorded, summarize,
)
from mail import DEFAULT_CONFIG, MailClient, _load_settings
from mail_logging import close_logging, set_log_secrets, start_logging


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
        self.draft = None
        self.failures = {"body": 0, "summary": 0}
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
        snapshot = self.receiver.memory.snapshot()
        activity = self.receiver.mailbox.latest_user_activity(self.receiver.cursor)
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
        self.reset(max(candidates) if candidates else self.now(), "启动历史基线")

    def _current_draft(self, snapshot):
        if (self.draft is None or self.draft["version"] != self.activity_version
                or self.draft["snapshot"]["revision"] != snapshot["revision"]):
            self.draft = {
                "snapshot": snapshot, "version": self.activity_version,
                "body_attempts": 0, "summary_attempts": 0,
            }
        return self.draft

    def tick(self) -> None:
        if self.stopped:
            raise MemoryError("主动发送已停止，请先核验发送状态，禁止重复进入发送。")
        if self.deadline is None:
            raise RuntimeError("请先启动随机计时。")
        if not self.receiver.poll_once() or self.receiver.pending:
            self.draft = None
            logger.info("本轮未完成收信同步或仍有待回复任务，不主动发信。")
            return
        if self.monotonic() < self.deadline:
            return
        draft = self._current_draft(self.receiver.memory.snapshot(
            include_story=self.receiver.extraction_prompt is not None,
        ))
        stage = "summary" if "body" in draft else "body"
        try:
            if "body" not in draft:
                draft["body_attempts"] += 1
                logger.info("空闲到期，开始生成主动邮件，attempt=%s。", draft["body_attempts"])
                question = self.receiver.memory.question(
                    draft["snapshot"], self.prompts["task"], mode="scheduled_proactive",
                    observed_user_activity=self.receiver.observed_user_activity,
                    observation=self.receiver.observation, as_of=self.now().isoformat(),
                    story_context=draft["snapshot"].get("story_context"),
                )
                body = self.receiver.bot.ask(question, system_prompt=self.prompts["system_prompt"])
                if not isinstance(body, str) or not body.strip():
                    raise ValueError("主动正文为空。")
                draft["body"] = body
            stage = "summary"
            draft["summary_attempts"] += 1
            draft["summaries"] = summarize(
                self.receiver.bot, self.prompts["summary_prompt"], None, draft["body"],
                secrets=self.receiver.memory.secrets,
                story_context=draft["snapshot"].get("story_context"),
                extraction_prompt=self.receiver.extraction_prompt,
                history=[{key: item[key] for key in ("type", "received_at", "sent_at", "user_summary", "assistant_summary")}
                         for item in draft["snapshot"]["dialogs"]],
                anchors={"as_of": self.now().isoformat(), "received_at": None},
            )
        except (OpenAIError, ValueError) as exc:
            self.failures[stage] += 1
            attempts = self.failures[stage]
            temporary = (
                isinstance(exc, (APIConnectionError, ValueError))
                or isinstance(exc, APIStatusError)
                and (exc.status_code in {408, 409, 429} or exc.status_code >= 500)
            )
            logger.warning("主动邮件生成失败，stage=%s type=%s attempt=%s。",
                           stage, type(exc).__name__, attempts, exc_info=True)
            if not temporary or attempts >= 3:
                self.stopped = True
                raise RuntimeError("主动生成失败，已达到重试上限或发生永久错误。") from None
            return

        # 模型生成可能耗时较长：复查用户活动，再持久化并发送。
        imap_ok = self.receiver.poll_once()
        current = self.receiver.memory.snapshot()
        if (not imap_ok or self.receiver.pending or draft["version"] != self.activity_version
                or current["revision"] != draft["snapshot"]["revision"]):
            self.draft = None
            logger.info("主动草稿已取消：用户活动、记忆或邮箱同步状态发生变化。")
            return
        record = new_record(draft["snapshot"], None, draft["summaries"])
        self.stopped = True  # 任何发送异常都不得再次 tick 重发。
        logger.info("发送主动邮件，结果未确认前不重入发送。")
        send_recorded(
            self.receiver.memory, draft["snapshot"], record, self.receiver.mailbox,
            self.prompts["subject"], draft["body"],
        )
        logger.info("主动邮件已由 SMTP 接受且记忆确认，不保证最终投递。")
        self.stopped = False
        self.reset(self.now(), "主动发送成功")

    def run(self) -> None:
        self.start()
        logger.info("统一收信与随机主动来信已就绪，每 %g 分钟检查。", self.seconds / 60)
        logger.info("不要并行运行 dfy_receive.py 或其他 AI 发信入口。")
        next_poll = self.monotonic()
        while True:
            time.sleep(max(0.0, next_poll - self.monotonic()))
            self.tick()
            now = self.monotonic()
            next_poll += (max(0, int((now - next_poll) // self.seconds)) + 1) * self.seconds


def main() -> None:
    start_logging("dfy_clock.py")
    logger.info("随机主动来信进程启动。")
    try:
        settings = _load_settings(DEFAULT_CONFIG)
        idle_range(settings)
        interval_seconds(settings)
        prompts = load_clock_prompts()
        set_log_secrets(os.getenv(settings["assistant"]["auth_code_env"], ""))
        api_key = os.getenv("DEEPSEEK_API_KEY") or getpass("请输入 DeepSeek API Key（不显示）：")
        set_log_secrets(api_key)
        auth_code = os.getenv(settings["assistant"]["auth_code_env"]) or getpass("请输入助手邮箱客户端授权码（不显示）：")
        set_log_secrets(auth_code)
        mailbox = MailClient(config_path=DEFAULT_CONFIG, auth_code=auth_code)
        bot = DeepSeekChat(api_key=api_key)
        receiver = MailReceiver(
            mailbox, bot, prompts["reply_prompt"], summary_prompt=prompts["summary_prompt"],
            memory_path=MEMORY_PATH, secrets=(api_key, auth_code),
            extraction_prompt=prompts.get("extraction_prompt"),
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
        close_logging()


if __name__ == "__main__":
    main()
