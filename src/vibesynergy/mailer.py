"""A generic mailer: send through any SMTP server, optionally keeping a copy in the Sent
folder through IMAP (many providers don't store mail sent over SMTP).

Configured under ``[mail]``: ``smtp_host``, ``smtp_port``, ``smtp_security`` (starttls |
ssl | none), ``username``, ``from``; optionally ``imap_host``, ``imap_port`` (SSL) and
``sent_folder``. The password is in the secret store ``mail`` (set in Settings → Alerts),
never in the config file. Used by alerts, and by anything else that sends mail later.
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from vibesynergy.core.secrets import SecretStore

SECRETS = "mail"
SECURITY = ("starttls", "ssl", "none")
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


class MailError(Exception):
    pass


def configured(settings: Any) -> bool:
    cfg = settings.mail
    return bool(cfg.get("smtp_host") and cfg.get("username") and _password(settings))


def _password(settings: Any) -> str | None:
    return (SecretStore(settings.secrets_dir, SECRETS).get("password") or {}).get("password")


def save_password(settings: Any, password: str) -> None:
    SecretStore(settings.secrets_dir, SECRETS).set("password", {"password": password})


def send(settings: Any, to: str, subject: str, body: str, html: str | None = None) -> None:
    """Send a plain-text message (with an HTML alternative when given); with IMAP set
    up, also file a copy in the Sent folder."""
    cfg = settings.mail
    if not configured(settings):
        raise MailError("mail is not set up: fill in Mail settings under Settings → Alerts")
    security = cfg.get("smtp_security", "starttls")
    if security not in SECURITY:
        raise MailError(f"[mail] smtp_security must be one of {', '.join(SECURITY)}")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.get("from") or cfg["username"]
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=str(cfg["smtp_host"]))
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    host, port = cfg["smtp_host"], int(cfg.get("smtp_port") or DEFAULT_PORTS[security])
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
        with smtp:
            if security == "starttls":
                smtp.starttls(context=context)
            smtp.login(cfg["username"], _password(settings) or "")
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"sending through {host}:{port} failed: {exc}") from exc
    if cfg.get("imap_host"):
        _file_copy(settings, message)


def _file_copy(settings: Any, message: EmailMessage) -> None:
    """Append the sent message to the Sent folder (a failure here doesn't undo the send)."""
    cfg = settings.mail
    try:
        with imaplib.IMAP4_SSL(
            cfg["imap_host"],
            int(cfg.get("imap_port") or 993),
            ssl_context=ssl.create_default_context(),
            timeout=30,  # a server that accepts and then stalls must not hold up the sync lane
        ) as imap:
            imap.login(cfg["username"], _password(settings) or "")
            imap.append(
                cfg.get("sent_folder") or "Sent",
                "(\\Seen)",
                imaplib.Time2Internaldate(time.time()),
                message.as_bytes(),
            )
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailError(f"sent, but saving a copy through IMAP failed: {exc}") from exc
