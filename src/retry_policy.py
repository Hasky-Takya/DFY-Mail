"""不阻塞收信的退避时间与安全故障分类。"""

import imaplib
import smtplib
import ssl
from openai import APIConnectionError, APIStatusError


def retry_delay(retry_number):
    """第1次重传等待2秒，第8次及以后256秒。"""
    return 2 ** min(max(1, int(retry_number)), 8)


def classify_error(exc, service="api"):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "fatal", "tls_verification_failed"
    if isinstance(exc, APIConnectionError):
        return "transient", "api_connection_failed"
    if isinstance(exc, APIStatusError):
        code = getattr(exc, "status_code", 0)
        if code in (408, 409, 429) or code >= 500:
            return "transient", "api_temporary_failure"
        if code in (401, 402, 403, 404):
            return "fatal", "api_auth_access_failed"
        return "invalid", "api_request_rejected"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "fatal", "smtp_auth_failed"
    if isinstance(exc, imaplib.IMAP4.abort):
        return "transient", "imap_connection_failed"
    if isinstance(exc, imaplib.IMAP4.error):
        text = str(exc).lower()  # 分类使用原文，但永不记录该文本。
        if any(token in text for token in ("authentication", "authenticate", "login", "password", "authorization", "auth failed")):
            return "fatal", "imap_auth_failed"
        return "transient", "imap_temporary_failure"
    if isinstance(exc, OSError):
        return "transient", service + "_connection_failed"
    if isinstance(exc, ValueError):
        if type(exc).__name__ == "ResponseProtocolError":
            return "invalid", "api_response_protocol_invalid"
        return "invalid", "model_output_invalid"
    return "fatal", service + "_unexpected_failure"


def classify_chain(exc, service="api"):
    """穿透已包装异常，只返回固定安全代码，不泄露底层错误消息。"""
    seen = set()
    current = exc
    result = classify_error(exc, service)
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidate = classify_error(current, service)
        if candidate[1] in ("tls_verification_failed", "smtp_auth_failed", "imap_auth_failed", "api_auth_access_failed"):
            return candidate
        if candidate[0] == "transient":
            result = candidate
        current = current.__cause__ or current.__context__
    return result
