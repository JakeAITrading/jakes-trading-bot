"""Optional email summary of each cycle that traded (or would have)."""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self):
        self.to = os.environ.get("ALERT_EMAIL_TO", "")
        self.host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")

    @property
    def enabled(self) -> bool:
        return bool(self.to and self.user and self.password)

    def send_report(self, report: dict) -> None:
        if not self.enabled:
            return
        msg = EmailMessage()
        mode = "DRY RUN" if report.get("dry_run") else "LIVE"
        msg["Subject"] = f"[Trading bot {mode}] {len(report.get('actions', []))} Moby trade(s)"
        msg["From"], msg["To"] = self.user, self.to
        msg.set_content(json.dumps(report, indent=2, default=str))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls()
                s.login(self.user, self.password)
                s.send_message(msg)
        except Exception:
            log.exception("Could not send alert email")
