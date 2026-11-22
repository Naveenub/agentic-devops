"""Outbound email over SMTP (stdlib only). Used for API-key recovery links; nothing else sends mail.

  PRODUCT_SMTP_HOST       SMTP server. Unset = key recovery by email is off (operators can still issue keys).
  PRODUCT_SMTP_PORT       default 587 (STARTTLS). 465 uses implicit TLS.
  PRODUCT_SMTP_USER / PRODUCT_SMTP_PASSWORD   optional login
  PRODUCT_SMTP_STARTTLS   set to `off` only for a trusted local relay on a private network
  PRODUCT_SMTP_FROM       envelope + From address, e.g. "DevOps AI Agent <no-reply@example.com>"
  PRODUCT_PUBLIC_URL      public base URL of this deployment, e.g. https://agent.example.com. Recovery links are built
                          from this, never from the request's Host header (which a caller controls)."""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

log = logging.getLogger("product.mailer")


def public_url() -> str:
    return os.getenv("PRODUCT_PUBLIC_URL", "").strip().rstrip("/")


def configured() -> bool:
    url = public_url()
    return bool(os.getenv("PRODUCT_SMTP_HOST") and os.getenv("PRODUCT_SMTP_FROM")
                and url.startswith(("http://", "https://")))


def send(to: str, subject: str, body: str) -> None:
    """Raises on any SMTP/network failure; callers decide whether that matters."""
    host, port = os.environ["PRODUCT_SMTP_HOST"], int(os.getenv("PRODUCT_SMTP_PORT", "587"))
    sender = os.environ["PRODUCT_SMTP_FROM"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port, timeout=15)
    with smtp:
        if port != 465 and (os.getenv("PRODUCT_SMTP_STARTTLS") or "on").lower() not in ("off", "0", "false", "no"):
            smtp.starttls(context=ctx)
        if os.getenv("PRODUCT_SMTP_USER"):
            smtp.login(os.environ["PRODUCT_SMTP_USER"], os.getenv("PRODUCT_SMTP_PASSWORD", ""))
        smtp.send_message(msg, from_addr=parseaddr(sender)[1] or sender, to_addrs=[to])


def send_quietly(to: str, subject: str, body: str) -> None:
    """For background tasks: a mail failure is logged (never the body, which holds a secret link), not raised."""
    try:
        send(to, subject, body)
    except Exception as e:  # noqa: BLE001 - smtplib, ssl, socket and OS errors all land here
        log.warning("could not send email: %s", type(e).__name__)
