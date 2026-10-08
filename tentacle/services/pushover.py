"""Pushover messages to the admin's phone (provider health alerts).

The app token and the user key are credentials: they live in the settings
table (pushover_app_token, pushover_user_key), masked by the settings API,
and go to Pushover in the POST body, never in a URL or a log line.
"""
import logging

import requests

from models.database import get_setting

logger = logging.getLogger(__name__)

API_URL = "https://api.pushover.net/1/messages.json"
TIMEOUT = 15


def keys(db) -> tuple:
    return ((get_setting(db, "pushover_app_token", "") or "").strip(),
            (get_setting(db, "pushover_user_key", "") or "").strip())


def configured(db) -> bool:
    token, user = keys(db)
    return bool(token and user)


def post(token: str, user: str, title: str, message: str, url: str = "") -> tuple:
    """One message. (sent, error text for the admin); never raises."""
    if not token or not user:
        return False, "Pushover is not set up: enter the app token and the user key"
    data = {"token": token, "user": user, "title": title[:250], "message": message[:1024]}
    if url:
        data["url"] = url[:512]
    try:
        r = requests.post(API_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"Pushover did not answer ({type(e).__name__})"
    if r.status_code == 200:
        return True, ""
    try:
        errors = r.json().get("errors") or []
    except (ValueError, AttributeError):
        errors = []
    # Pushover's own words ("application token is invalid"); they never echo the keys.
    text = "; ".join(str(e) for e in errors if e)[:300]
    return False, f"Pushover refused the message (HTTP {r.status_code}){': ' + text if text else ''}"


def send(db, title: str, message: str, url: str = "") -> bool:
    """False when Pushover isn't set up or the send failed; never raises."""
    token, user = keys(db)
    if not token or not user:
        return False
    ok, error = post(token, user, title, message, url)
    if not ok:
        logger.warning(f"[Pushover] {title!r} not sent: {error}")
    return ok
