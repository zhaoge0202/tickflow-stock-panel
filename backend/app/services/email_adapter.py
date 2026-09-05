"""SMTP email notification adapter.

Transport failures are isolated from alert persistence and SSE delivery. SMTP credentials
are supplied by the caller from ``secrets_store`` and never logged here.
"""
from __future__ import annotations

import logging
import smtplib
import time
from contextlib import suppress
from email.message import EmailMessage
from email.utils import parseaddr

logger = logging.getLogger(__name__)

SECURITY_MODES = {"ssl", "starttls", "none"}
_MAX_ATTEMPTS = 2


def is_valid_email(address: str) -> bool:
    """Small dependency-free mailbox validation suitable for configuration checks."""
    parsed = parseaddr((address or "").strip())[1]
    if parsed != (address or "").strip() or parsed.count("@") != 1:
        return False
    local, domain = parsed.rsplit("@", 1)
    return bool(local and domain and "." in domain and " " not in parsed)


def is_configured(config: dict) -> bool:
    """Return whether the non-secret fields are sufficient to attempt delivery."""
    sender = str(config.get("from_address") or config.get("username") or "").strip()
    recipients = config.get("to_addresses") or []
    return bool(config.get("host") and sender and recipients)


def send_email(
    config: dict,
    password: str,
    subject: str,
    body: str,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> bool:
    """Send one UTF-8 plain-text email through SSL, STARTTLS, or plain SMTP."""
    if not is_configured(config):
        return False

    host = str(config.get("host") or "").strip()
    try:
        port = int(config.get("port", 465))
    except (TypeError, ValueError):
        return False
    security = str(config.get("security") or "ssl")
    username = str(config.get("username") or "").strip()
    sender = str(config.get("from_address") or username).strip()
    recipients = [str(item).strip() for item in config.get("to_addresses", [])]
    if (
        not 1 <= port <= 65535
        or security not in SECURITY_MODES
        or not is_valid_email(sender)
        or not recipients
        or any(not is_valid_email(item) for item in recipients)
    ):
        return False

    message = EmailMessage()
    message["Subject"] = str(subject or "TickFlow 通知")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(str(body or ""))

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        smtp = None
        try:
            if security == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                smtp = smtplib.SMTP(host, port, timeout=10)
                if security == "starttls":
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
            # Delivery already succeeded; a failed QUIT must not retry and duplicate the email.
            try:
                smtp.quit()
            except Exception:
                with suppress(Exception):
                    smtp.close()
            return True
        except Exception as exc:  # SMTP/network errors must not escape
            last_err = str(exc)
            if smtp is not None:
                with suppress(Exception):
                    smtp.close()
        if attempt < max_attempts:
            time.sleep(1)

    logger.warning("邮件推送最终失败(已尝试 %d 次): %s", max_attempts, last_err)
    return False
