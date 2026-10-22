"""TLS-only SMTP/IMAP adapters. Network use is only from explicit CLI actions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid, parsedate_to_datetime
import imaplib
import re
import smtplib
import ssl

from .config import Settings, require_mail_config
from .parser import sender_authentication

FEDEX_QUERY_ADDRESS = "track@fedex.com"
MAX_TRACKING_NUMBERS = 30
MAX_IMAP_MESSAGES_PER_FETCH = 50
MAX_RAW_EMAIL_BYTES = 1024 * 1024
MAX_EMAIL_HEADER_BYTES = 64 * 1024


class DeliveryFailed(RuntimeError):
    """SMTP explicitly refused the message or failed before DATA was attempted."""


class DeliveryUncertain(RuntimeError):
    """Connection ended during message submission; never automatically resend."""


class MailAuthenticationFailed(DeliveryFailed):
    """SMTP credentials were explicitly rejected; pause sending until re-authorization."""


def build_query_message(mail_user: str, tracking_numbers: list[str]) -> EmailMessage:
    if not 1 <= len(tracking_numbers) <= MAX_TRACKING_NUMBERS:
        raise ValueError(f"每封邮件须包含 1–{MAX_TRACKING_NUMBERS} 个跟踪号")
    if len({number.casefold() for number in tracking_numbers}) != len(tracking_numbers):
        raise ValueError("同一封查询邮件不可重复跟踪号")
    message = EmailMessage()
    message["From"] = mail_user
    message["To"] = FEDEX_QUERY_ADDRESS
    # Intentionally omit Subject: FedEx's email query format requires it to be blank.
    message.set_content("\n".join(tracking_numbers), subtype="plain", charset="utf-8")
    message["Message-ID"] = make_msgid()
    return message


def _smtp_client(settings: Settings):
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
    client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    client.starttls(context=context)
    return client


def test_smtp_auth(settings: Settings) -> None:
    require_mail_config(settings, smtp=True)
    client = None
    try:
        client = _smtp_client(settings)
        client.login(settings.mail_user, settings.mail_app_password)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError, smtplib.SMTPHeloError,
            smtplib.SMTPNotSupportedError, OSError) as exc:
        raise DeliveryFailed(f"SMTP 连接/认证失败：{type(exc).__name__}") from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass


def send_query_email(settings: Settings, message: EmailMessage) -> str:
    require_mail_config(settings, smtp=True)
    client = None
    try:
        client = _smtp_client(settings)
        client.login(settings.mail_user, settings.mail_app_password)
    except Exception as exc:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            raise MailAuthenticationFailed("SMTP 凭据被拒绝") from exc
        raise DeliveryFailed(f"SMTP 连接/认证失败：{type(exc).__name__}") from exc
    try:
        refused = client.send_message(message)
        if refused:
            raise DeliveryFailed("SMTP 明确拒绝收件人")
        return str(message["Message-ID"] or "")
    except DeliveryFailed:
        raise
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        raise DeliveryFailed(f"SMTP 明确拒绝：{type(exc).__name__}") from exc
    except Exception as exc:
        raise DeliveryUncertain(f"SMTP 提交结果不确定：{type(exc).__name__}") from exc
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass


@contextmanager
def _imap_client(settings: Settings):
    require_mail_config(settings, imap=True)
    client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(settings.mail_user, settings.mail_app_password)
        yield client
    except imaplib.IMAP4.error as exc:
        raise DeliveryFailed(f"IMAP 登录/协议失败：{type(exc).__name__}") from exc
    finally:
        try:
            client.logout()
        except Exception:
            pass


def test_imap_auth(settings: Settings) -> None:
    with _imap_client(settings) as client:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise DeliveryFailed("IMAP 登录成功，但无法只读打开 INBOX")


def send_operator_alert(settings: Settings, message: EmailMessage) -> str:
    """Send a separately addressed operator alert using the configured TLS SMTP account."""
    return send_query_email(settings, message)


def _imap_uidvalidity(client) -> str:
    validity_data = client.response("UIDVALIDITY")[1]
    if not validity_data or not validity_data[0]:
        raise DeliveryFailed("IMAP 服务端未提供 UIDVALIDITY")
    value = validity_data[0].decode("ascii", "replace") if isinstance(validity_data[0], bytes) else str(validity_data[0])
    if not value.isdigit():
        raise DeliveryFailed("IMAP UIDVALIDITY 格式无效")
    return value


def _metadata_for_uid(client, uid: bytes | str) -> tuple[bytes, datetime, int | None]:
    status, payload = client.uid("FETCH", uid, "(INTERNALDATE RFC822.SIZE)")
    if status != "OK":
        raise DeliveryFailed("IMAP 无法读取邮件大小/到达时间")
    metadata = None
    for item in payload or []:
        candidate = item[0] if isinstance(item, tuple) and item else item
        if isinstance(candidate, bytes) and (b"INTERNALDATE" in candidate or b"RFC822.SIZE" in candidate):
            metadata = candidate
            break
    if metadata is None:
        raise DeliveryFailed("IMAP 未返回邮件大小/到达时间元数据")
    received_at = datetime.now(timezone.utc)
    date_match = re.search(rb'INTERNALDATE\s+"([^"]+)"', metadata, re.I)
    if date_match:
        try:
            parsed = parsedate_to_datetime(date_match.group(1).decode("ascii", "replace"))
            if parsed.tzinfo is not None:
                received_at = parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", metadata, re.I)
    size = int(size_match.group(1)) if size_match else None
    return metadata, received_at, size


def _header_fields_for_uid(client, uid: bytes | str) -> bytes:
    status, payload = client.uid(
        "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM AUTHENTICATION-RESULTS MESSAGE-ID)])")
    if status != "OK":
        raise DeliveryFailed("IMAP 无法只读读取邮件认证头")
    part = next((item for item in payload or [] if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
    if part is None:
        return b""
    return part[1]


def _full_message_for_uid(client, uid: bytes | str) -> bytes:
    status, payload = client.uid("FETCH", uid, "(BODY.PEEK[])")
    if status != "OK":
        raise DeliveryFailed("IMAP 无法只读读取邮件正文")
    raw = next((part[1] for part in payload if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
    if raw is None:
        raise DeliveryFailed("IMAP 邮件正文为空")
    if len(raw) > MAX_RAW_EMAIL_BYTES:
        raise DeliveryFailed("邮件超过 1 MiB 安全上限；未解析正文")
    return raw


def fetch_new_messages(settings: Settings, checkpoint: dict | None = None) -> list[dict]:
    """Incrementally fetch at most 50 messages; inspect bounded headers before any body fetch."""
    messages: list[dict] = []
    with _imap_client(settings) as client:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise DeliveryFailed("IMAP 无法只读打开 INBOX")
        uidvalidity = _imap_uidvalidity(client)
        try:
            last_uid = int(checkpoint["last_uid"]) if checkpoint and checkpoint.get("uidvalidity") == uidvalidity else 0
        except (TypeError, ValueError):
            last_uid = 0
        uid_range = f"{last_uid + 1}:*"
        status, all_data = client.uid("SEARCH", None, "UID", uid_range)
        if status != "OK":
            raise DeliveryFailed("IMAP UID 增量搜索失败")
        all_uids = (all_data[0] or b"").split() if all_data else []
        all_uids.sort(key=lambda value: int(value))
        if not all_uids:
            return []
        high_watermark = int(all_uids[-1])

        # FROM is only a server-side privacy/performance prefilter. Every fetched header
        # is still checked for FedEx domain and aligned Gmail auth claims before any body fetch.
        status, fedex_data = client.uid("SEARCH", None, "UID", uid_range, "FROM", "fedex.com")
        if status != "OK":
            raise DeliveryFailed("IMAP FedEx 发件人候选搜索失败")
        candidate_uids = (fedex_data[0] or b"").split() if fedex_data else []
        candidate_uids.sort(key=lambda value: int(value))
        if not candidate_uids:
            return [{"uidvalidity": uidvalidity, "uid": str(high_watermark),
                     "checkpoint_only": True, "raw": b"", "received_at": datetime.now(timezone.utc)}]
        has_more_candidates = len(candidate_uids) > MAX_IMAP_MESSAGES_PER_FETCH
        uids = candidate_uids[:MAX_IMAP_MESSAGES_PER_FETCH]
        checkpoint_after_batch = int(uids[-1]) if has_more_candidates else high_watermark
        for uid in uids:
            uid_text = uid.decode("ascii", "replace") if isinstance(uid, bytes) else str(uid)
            try:
                metadata, received_at, size = _metadata_for_uid(client, uid)
            except DeliveryFailed:
                # Do not return later UIDs: the persistent checkpoint must remain contiguous.
                raise
            base = {"uidvalidity": uidvalidity, "uid": uid_text, "received_at": received_at,
                    "checkpoint_after_batch": str(checkpoint_after_batch)}
            if size is None:
                messages.append({**base, "raw": b"", "oversize": True,
                                 "oversize_reason": "size_unavailable", "reported_size": None})
                continue
            if size > MAX_RAW_EMAIL_BYTES:
                messages.append({**base, "raw": b"", "oversize": True,
                                 "oversize_reason": "message_too_large", "reported_size": size})
                continue
            try:
                header_bytes = _header_fields_for_uid(client, uid)
            except DeliveryFailed:
                # Do not return later UIDs: the persistent checkpoint must remain contiguous.
                raise
            if len(header_bytes) > MAX_EMAIL_HEADER_BYTES:
                messages.append({**base, "raw": header_bytes[:MAX_EMAIL_HEADER_BYTES],
                                 "oversize": True, "oversize_reason": "header_too_large", "reported_size": size})
                continue

            header_message = BytesParser(policy=policy.default).parsebytes(header_bytes)
            verified, _, _ = sender_authentication(header_message)
            if not verified:
                # Retain only bounded headers for the unverified summary; do not download body/attachments.
                messages.append({**base, "raw": header_bytes, "headers_only": True})
                continue
            try:
                raw = _full_message_for_uid(client, uid)
            except DeliveryFailed:
                # Do not return later UIDs: the persistent checkpoint must remain contiguous.
                raise
            messages.append({**base, "raw": raw})
    return messages


def fetch_message_uid(settings: Settings, uidvalidity: str, uid: str) -> dict:
    """Explicitly re-fetch one message for operator-requested revalidation."""
    if not str(uidvalidity).isdigit() or not str(uid).isdigit():
        raise ValueError("邮件 UID/UIDVALIDITY 无效")
    with _imap_client(settings) as client:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise DeliveryFailed("IMAP 无法只读打开 INBOX")
        current_validity = _imap_uidvalidity(client)
        if current_validity != str(uidvalidity):
            raise DeliveryFailed("邮箱 UIDVALIDITY 已变化，拒绝用新 UID 误取另一封邮件")
        _, received_at, size = _metadata_for_uid(client, str(uid))
        if size is None or size > MAX_RAW_EMAIL_BYTES:
            raise DeliveryFailed("邮件正文超过安全上限/无法确认大小")
        header_bytes = _header_fields_for_uid(client, str(uid))
        if len(header_bytes) > MAX_EMAIL_HEADER_BYTES:
            raise DeliveryFailed("邮件认证头超过 64 KiB 安全上限")
        raw = _full_message_for_uid(client, str(uid))
        return {"uidvalidity": current_validity, "uid": str(uid), "received_at": received_at, "raw": raw}
