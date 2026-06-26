from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import httpx

from ajap import config

logger = logging.getLogger(__name__)

_TRACK_EMOJI = {
    "DATA_ENGINEERING": "🔵",
    "MLOPS_MLE": "🟣",
    "GENERAL_SWE": "🟢",
}
_MAX_DISCORD_CHARS = 1990
_MAX_SHOWN = 12


def _build_digest(matches: list[dict]) -> str:
    n = len(matches)
    header = f"🆕 **{n} new match{'es' if n != 1 else ''}** from AJAP\n"
    lines = []
    for m in matches[:_MAX_SHOWN]:
        emoji = _TRACK_EMOJI.get(m["track"], "⚪")
        match_str = ""
        if m.get("resume_pick") and m.get("fit_confidence"):
            match_str = f" → {m['resume_pick']} ({m['fit_confidence']})"
        lines.append(f"{emoji} {m['track']} — {m['company']} — {m['title']}{match_str} — {m['url']}")
    if n > _MAX_SHOWN:
        lines.append(f"+{n - _MAX_SHOWN} more in the dashboard")
    body = "\n".join(lines)
    content = header + body
    if len(content) > _MAX_DISCORD_CHARS:
        content = content[:_MAX_DISCORD_CHARS] + "…"
    return content


def _send_discord(matches: list[dict]) -> None:
    content = _build_digest(matches)
    resp = httpx.post(config.DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()
    logger.info("Discord alert sent: %d matches", len(matches))


def _email_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS and config.SMTP_TO)


def _send_email(matches: list[dict]) -> None:
    n = len(matches)
    subject = f"AJAP: {n} new match{'es' if n != 1 else ''}"
    body_lines: list[str] = [f"{n} new role(s) landed in PENDING_EXECUTION:\n"]
    for m in matches:
        emoji = _TRACK_EMOJI.get(m["track"], "")
        body_lines.append(f"{emoji} [{m['track']}] {m['company']} — {m['title']}")
        body_lines.append(f"  {m['url']}\n")

    msg = MIMEText("\n".join(body_lines))
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = config.SMTP_TO

    with smtplib.SMTP(config.SMTP_HOST, int(config.SMTP_PORT)) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASS)
        s.sendmail(msg["From"], [config.SMTP_TO], msg.as_string())
    logger.info("Email alert sent: %d matches", n)


def send_alert(matches: list[dict]) -> bool:
    """Fire Discord (preferred) or email (fallback) for new PENDING_EXECUTION matches.

    Each match dict: {company, title, track, url}.
    Returns True if an alert was delivered.
    """
    if not matches:
        return False

    if config.DISCORD_WEBHOOK_URL:
        try:
            _send_discord(matches)
            return True
        except Exception as exc:
            logger.error("Discord alert failed: %s", exc)

    if _email_configured():
        try:
            _send_email(matches)
            return True
        except Exception as exc:
            logger.error("Email alert failed: %s", exc)

    logger.info("No alert destination configured — %d new matches:", len(matches))
    for m in matches:
        logger.info("  [%s] %s @ %s  %s", m["track"], m["title"], m["company"], m["url"])
    return False
