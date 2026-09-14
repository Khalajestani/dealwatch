"""Optional notifications after a run: e-mail (SMTP) or Telegram. Channel is chosen in Settings."""

from __future__ import annotations

import json
import logging
import re
import smtplib
import urllib.request
from email.message import EmailMessage

from ..config import Env
from ..models import Run
from ..settings import Settings
from .report import render_html

log = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    pass


def send(env: Env, settings: Settings, run: Run, markdown: str) -> None:
    subject = f"Dealwatch: {run.deal_count} deal(s) - run #{run.id}"
    if settings.notify_channel == "email":
        _send_email(env, subject, markdown)
    elif settings.notify_channel == "telegram":
        _send_telegram(env, subject, markdown)


def _send_email(env: Env, subject: str, markdown: str) -> None:
    if not (env.smtp_host and env.smtp_from and env.smtp_to):
        raise NotifyError("email channel selected but SMTP_HOST / SMTP_FROM / SMTP_TO are not set")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env.smtp_from
    msg["To"] = env.smtp_to
    msg.set_content(markdown)
    msg.add_alternative(f"<html><body>{render_html(markdown)}</body></html>", subtype="html")
    if env.smtp_port == 465:
        with smtplib.SMTP_SSL(env.smtp_host, env.smtp_port, timeout=30) as smtp:
            if env.smtp_user and env.smtp_password:
                smtp.login(env.smtp_user, env.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(env.smtp_host, env.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if env.smtp_user and env.smtp_password:
                smtp.login(env.smtp_user, env.smtp_password)
            smtp.send_message(msg)


def markdown_to_plain(markdown: str, limit: int = 3800) -> str:
    text = re.sub(r"<details>.*?</details>", "", markdown, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = text.replace("**", "").replace("_", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + ("\n…" if len(text) > limit else "")


def _send_telegram(env: Env, subject: str, markdown: str) -> None:
    if not (env.telegram_bot_token and env.telegram_chat_id):
        raise NotifyError("telegram channel selected but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
    payload = json.dumps(
        {"chat_id": env.telegram_chat_id, "text": f"{subject}\n\n{markdown_to_plain(markdown)}", "disable_web_page_preview": True}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{env.telegram_bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https host
        if resp.status >= 300:
            raise NotifyError(f"telegram responded {resp.status}")
