import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from config import EMAIL_SENDER, EMAIL_PASSWORD, logger

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL

# Short, strictly lowercase subject lines (rotated deterministically).
SUBJECT_LINES = ["quick question", "your channel", "your videos"]

def _pick_subject(blogger_name: str) -> str:
    """Deterministically pick a lowercase subject so retries stay consistent."""
    if not blogger_name:
        return SUBJECT_LINES[0]

    index = sum(ord(ch) for ch in blogger_name) % len(SUBJECT_LINES)
    return SUBJECT_LINES[index]

def send_cold_email(to_email: str, blogger_name: str, pitch_text: str) -> bool:
    """
    Send a cold pitch via Gmail SMTP over SSL (port 465).

    Returns:
        True  -> email was sent.
        False -> skipped (no recipient) or failed.
    """
    if not to_email:
        logger.info(
            "No contact email for %s; skipping email send", blogger_name
        )
        return False

    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        logger.error("Email credentials missing; cannot send email")
        return False

    subject = _pick_subject(blogger_name)

    message = EmailMessage()
    message["From"] = formataddr((EMAIL_SENDER.split("@")[0], EMAIL_SENDER))
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(pitch_text)

    context = ssl.create_default_context()

    try:
        with smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, context=context, timeout=30
        ) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(message)

        logger.info(
            "Email sent to %s (%s) | subject: '%s'",
            to_email,
            blogger_name,
            subject,
        )
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP auth failed (check Gmail app password): %s", exc)
        return False
    except Exception as exc:  # network / SMTP / SSL errors
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False
