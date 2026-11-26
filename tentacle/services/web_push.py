"""Web Push: Tentacle's messages on a phone or computer with no page open
(Nov 2026, first for Save offline's "<title> is ready to save").

A browser that allowed it gives a subscription (its push service's address
and two keys, PushSubscription); Tentacle sends each message encrypted for
that browser only (RFC 8291, aes128gcm) and signed with the server's own
VAPID key (RFC 8292), which it makes once and keeps in the settings table
(`webpush_private_key`: a credential, never served, never logged).

Works in Chrome, Edge and Firefox (phone and computer) and in the iPhone's
home-screen app (iOS 16.4+); an iPhone browser tab can't get them.

Only the browsers' own push services are sent to (PUSH_HOSTS): a
subscription is the browser's word, and Tentacle must not be made to POST
anywhere else.
"""
import base64
import json
import logging
import os
import struct
import time
from typing import Optional
from urllib.parse import urlsplit

import requests

from models.database import get_setting, set_setting

logger = logging.getLogger(__name__)

KEY_SETTING = "webpush_private_key"
TIMEOUT = 15
TTL_S = 24 * 3600
# The push services of Chrome/Edge/Android (FCM), Firefox, Safari/iOS, Windows.
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com",
              "android.googleapis.com")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def allowed_endpoint(url: str) -> bool:
    """A push service's https address (pure)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.username and any(
        host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def _private_key(db):
    """The server's VAPID key, made the first time it is needed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pem = (get_setting(db, KEY_SETTING, "") or "").strip()
    if pem:
        try:
            return serialization.load_pem_private_key(pem.encode(), password=None)
        except ValueError:
            logger.warning("[Push] The saved push key can't be read: a new one is made "
                           "(browsers subscribed before must subscribe again)")
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    set_setting(db, KEY_SETTING, pem)
    return key


def _raw_public(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key(db) -> str:
    """What a browser subscribes with (applicationServerKey), base64url."""
    return _b64(_raw_public(_private_key(db)))


def _vapid_header(key, endpoint: str, subject: str, now: Optional[float] = None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600),
              "sub": subject}
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{head}.{body}".encode()
    r, s = decode_dss_signature(key.sign(signing, ec.ECDSA(hashes.SHA256())))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={head}.{body}.{_b64(sig)}, k={_b64(_raw_public(key))}"


def encrypt(payload: bytes, p256dh: str, auth: str, salt: Optional[bytes] = None,
            server_key=None) -> bytes:
    """The message for one browser (RFC 8291, aes128gcm, one record)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_public = _unb64(p256dh)
    auth_secret = _unb64(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    rs = 4096
    return salt + struct.pack(">IB", rs, len(as_public)) + as_public + record


def _subject(db) -> str:
    """Who sends (the push services ask): Tentacle's https address."""
    from services import family
    tent = (family.addresses(db).get("tentacle") or "").strip()
    return tent if tent.startswith("https://") else "mailto:tentacle@localhost"


def send(db, user_id: int, message: dict, endpoint: Optional[str] = None) -> int:
    """Sends `message` ({"title", "body", "url", "tag"}) to every browser
    this person allowed (`endpoint`: only that one, when it is still
    theirs); a subscription its push service says is gone is deleted.
    Returns how many took it. Never raises."""
    from models.database import PushSubscription
    subs = db.query(PushSubscription).filter(PushSubscription.user_id == user_id).all()
    if endpoint and any(s.endpoint == endpoint for s in subs):
        subs = [s for s in subs if s.endpoint == endpoint]
    if not subs:
        return 0
    try:
        key = _private_key(db)
        subject = _subject(db)
    except Exception as e:
        logger.warning(f"[Push] No push key: {type(e).__name__}")
        return 0
    payload = json.dumps(message).encode()[:3000]
    sent = 0
    for sub in subs:
        if not allowed_endpoint(sub.endpoint):
            db.delete(sub)
            continue
        host = urlsplit(sub.endpoint).hostname
        try:
            body = encrypt(payload, sub.p256dh, sub.auth)
            r = requests.post(sub.endpoint, data=body, timeout=TIMEOUT, headers={
                "Authorization": _vapid_header(key, sub.endpoint, subject),
                "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                "TTL": str(TTL_S), "Urgency": "normal"})
        except Exception as e:
            logger.info(f"[Push] Not sent to {host}: {type(e).__name__}")
            continue
        if r.status_code in (404, 410):
            logger.info(f"[Push] A browser's subscription at {host} is gone: removed")
            db.delete(sub)
        elif r.status_code >= 300:
            logger.info(f"[Push] {host} answered {r.status_code}: {(r.text or '')[:200]}")
        else:
            sub.last_sent_at = _now()
            sent += 1
    db.commit()
    return sent


def _now():
    from datetime import datetime
    return datetime.utcnow()


def subscribe(db, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
    """Keeps a browser's subscription for this person (ValueError when it isn't a push service's)."""
    from models.database import PushSubscription
    if not allowed_endpoint(endpoint) or len(endpoint) > 2000:
        raise ValueError("Not a browser's push service")
    try:
        if len(_unb64(p256dh)) != 65 or len(_unb64(auth)) != 16:
            raise ValueError("Not a push subscription's keys")
    except (ValueError, TypeError):
        raise ValueError("Not a push subscription's keys")
    sub = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if sub is None:
        sub = PushSubscription(endpoint=endpoint)
        db.add(sub)
    sub.user_id, sub.p256dh, sub.auth = user_id, p256dh, auth
    db.commit()


def unsubscribe(db, user_id: int, endpoint: str) -> None:
    from models.database import PushSubscription
    db.query(PushSubscription).filter(PushSubscription.user_id == user_id,
                                      PushSubscription.endpoint == endpoint).delete()
    db.commit()
