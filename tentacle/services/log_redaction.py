"""Keep credentials out of every log record, whichever logger made it.

Xtream providers put the account's username and password in the stream path
(/live/<user>/<pass>/<id>.ts) and in playlist/guide query strings, and the
Jellyfin plugin and Android TV app authenticate with ?api_key=<access token>.
Those URLs reach the log from Tentacle's own messages, from httpx's request
logging and from uvicorn's access log. Redacting at record creation covers all
of them, and every handler (console, the dashboard's live log) at once.
"""
import logging
import re
import traceback

_XTREAM_PATH = re.compile(
    r"(/(?:live|movie|series|timeshift)/)[^/\s?#\"']+/[^/\s?#\"']+/", re.IGNORECASE)
_SECRET_PARAM = re.compile(
    r"((?:^|[?&;\s])(?:api_key|apikey|password|passwd|pwd|token|access_token|secret|x-emby-token)=)"
    r"[^&\s\"']+", re.IGNORECASE)

_MARK = "_tentacle_redacting"


def redact(text: str) -> str:
    if not text or ("=" not in text and "/" not in text):
        return text
    text = _XTREAM_PATH.sub(r"\1***/***/", text)
    return _SECRET_PARAM.sub(r"\1***", text)


def installed() -> bool:
    return getattr(logging.getLogRecordFactory(), _MARK, False)


def install() -> None:
    if installed():
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        try:
            if record.name == "uvicorn.access" and isinstance(record.args, tuple) \
                    and len(record.args) == 5:
                a = list(record.args)
                a[2] = redact(str(a[2]))
                record.args = tuple(a)
            else:
                msg = record.getMessage()
                clean = redact(msg)
                if clean != msg:
                    record.msg, record.args = clean, None
        except Exception:
            pass  # never lose a log line over redaction
        try:
            # A traceback is rendered by the handler's Formatter, long after
            # this factory ran, and requests/httpx put the whole URL in the
            # exception text ("500 Server Error for url: ...password=...").
            # Formatter.format() uses record.exc_text as-is when it is already
            # set, so render it here, cleaned, once for every handler.
            if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
                record.exc_text = redact(
                    "".join(traceback.format_exception(*record.exc_info)).rstrip("\n"))
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:
            pass
        return record

    setattr(factory, _MARK, True)
    logging.setLogRecordFactory(factory)
