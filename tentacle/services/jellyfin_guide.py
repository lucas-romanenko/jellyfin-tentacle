"""Make Jellyfin pick up a changed Live TV lineup.

Shared by the IPTV Live TV page's "Refresh guide" button and by the YouTube
source, which used to leave this step to the user ("then refresh the guide in
Jellyfin") — the one manual step in an otherwise automatic flow.
"""
import logging
import threading

import requests

logger = logging.getLogger(__name__)


class GuideRefreshError(RuntimeError):
    """Jellyfin answered, but could not do what was asked."""


# One refresh at a time: every caller (the dashboard's refresh-guide route,
# the YouTube source) runs in this one process. Two at once both read the
# same provider, both re-created it and both deleted the old one, leaving
# two for good (#274).
_guide_lock = threading.Lock()


def _tentacle_providers(jf_url: str, headers: dict) -> list[dict]:
    """Jellyfin's XMLTV listing providers whose Path points at Tentacle's
    xmltv.xml, in Jellyfin's order."""
    livetv_cfg = requests.get(f"{jf_url}/System/Configuration/livetv",
                              headers=headers, timeout=10).json()
    found = []
    for lp in livetv_cfg.get("ListingProviders") or []:
        if lp.get("Type") != "xmltv":
            continue
        lp_path = (lp.get("Path") or "")
        if "xmltv.xml" not in lp_path.lower():
            logger.info(f"[LiveTV] Skipping non-Tentacle xmltv listing provider (Path={lp_path})")
            continue
        found.append(lp)
    return found


def _recreate(jf_url: str, headers: dict, path: str, same: list[dict]) -> None:
    """Re-create the listing provider for one Path from its first copy, then
    delete every other copy of that Path. Jellyfin 10.11 can save the provider
    and still answer 500 (it saves its config, then queueing RefreshGuide can
    throw), so whether it was created is read from its config, not the reply."""
    old_ids = {lp.get("Id") for lp in same}
    lp_new = {k: v for k, v in same[0].items() if k != "Id"}
    resp, error = None, None
    try:
        resp = requests.post(f"{jf_url}/LiveTv/ListingProviders", headers=headers,
                             json=lp_new, timeout=15)
    except requests.RequestException as e:
        error = e
    after = [lp for lp in _tentacle_providers(jf_url, headers) if lp.get("Path") == path]
    created = [lp for lp in after if lp.get("Id") not in old_ids]
    if not created:
        # Nothing was saved: the old provider(s) stay, so Jellyfin is never
        # left without one, and the caller hears why.
        if error is not None:
            raise error
        resp.raise_for_status()
        logger.warning("[LiveTV] Jellyfin accepted the listing provider but saved no new one")
        return
    reply_id = None
    if resp is not None and resp.ok:
        try:
            reply_id = resp.json().get("Id")
        except ValueError:
            pass
    keep = next((lp for lp in created if lp.get("Id") == reply_id), created[-1])
    saved_anyway = "" if resp is not None and resp.ok else " (Jellyfin saved it, then answered with an error)"
    logger.info(f"[LiveTV] Re-created Tentacle XMLTV listing provider as {keep.get('Id')}{saved_anyway}")
    for lp in after:
        if lp.get("Id") and lp.get("Id") != keep.get("Id"):
            requests.delete(f"{jf_url}/LiveTv/ListingProviders", params={"Id": lp["Id"]},
                            headers=headers, timeout=10)
            logger.info(f"[LiveTV] Deleted old Tentacle XMLTV listing provider {lp['Id']}")


def refresh_jellyfin_guide(jf_url: str, jf_key: str) -> None:
    """Re-create Tentacle's XMLTV listing provider, then run RefreshGuide.

    Two steps because of a Jellyfin quirk: re-POSTing a listing provider with
    the same Id does NOT map newly appeared channels to their guide data. It
    has to be recreated. Safeguards on that:
      1. Only listing providers whose Path points at Tentacle's own xmltv.xml
         are touched — never an unrelated xmltv provider.
      2. The new one is created and confirmed (in Jellyfin's config) BEFORE
         the old one is deleted, so a failed recreate cannot leave Jellyfin
         with no listing provider.
      3. One provider per Path is kept: copies left by an earlier failure are
         deleted, not re-created, and refreshes never overlap (#274).

    Raises requests.RequestException if Jellyfin cannot be reached, and
    GuideRefreshError if it has no RefreshGuide task.
    """
    headers = {"X-Emby-Token": jf_key, "Content-Type": "application/json"}

    with _guide_lock:
        by_path: dict[str, list[dict]] = {}
        for lp in _tentacle_providers(jf_url, headers):
            by_path.setdefault(lp.get("Path") or "", []).append(lp)
        for path, same in by_path.items():
            _recreate(jf_url, headers, path, same)

        tasks = requests.get(f"{jf_url}/ScheduledTasks", headers=headers, timeout=10).json()
        guide_task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
        if not guide_task:
            raise GuideRefreshError("RefreshGuide task not found in Jellyfin")
        resp = requests.post(f"{jf_url}/ScheduledTasks/Running/{guide_task['Id']}",
                             headers=headers, timeout=10)
        resp.raise_for_status()
