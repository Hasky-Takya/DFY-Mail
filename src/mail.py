"""单用户邮箱收发接口，不调用 DeepSeek，不自动回复或保存处理进度。"""

import argparse
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "settings.json"
logger = logging.getLogger("dfy.mail")
logger.addHandler(logging.NullHandler())


def _load_settings(config_path: str | Path | None = None) -> dict:
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG
    with path.open(encoding="utf-8-sig") as file:
        settings = json.load(file)
    if not isinstance(settings, dict):
        raise ValueError("settings.json 必须是 JSON 对象。")

    for section in ("assistant", "user"):
        if not isinstance(settings.get(section), dict):
            raise ValueError(f"缺少配置对象：{section}")
        address = settings[section].get("email")
        if not isinstance(address, str) or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
            address,
        ):
            raise ValueError(f"请在 {section}.email 填写有效的完整邮箱地址。")

    assistant = settings["assistant"]
    if assistant["email"].casefold() == settings["user"]["email"].casefold():
        raise ValueError("助手邮箱与用户邮箱不能相同。")
    for key in ("auth_code_env", "imap_host", "smtp_host"):
        value = assistant.get(key)
        if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
            raise ValueError(f"assistant.{key} 必须是非空且不含空白的字符串。")
    for key in ("imap_port", "smtp_port"):
        value = assistant.get(key)
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(f"assistant.{key} 必须是 1 到 65535 之间的整数。")
    return settings


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _body_text(message: EmailMessage) -> str:
    for content_type in ("plain", "html"):
        part = message.get_body(preferencelist=(content_type,))
        if part is None:
            continue
        try:
            text = part.get_content()
        except (LookupError, UnicodeError):
            text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if not isinstance(text, str):
            continue
        if part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        if text.strip():
            return text.strip()
    return ""


class MailboxStateError(RuntimeError):
    """邮箱 UID 状态失效，不能继续使用已有游标。"""


def _message_ids(value: str) -> list[str]:
    # 仅保留安全的 Message-ID，保留原有顺序和重复项。
    return re.findall(r"<[^<>\s@]+@[^<>\s@]+>", value)


def _fetch_email_data(parts) -> tuple[bytes | None, str | None]:
    """从同一 FETCH tuple 提取正文和服务器接收时间，不信任用户 Date。"""
    item = next((item for item in parts if isinstance(item, tuple)), None)
    if item is None:
        return None, None
    metadata, raw = item
    if not isinstance(metadata, bytes):
        return raw, None
    match = re.search(
        rb'\bINTERNALDATE\s+"([ 0-9]{2}-[A-Za-z]{3}-[0-9]{4} '
        rb'[0-9]{2}:[0-9]{2}:[0-9]{2} [+-][0-9]{4})"', metadata, re.I,
    )
    if match is not None:
        try:
            received = parsedate_to_datetime(match.group(1).decode("ascii"))
            if received.tzinfo is not None:
                return raw, received.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    return raw, None


class MailClient:
    """仅接收配置用户的邮件，并固定向该用户发信。

    From 过滤不是强身份认证；重复读取会返回相同邮件，不提供去重或自动回复。
    所有连接使用 SSL；构造实例和导入模块不会联网。
    """

    def __init__(self, config_path: str | Path | None = None, auth_code: str | None = None):
        settings = _load_settings(config_path)
        self.assistant = settings["assistant"]
        self.user_email = settings["user"]["email"]
        auth_code = auth_code or os.getenv(self.assistant["auth_code_env"])
        if not auth_code or not auth_code.strip():
            raise ValueError("请传入邮箱授权码，或设置配置中指定的授权码环境变量。")
        self._auth_code = auth_code.strip()

    def describe_error(self, exc: Exception) -> str:
        """显示排错信息，但隐藏授权码、邮箱地址和可能出现的邮件头。"""
        text = str(exc)
        secret = getattr(self, "_auth_code", "")
        if secret:
            text = text.replace(secret, "[授权码已隐藏]")
        text = re.sub(r"(?i)\b(?:LOGIN|AUTHENTICATE)\b[^\r\n]*", "认证命令已隐藏", text)
        text = re.sub(r"(?im)^(?:From|To|Subject|Authorization):.*$", "[邮件头已隐藏]", text)
        text = re.sub(r"[^\s<>@]+@[^\s<>@]+", "[邮箱已隐藏]", text)
        text = " ".join(text.split())[:500]
        stage = getattr(exc, "mail_stage", "IMAP 查询")
        return f"阶段：{stage}；详情：{text or type(exc).__name__}"

    @staticmethod
    def _imap_command(stage, action, *args, **kwargs):
        logger.debug("IMAP 阶段开始：%s", stage)
        try:
            result = action(*args, **kwargs)
            status = result[0] if isinstance(result, tuple) and result else "完成"
            logger.debug("IMAP 阶段完成：%s status=%s", stage, status)
            return result
        except (imaplib.IMAP4.error, OSError) as exc:
            if not hasattr(exc, "mail_stage"):
                exc.mail_stage = stage
            logger.debug("IMAP 阶段异常：%s type=%s", stage, type(exc).__name__)
            raise

    @contextmanager
    def _imap(self):
        connection = self._imap_command(
            "建立 SSL 连接", imaplib.IMAP4_SSL,
            self.assistant["imap_host"], self.assistant["imap_port"],
            ssl_context=ssl.create_default_context(), timeout=30,
        )
        try:
            self._imap_command("登录邮箱", connection.login, self.assistant["email"], self._auth_code)
            # imaplib 没有公开的 ID 方法；网易邮箱可能要求登录后声明客户端身份。
            if b"ID" in connection.capabilities or "ID" in connection.capabilities:
                imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
                status, _ = self._imap_command(
                    "声明 IMAP 客户端 ID", connection._simple_command,
                    "ID", '("name" "dfy-mail" "version" "1.0")'
                )
                if status != "OK":
                    raise imaplib.IMAP4.error("IMAP ID 失败。")
            status, _ = self._imap_command("只读打开 INBOX", connection.select, "INBOX", readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error("无法只读打开收件箱。")
            yield connection
        finally:
            with suppress(imaplib.IMAP4.error, OSError):
                connection.logout()
            with suppress(OSError):
                connection.shutdown()

    @contextmanager
    def _smtp(self):
        connection = smtplib.SMTP_SSL(
            self.assistant["smtp_host"], self.assistant["smtp_port"],
            context=ssl.create_default_context(), timeout=30,
        )
        try:
            connection.login(self.assistant["email"], self._auth_code)
            yield connection
        finally:
            # 不让 QUIT 失败掩盖实际收发结果；不自动重试发送。
            with suppress(smtplib.SMTPException, OSError):
                connection.quit()
            connection.close()

    def check_connection(self) -> None:
        """检查 IMAP 和 SMTP 登录，不读取正文、不发送测试邮件。"""
        with self._imap():
            pass
        with self._smtp():
            pass

    def read_user_emails(self, limit: int = 10) -> list[dict]:
        """读取 INBOX 中最近的用户来信（新到旧），包含已读邮件，不改变已读状态。

        uid 仅在当前文件夹及 uidvalidity 内有效，不应单独用于长期去重。
        正文优先纯文本；HTML 仅提取文字，忽略附件，不加载远程资源。
        """
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit 必须是 1 到 50 之间的整数。")
        result = []
        with self._imap() as connection:
            _, validity = connection.response("UIDVALIDITY")
            uidvalidity = validity[0].decode() if validity and validity[0] else ""
            status, data = connection.uid("search", None, "FROM", f'"{self.user_email}"')
            if status != "OK":
                raise imaplib.IMAP4.error("搜索用户邮件失败。")
            uids = sorted((data[0] or b"").split(), key=int, reverse=True)
            for uid in uids:
                status, parts = self._imap_command(
                    "UID FETCH 读取正文", connection.uid, "fetch", uid, "(INTERNALDATE BODY.PEEK[])"
                )
                if status != "OK":
                    raise imaplib.IMAP4.error("读取邮件失败。")
                raw, received_at = _fetch_email_data(parts)
                if raw is None:
                    continue  # 邮件可能在搜索后被其他客户端移走。
                parsed = self._parse_user_email(
                    raw, uid, uidvalidity, received_at=received_at,
                    received_at_source="imap_internaldate" if received_at is not None else None,
                )
                if parsed is None:
                    continue
                result.append(parsed)
                if len(result) == limit:
                    break
        return result

    @staticmethod
    def _sender_address(message: EmailMessage) -> tuple[str | None, str | None]:
        from_headers = message.get_all("From", [])
        if len(from_headers) != 1:
            return None, "from_header_count：From 缺失或出现多个"
        header = from_headers[0]
        if header.defects:
            # 只容忍显示名不规范：原始字段必须明确包含唯一的 <addr-spec>。
            # 不从解析器修复后的字符串或显示名里猜邮箱，不接受列表、组或注释。
            raw = next(value for key, value in message.raw_items() if key.lower() == "from")
            unfolded = re.sub(r"\r?\n[ \t]+", " ", raw)
            match = re.fullmatch(r'([^\r\n<>@,;()]*)<([^<>\s]+@[^<>\s]+)>[ \t]*', unfolded)
            if match and match.group(1).count('"') % 2 == 0:
                candidate = policy.default.header_factory("From", match.group(2))
                if not candidate.defects and len(candidate.addresses) == 1:
                    logger.debug("From 显示名格式兼容：使用唯一尖括号内地址，仍执行用户精确匹配。")
                    return candidate.addresses[0].addr_spec, None
            defects = ",".join(type(item).__name__ for item in header.defects)
            return None, f"from_header_defect：From 解析异常（{defects}），无法安全提取唯一地址"
        if len(header.addresses) != 1:
            return None, "from_address_count：From 不是唯一发件地址"
        return header.addresses[0].addr_spec, None

    def _email_filter_reason(self, message: EmailMessage, *, automatic: bool) -> str | None:
        sender, reason = self._sender_address(message)
        if reason:
            return reason
        if sender.casefold() != self.user_email.casefold():
            return "sender_mismatch：实际 From 地址与 settings.json 的 user.email 不一致"
        if automatic:
            auto = str(message.get("Auto-Submitted", "no")).split(";", 1)[0].strip().lower()
            precedence = str(message.get("Precedence", "")).strip().lower()
            if sender.casefold() == self.assistant["email"].casefold():
                return "self_mail：来自助手自身"
            if auto != "no":
                return "auto_submitted：邮件带自动生成或自动回复标记"
            if message.get("List-Id") is not None:
                return "list_id：邮件带邮件列表标记"
            if precedence in {"bulk", "list", "junk"}:
                return "precedence：邮件被标记为批量或列表邮件"
            if str(message.get("Return-Path", "")).strip() == "<>":
                return "empty_return_path：空退信地址"
            if message.get_content_type() in {"multipart/report", "message/delivery-status"}:
                return "delivery_report：投递状态报告"
        return None

    def _parse_user_email(
        self, raw: bytes, uid: bytes, uidvalidity: str, *, automatic: bool = False,
        received_at: str | None = None, received_at_source: str | None = None,
    ) -> dict | None:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        reason = self._email_filter_reason(message, automatic=automatic)
        if reason is not None:
            logger.info("跳过 UID=%s；原因=%s", uid.decode(), reason)
            return None
        sender, _ = self._sender_address(message)
        if received_at is None:
            received_at = datetime.now(timezone.utc).isoformat()
            received_at_source = "observed_at"
        in_reply_to = next(
            (value for key, value in message.raw_items() if key.lower() == "in-reply-to"),
            None,
        )
        return {
            "uid": uid.decode(),
            "uidvalidity": uidvalidity,
            "message_id": str(message.get("Message-ID", "")).strip(),
            "in_reply_to": in_reply_to,
            "references": str(message.get("References", "")),
            "received_at": received_at,
            "received_at_source": received_at_source or "imap_internaldate",
            "from": sender,
            "subject": str(message.get("Subject", "")),
            "date": str(message.get("Date", "")),
            "body": _body_text(message),
        }

    @staticmethod
    def _cursor_from_connection(connection) -> tuple[int, int]:
        values = {}
        for key in ("UIDVALIDITY", "UIDNEXT"):
            _, data = connection.response(key)
            if data and isinstance(data[0], bytes) and data[0].isdigit() and int(data[0]) > 0:
                values[key] = int(data[0])
        if len(values) == 2:
            logger.debug("游标来源：SELECT/EXAMINE 响应。")
            return values["UIDVALIDITY"], values["UIDNEXT"] - 1

        # 部分服务器的 SELECT 响应不带 UIDNEXT，主动查询同一个 INBOX。
        # 两个字段都取自同次 STATUS，避免把不同邮箱代际的状态拼在一起。
        try:
            status, data = MailClient._imap_command(
                "STATUS 查询邮箱 UID 状态", connection.status, "INBOX", "(UIDVALIDITY UIDNEXT)"
            )
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error:
            status, data = "NO", []
        payload = b" ".join(item for item in (data or []) if isinstance(item, bytes))
        match = re.search(rb"\(([^()]*)\)\s*$", payload) if status == "OK" else None
        fields = match.group(1) if match else b""
        snapshot = {}
        for key in ("UIDVALIDITY", "UIDNEXT"):
            matches = re.findall(rb"\b" + key.encode() + rb"\s+([0-9]+)(?=\s|$)", fields, re.I)
            if len(matches) == 1 and int(matches[0]) > 0:
                snapshot[key] = int(matches[0])
        if (
            "UIDVALIDITY" in values and "UIDVALIDITY" in snapshot
            and values["UIDVALIDITY"] != snapshot["UIDVALIDITY"]
        ):
            raise MailboxStateError("查询期间邮箱 UIDVALIDITY 已改变，已停止监听。")
        validity = snapshot.get("UIDVALIDITY", values.get("UIDVALIDITY"))
        if validity is None:
            raise MailboxStateError("无法取得有效的 UIDVALIDITY，不能安全监听邮箱。")
        if len(snapshot) == 2:
            logger.debug("游标来源：STATUS 响应。")
            return validity, snapshot["UIDNEXT"] - 1

        # 部分邮箱连 STATUS 也不提供 UIDNEXT；只搜索编号，不读取历史正文。
        status, data = MailClient._imap_command(
            "UID SEARCH ALL 查询编号基线", connection.uid, "search", None, "ALL"
        )
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise MailboxStateError("无法查询邮箱 UID 编号，已停止监听。")
        numbers = data[0].split()
        if any(not uid.isdigit() or int(uid) < 1 for uid in numbers):
            raise MailboxStateError("邮箱返回了无效的 UID 编号，已停止监听。")
        maximum = max((int(uid) for uid in numbers), default=0)
        logger.debug("游标来源：UID SEARCH ALL，编号数量=%s 最大UID=%s", len(numbers), maximum)
        return validity, maximum

    def get_mailbox_cursor(self) -> tuple[int, int]:
        """以 UIDNEXT 或现有最大 UID 建立基线，排除启动监听前的邮件。"""
        with self._imap() as connection:
            return self._cursor_from_connection(connection)

    def read_user_emails_since(
        self, cursor: tuple[int, int]
    ) -> tuple[tuple[int, int], list[dict]]:
        """读取游标后的全部用户新信，返回新游标与旧到新排序的邮件。

        自动邮件和退信不会返回。失败时不返回新游标，调用者应保留旧值。
        """
        with self._imap() as connection:
            current = self._cursor_from_connection(connection)
            if current[0] != cursor[0]:
                raise MailboxStateError("邮箱 UID 状态已改变，请停止并核查后重新启动监听。")
            if current[1] <= cursor[1]:
                # SEARCH ALL 的最大 UID 可因删信变小；同一 UIDVALIDITY 下不能倒退游标。
                return cursor, []
            start, end = cursor[1] + 1, current[1]
            status, data = self._imap_command(
                "UID SEARCH 查询用户新邮件", connection.uid,
                "search", None, "UID", f"{start}:{end}", "FROM", f'"{self.user_email}"'
            )
            if status != "OK":
                raise imaplib.IMAP4.error("搜索新邮件失败。")
            uids = sorted({int(uid) for uid in (data[0] or b"").split()})
            result = []
            for number in uids:
                if not start <= number <= end:
                    continue
                uid = str(number).encode()
                status, parts = self._imap_command(
                    "UID FETCH 读取正文", connection.uid, "fetch", uid, "(INTERNALDATE BODY.PEEK[])"
                )
                if status != "OK":
                    raise imaplib.IMAP4.error("读取新邮件失败。")
                raw, received_at = _fetch_email_data(parts)
                if raw is None:
                    continue
                parsed = self._parse_user_email(
                    raw, uid, str(current[0]), automatic=True, received_at=received_at,
                    received_at_source="imap_internaldate" if received_at is not None else None,
                )
                if parsed is not None:
                    result.append(parsed)
            logger.debug("增量读取完成：范围=%s:%s 候选=%s 用户邮件=%s", start, end, len(uids), len(result))
            return current, result

    def diagnose_email(self, uid: int) -> dict:
        """只读检查指定 UID 的过滤结果，不读取正文、不调用模型、不发信。"""
        if type(uid) is not int or uid <= 0:
            raise ValueError("uid 必须是正整数。")
        with self._imap() as connection:
            status, parts = self._imap_command(
                "诊断指定 UID 的邮件头", connection.uid, "fetch", str(uid),
                "(BODY.PEEK[HEADER.FIELDS (FROM AUTO-SUBMITTED LIST-ID PRECEDENCE RETURN-PATH CONTENT-TYPE)])",
            )
            if status != "OK":
                raise imaplib.IMAP4.error("读取诊断邮件头失败。")
            raw = next((item[1] for item in parts if isinstance(item, tuple)), None)
            if raw is None:
                raise ValueError("该 UID 不在当前 INBOX 中，可能已移动或删除。")
            message = BytesParser(policy=policy.default).parsebytes(raw)
            reason = self._email_filter_reason(message, automatic=True)
            return {
                "uid": uid,
                "reply_allowed": reason is None,
                "reason": reason or "符合用户来信过滤规则",
                "note": "仅检查邮件头，未读取正文或发送回复；此操作不会改变监听基线。",
            }

    def send_email(
        self, subject: str, body: str, *, in_reply_to: str | None = None,
        references: str | None = None, auto_replied: bool = False,
        message_id: str | None = None,
    ) -> str:
        """向配置用户发送纯文本邮件，返回 Message-ID；成功仅表示 SMTP 接受。"""
        if not subject.strip() or "\r" in subject or "\n" in subject:
            raise ValueError("邮件主题不能为空或包含换行。")
        if not body.strip():
            raise ValueError("邮件正文不能为空。")
        if message_id is not None:
            atom = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
            if not isinstance(message_id, str) or re.fullmatch(
                rf"<{atom}(?:\.{atom})*@{atom}(?:\.{atom})*>", message_id
            ) is None:
                raise ValueError("message_id 必须是单个安全的 Message-ID，且不能包含换行。")
        else:
            message_id = make_msgid(domain=self.assistant["email"].split("@")[1])
        message = EmailMessage()
        message["From"] = self.assistant["email"]
        message["To"] = self.user_email
        message["Subject"] = subject.strip()
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = message_id
        parents = _message_ids(in_reply_to or "")
        if parents:
            message["In-Reply-To"] = parents[-1]
        chain = _message_ids(references or "")
        if parents and parents[-1] not in chain:
            chain.append(parents[-1])
        if chain:
            message["References"] = " ".join(chain[-20:])
        if auto_replied:
            message["Auto-Submitted"] = "auto-replied"
        message.set_content(body, charset="utf-8")
        with self._smtp() as connection:
            connection.send_message(
                message, from_addr=self.assistant["email"], to_addrs=[self.user_email]
            )
        return str(message["Message-ID"])


def main() -> None:
    parser = argparse.ArgumentParser(description="126 邮箱收发接口；默认只检查连接。")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("check", help="检查收发连接，不发信")
    diagnose = commands.add_parser("diagnose", help="只读诊断指定 UID 为什么被过滤，不读正文、不发信")
    diagnose.add_argument("--uid", type=int, required=True)
    read = commands.add_parser("read", help="读取最近的用户来信，包含已读邮件")
    read.add_argument("--limit", type=int, default=10)
    send = commands.add_parser("send", help="向配置用户发送邮件")
    send.add_argument("--subject", required=True)
    send.add_argument("--body", required=True)
    args = parser.parse_args()

    try:
        settings = _load_settings(args.config)
        auth_code = os.getenv(settings["assistant"]["auth_code_env"]) or getpass(
            "请输入助手邮箱客户端授权码（不显示）："
        )
        client = MailClient(args.config, auth_code=auth_code)
        if args.command == "diagnose":
            print(json.dumps(client.diagnose_email(args.uid), ensure_ascii=False, indent=2))
        elif args.command == "read":
            messages = client.read_user_emails(args.limit)
            print(json.dumps(messages, ensure_ascii=False, indent=2))
        elif args.command == "send":
            message_id = client.send_email(args.subject, args.body)
            print(f"SMTP 已接受邮件（不保证最终投递），Message-ID：{message_id}")
        else:
            client.check_connection()
            print("IMAP 和 SMTP 连接检查通过，未发送邮件。")
    except ValueError as exc:
        parser.exit(1, f"配置或输入错误：{exc}\n")
    except smtplib.SMTPAuthenticationError:
        parser.exit(1, "SMTP 登录失败，请检查助手邮箱、授权码及 SMTP 服务开关。\n")
    except imaplib.IMAP4.error:
        parser.exit(1, "IMAP 操作失败，请检查授权码、IMAP 服务及客户端访问限制。\n")
    except (smtplib.SMTPException, OSError) as exc:
        parser.exit(1, f"邮箱操作失败（{type(exc).__name__}），请检查配置或网络；发送失败时勿盲目重试。\n")
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")


if __name__ == "__main__":
    main()
