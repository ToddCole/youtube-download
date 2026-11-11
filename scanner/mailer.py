"""Email copy of the digest and alerts, over SMTP (Gmail by default).

.env:
  MFO_EMAIL_TO    where to send (required to turn email on)
  SMTP_PASSWORD   for Gmail, an App Password (myaccount.google.com/apppasswords)
  SMTP_USER       login; defaults to MFO_EMAIL_TO
  SMTP_HOST       default smtp.gmail.com
  SMTP_PORT       default 465 (SSL)

Manual test:  python3 mailer.py
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

import settings  # noqa: F401  (loads .env)


def configured() -> bool:
    return bool(os.environ.get("MFO_EMAIL_TO") and os.environ.get("SMTP_PASSWORD"))


def send(subject: str, body: str) -> bool:
    """Plain-text email. Returns False (with a warning) rather than raising."""
    if not configured():
        return False
    to = os.environ["MFO_EMAIL_TO"]
    user = os.environ.get("SMTP_USER") or to
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"MFO Editorial Desk <{user}>"
    msg["To"] = to
    msg.set_content(body)
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
            # Google shows App Passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
            smtp.login(user, "".join(os.environ["SMTP_PASSWORD"].split()))
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: email failed ({subject}): {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    if not configured():
        print("not configured: set MFO_EMAIL_TO and SMTP_PASSWORD in .env")
        sys.exit(1)
    ok = send("MFO Desk test", "Email alerts are working.")
    print("sent" if ok else "not sent")
    sys.exit(0 if ok else 1)
