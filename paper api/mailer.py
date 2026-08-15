"""Delivery of the sign-in code, over Gmail SMTP.

`smtplib` and nothing else — no provider SDK, no queue, no retries. The message is small,
the recipient list is one address long, and a failure is recoverable by the user pressing
the button again, so the machinery a transactional mail service brings would be carrying
weight it does not have here.

Credentials are `GMAIL_USER` and `GMAIL_APP_PASSWORD`, from the environment or
`../.env.local`. It must be an **app password** (myaccount.google.com → Security →
2-Step Verification → App passwords), not the account password: Google has refused plain
password SMTP since 2022, and the app password is separately revocable, which matters for
a credential that lives on a workstation.

Sending is blocking and takes about a second. Every caller in `api_auth` therefore runs
it off the request path — see the note there about why that is a security property and
not just latency.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import api_config

log = logging.getLogger("stockhunt.api.mail")


class MailError(RuntimeError):
    """SMTP refused, or was never configured. Carries a message safe to log."""


def configured() -> bool:
    return api_config.mail_configured()


def send(to: str, subject: str, text: str, html: str | None = None) -> None:
    """Send one message. Raises `MailError`; never returns a failure quietly."""
    if not configured():
        raise MailError("no SMTP credentials (GMAIL_USER / GMAIL_APP_PASSWORD)")
    if "\n" in to or "\r" in to or "\n" in subject or "\r" in subject:
        # Header injection. The addresses here are already validated upstream, so this
        # can only fire if a future caller stops doing that -- which is exactly when a
        # cheap check earns its place.
        raise MailError("newline in recipient or subject")

    msg = EmailMessage()
    msg["From"] = formataddr((api_config.MAIL_FROM_NAME, str(api_config.MAIL_FROM)))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Auto-Submitted"] = "auto-generated"        # keeps vacation responders quiet
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(api_config.SMTP_HOST, api_config.SMTP_PORT,
                              timeout=api_config.SMTP_TIMEOUT, context=context) as smtp:
            smtp.login(str(api_config.GMAIL_USER), str(api_config.GMAIL_APP_PASSWORD))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"SMTP login rejected: {exc.smtp_code} — is it an app password?") from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"SMTP send failed: {exc}") from exc


def send_code(to: str, code: str, ttl_seconds: int) -> None:
    """The sign-in code itself.

    The code is not in the subject line. It would be convenient — a phone shows it in the
    notification without unlocking — and that is the problem: a lock-screen preview is
    readable by whoever is holding the phone, which is the one attacker a second factor
    delivered to a mailbox is supposed to stop.
    """
    minutes = max(1, round(ttl_seconds / 60))
    app = api_config.APP_NAME
    text = (
        f"Your {app} sign-in code:\n\n"
        f"    {code}\n\n"
        f"It expires in {minutes} minutes and can be used once.\n\n"
        f"If you did not ask to sign in, ignore this message — somebody typed your "
        f"address into the login form and nothing has happened to your account. "
        f"Access to this desk is by invitation, so no code was created for anyone else.\n"
    )
    html = (
        f'<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        f'font-size:15px;line-height:1.5;color:#111">'
        f'<p>Your <strong>{app}</strong> sign-in code:</p>'
        f'<p style="font-size:30px;letter-spacing:.18em;font-weight:700;'
        f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin:18px 0">{code}</p>'
        f'<p>It expires in {minutes} minutes and can be used once.</p>'
        f'<p style="color:#666;font-size:13px">If you did not ask to sign in, ignore this '
        f'message — nothing has happened to your account.</p></div>'
    )
    send(to, f"{app} sign-in code", text, html)


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Check the SMTP credentials, or send a test.")
    ap.add_argument("--to", help="send a real test message to this address")
    ap.add_argument("--check", action="store_true",
                    help="log in and disconnect without sending anything")
    args = ap.parse_args()

    if not configured():
        print("not configured: set GMAIL_USER and GMAIL_APP_PASSWORD in ../.env.local")
        raise SystemExit(1)
    print(f"account   {api_config.GMAIL_USER}")
    print(f"server    {api_config.SMTP_HOST}:{api_config.SMTP_PORT}")

    if args.check or not args.to:
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(api_config.SMTP_HOST, api_config.SMTP_PORT,
                                  timeout=api_config.SMTP_TIMEOUT, context=context) as smtp:
                smtp.login(str(api_config.GMAIL_USER), str(api_config.GMAIL_APP_PASSWORD))
            print("login     OK")
        except Exception as exc:                                  # noqa: BLE001
            print(f"login     FAILED — {exc}")
            raise SystemExit(1)
    if args.to:
        send_code(args.to, "123456", api_config.OTP_TTL_SECONDS)
        print(f"sent      test code to {args.to}")


if __name__ == "__main__":
    _main()
