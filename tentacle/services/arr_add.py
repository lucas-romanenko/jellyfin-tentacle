"""Shared Radarr/Sonarr "add" plumbing.

One implementation for every add path, so the interactive add and the bulk
"add missing from list" add behave identically, and so a failure always carries
a reason the user can act on.

Three behaviours matter here:

* **Timeouts are not failures.** Radarr/Sonarr do a synchronous metadata
  refresh, artwork download and disk scan before answering `POST /movie`, which
  on a large library routinely takes longer than a short read timeout. The add
  has usually succeeded by then, so a timeout is verified rather than reported
  as a failure.
* **"Already added" is not a failure.** Both services say so clearly in the
  validator body; that maps to `already_exists`.
* **Every other failure keeps its reason.** `arr` validator payloads are
  structured and readable, and the known error codes map to plain sentences.
"""
import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# POST /movie and /series block on a metadata refresh + artwork fetch + disk
# scan. 127s has been measured on a busy instance; 90s covers the common case
# and a timeout past that is verified rather than reported as a failure.
ADD_TIMEOUT = 90
# Reads (root folders, quality profiles, lookups). /rootfolder computes free
# space per root, so it is not free either.
READ_TIMEOUT = 30
# How long to keep checking whether a timed-out add actually landed, counted
# from the moment the add was SENT (that is what the 127s measurement is), as a
# wall-clock deadline that includes the probes' own time. Radarr has been
# measured answering an add after 127s, so this has to outlast that. Measured
# from the send it also ends well inside the Jellyfin plugin's 240s add client
# (DiscoverController.AddClient); counted after the 90s timeout it did not.
VERIFY_TOTAL_SECONDS = 150

# added | exists | failed
ADDED = "added"
EXISTS = "exists"
FAILED = "failed"

# Validator error codes both services return, mapped to something actionable.
_VALIDATOR_MESSAGES = {
    "movieexistsvalidator": "it is already in Radarr",
    "seriesexistsvalidator": "it is already in Sonarr",
    "rootfoldervalidator": "the root folder Tentacle used does not exist in {service}",
    "seriespathvalidator": "another series is already using that folder",
    "moviepathvalidator": "another movie is already using that folder",
    "qualityprofileexistsvalidator": "the selected quality profile no longer exists in {service}",
    "recyclebinvalidator": "{service}'s recycle bin path is not writable",
}


def _validator_reasons(body_json, service: str) -> list:
    """Pull readable reasons out of an *arr validation response."""
    reasons = []
    entries = body_json if isinstance(body_json, list) else [body_json]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        code = (entry.get("errorCode") or "").lower()
        mapped = _VALIDATOR_MESSAGES.get(code)
        if mapped:
            reasons.append(mapped.format(service=service))
            continue
        msg = entry.get("errorMessage") or entry.get("message")
        if msg:
            reasons.append(str(msg).replace("<", " ").replace(">", " ").strip().rstrip("."))
    return reasons


def explain_arr_error(status_code: int, body: str, service: str) -> str:
    """Turn an *arr rejection into one sentence a user can act on."""
    text = body or ""
    try:
        parsed = json.loads(text) if text else None
    except Exception:
        parsed = None

    reasons = _validator_reasons(parsed, service) if parsed is not None else []
    if reasons:
        return f"{service} refused it: {'; '.join(dict.fromkeys(reasons))}."
    if status_code == 401:
        return f"{service} rejected Tentacle's API key — check it in Settings → Integrations."
    if status_code == 404:
        return f"{service} could not find this title in its metadata source."
    # The snippet lands in a toast that renders as HTML — strip anything that
    # could be read as markup rather than trusting the upstream response body.
    snippet = " ".join(text.replace("<", " ").replace(">", " ").split())[:180]
    return f"{service} returned HTTP {status_code}{f': {snippet}' if snippet else ''}."


def already_exists_in_body(body: str) -> bool:
    """True when an *arr rejection means "you already have this".

    Matches the validator codes and the exact phrase the *arrs use. A bare
    "already exists" substring was too loose — it matches unrelated messages
    (a folder or a root path already existing, say) and would report a real
    failure as a success.
    """
    low = (body or "").lower()
    return (
        "already been added" in low
        or "movieexistsvalidator" in low
        or "seriesexistsvalidator" in low
        or "artistexistsvalidator" in low
    )


class AddReport:
    """Accumulates add results and de-duplicates the failure reasons.

    Ten titles failing the same way should read as one sentence, not ten.
    """

    def __init__(self):
        self.added = 0
        self.already_exists = 0
        self.failed = 0
        self._reasons = []  # ordered, de-duplicated

    def record(self, outcome: str, reason: str = None):
        if outcome == ADDED:
            self.added += 1
        elif outcome == EXISTS:
            self.already_exists += 1
        else:
            self.failed += 1
            self.add_reason(reason)

    def add_reason(self, reason: str):
        if reason and reason not in self._reasons:
            self._reasons.append(reason)

    @property
    def detail(self) -> Optional[str]:
        if not self._reasons:
            return None
        return " ".join(self._reasons[:3]) + (
            f" (+{len(self._reasons) - 3} other reasons)" if len(self._reasons) > 3 else ""
        )

    def as_response(self) -> dict:
        resp = {"added": self.added, "already_exists": self.already_exists, "failed": self.failed}
        if self.detail:
            resp["detail"] = self.detail
        return resp


def radarr_root_folder(radarr_url: str, radarr_key: str) -> str:
    """Return Radarr's first non-VOD root folder.

    Raises RuntimeError if the folders can't be read. Inventing a path here is
    worse than failing: the guessed path is almost never a configured root, so
    the *arr rejects the add and a transient blip becomes a guaranteed failure
    the user can't diagnose.
    """
    r = requests.get(
        f"{radarr_url.rstrip('/')}/api/v3/rootfolder",
        headers={"X-Api-Key": radarr_key},
        timeout=READ_TIMEOUT,
    )
    r.raise_for_status()
    folders = r.json()
    if not folders:
        raise RuntimeError("Radarr has no root folders configured. Add one in Radarr → Settings → Media Management.")
    # Never default movie downloads into a VOD mount.
    non_vod = [f for f in folders if "vod" not in f["path"].lower()]
    return (non_vod or folders)[0]["path"]


def add_movie_to_radarr(radarr_url: str, radarr_key: str, tmdb_id: int,
                        quality_profile_id: int, root_folder: str) -> tuple:
    """POST one movie to Radarr. Returns (outcome, reason_or_None).

    A read timeout is verified against Radarr's library rather than reported as
    a failure — Radarr commonly finishes the add after we stop waiting.
    """
    url = f"{radarr_url.rstrip('/')}/api/v3/movie"
    payload = {
        "tmdbId": tmdb_id,
        "monitored": True,
        "qualityProfileId": quality_profile_id,
        "rootFolderPath": root_folder,
        "addOptions": {"searchForMovie": True},
    }
    sent_at = time.monotonic()
    try:
        r = requests.post(url, headers={"X-Api-Key": radarr_key}, json=payload, timeout=ADD_TIMEOUT)
    except requests.exceptions.Timeout:
        logger.warning(f"Radarr add tmdb:{tmdb_id} timed out after {ADD_TIMEOUT}s — verifying")
        if _radarr_has_movie(radarr_url, radarr_key, tmdb_id, sent_at=sent_at):
            logger.info(f"Radarr add tmdb:{tmdb_id} completed despite the timeout")
            return ADDED, None
        return FAILED, (
            f"Radarr did not finish adding within {ADD_TIMEOUT}s and the movie is not in its "
            f"library — it may be busy. Please retry in a moment."
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to add tmdb:{tmdb_id} to Radarr: {e}")
        return FAILED, "Could not reach Radarr. Check it is running and the URL in Settings → Integrations."

    if isinstance(getattr(r, "history", None), list) and r.history:
        # requests re-sends a POST answered with 301/302/303 as a GET of the
        # Location. Radarr's API never redirects, so a redirect means a proxy
        # (http -> https, a canonical host, an SSO login) answered instead, and
        # whatever came back — even 200 JSON, the movie LIST — is not an add.
        hop = r.history[0]
        where = hop.headers.get("Location", "?")
        logger.error(f"Radarr add tmdb:{tmdb_id} was redirected ({hop.status_code} -> {where}) — nothing was added")
        return FAILED, (f"Radarr's address redirects ({hop.status_code} to {where}), so the add never "
                        f"reached Radarr. Put the final address in Settings → Integrations.")

    if r.status_code < 400:
        try:
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("not a movie object")
        except ValueError:
            # A 2xx that is not Radarr's JSON (a reverse proxy's or SSO login
            # page, reached because requests follows a redirect on POST) means
            # the add never reached Radarr — same handling as Sonarr's add path.
            logger.error(f"Radarr returned a non-JSON {r.status_code} for tmdb:{tmdb_id}: {r.text[:200]}")
            return FAILED, ("Radarr returned a response Tentacle could not read. "
                            "Check whether a proxy sits in front of it.")
        return ADDED, None
    if already_exists_in_body(r.text):
        return EXISTS, None
    logger.error(f"Radarr rejected tmdb:{tmdb_id} — HTTP {r.status_code}: {r.text}")
    return FAILED, explain_arr_error(r.status_code, r.text, "Radarr")


def verify_backoff_delays(total_seconds: int = VERIFY_TOTAL_SECONDS) -> list:
    """Delays for the post-timeout poll: 2s, 4s, 8s, then every 15s."""
    delays = []
    elapsed = 0.0
    wait = 2.0
    while elapsed < total_seconds:
        delays.append(wait)
        elapsed += wait
        wait = min(wait * 2, 15.0)
    return delays


def _poll_until_present(check, label: str, total_seconds: int = VERIFY_TOTAL_SECONDS,
                        sent_at: Optional[float] = None) -> bool:
    """Poll `check` until it returns truthy or the budget runs out.

    A single sample taken two seconds after the timeout is not enough: the add
    that prompted this was answered by Radarr after 127 seconds, so a 90s
    timeout plus one immediate check still called a success a failure — and the
    user's retry then hit "already exists". Each probe is a targeted id filter,
    which is cheap enough to repeat.

    The budget is a deadline `total_seconds` after `sent_at` (the moment the add
    was sent; now if not given). Sleeps and probes both count, and no probe may
    run past the deadline, so a slow *arr cannot stretch the wait beyond the
    point where the Jellyfin plugin has already stopped listening.
    """
    deadline = (time.monotonic() if sent_at is None else sent_at) + total_seconds
    for delay in verify_backoff_delays(total_seconds):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        # 1s floor so the final probe, taken at the deadline, can still answer.
        probe_timeout = max(1.0, min(READ_TIMEOUT, deadline - time.monotonic()))
        try:
            if check(probe_timeout):
                return True
        except Exception as e:
            logger.debug(f"Verification probe for {label} failed: {e}")
    return False


def _radarr_has_movie(radarr_url: str, radarr_key: str, tmdb_id: int,
                      sent_at: Optional[float] = None) -> bool:
    """Whether Radarr ended up with this movie, polled (post-timeout verification)."""
    def _probe(timeout: float = READ_TIMEOUT) -> bool:
        r = requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/movie",
            headers={"X-Api-Key": radarr_key},
            params={"tmdbId": tmdb_id},
            timeout=timeout,
        )
        r.raise_for_status()
        movies = r.json()
        return isinstance(movies, list) and any(m.get("tmdbId") == tmdb_id for m in movies)

    return _poll_until_present(_probe, f"radarr tmdb:{tmdb_id}", sent_at=sent_at)
