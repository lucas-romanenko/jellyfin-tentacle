"""Make Jellyfin pick up a changed Live TV lineup.

Shared by the IPTV Live TV page's "Refresh guide" button and by the YouTube
source, which used to leave this step to the user ("then refresh the guide in
Jellyfin") — the one manual step in an otherwise automatic flow.
"""
import logging

import requests

logger = logging.getLogger(__name__)


class GuideRefreshError(RuntimeError):
    """Jellyfin answered, but could not do what was asked."""


def refresh_jellyfin_guide(jf_url: str, jf_key: str) -> None:
    """Re-create Tentacle's XMLTV listing provider, then run RefreshGuide.

    Two steps because of a Jellyfin quirk: re-POSTing a listing provider with
    the same Id does NOT map newly appeared channels to their guide data. It
    has to be recreated. Two safeguards on that:
      1. Only listing providers whose Path points at Tentacle's own xmltv.xml
         are touched — never an unrelated xmltv provider.
      2. The new one is created and confirmed BEFORE the old one is deleted, so
         a failed recreate cannot leave Jellyfin with no listing provider.

    Raises requests.RequestException if Jellyfin cannot be reached, and
    GuideRefreshError if it has no RefreshGuide task.
    """
    headers = {"X-Emby-Token": jf_key, "Content-Type": "application/json"}

    livetv_cfg = requests.get(f"{jf_url}/System/Configuration/livetv",
                              headers=headers, timeout=10).json()
    for lp in livetv_cfg.get("ListingProviders", []):
        if lp.get("Type") != "xmltv":
            continue
        lp_path = (lp.get("Path") or "")
        if "xmltv.xml" not in lp_path.lower():
            logger.info(f"[LiveTV] Skipping non-Tentacle xmltv listing provider (Path={lp_path})")
            continue
        lp_id = lp.get("Id")
        lp_new = {k: v for k, v in lp.items() if k != "Id"}
        resp = requests.post(f"{jf_url}/LiveTv/ListingProviders", headers=headers,
                             json=lp_new, timeout=15)
        resp.raise_for_status()
        new_id = resp.json().get("Id", "?")
        logger.info(f"[LiveTV] Re-created Tentacle XMLTV listing provider as {new_id}")
        if lp_id and new_id and new_id != "?" and new_id != lp_id:
            requests.delete(f"{jf_url}/LiveTv/ListingProviders?Id={lp_id}",
                            headers=headers, timeout=10)
            logger.info(f"[LiveTV] Deleted old Tentacle XMLTV listing provider {lp_id}")

    tasks = requests.get(f"{jf_url}/ScheduledTasks", headers=headers, timeout=10).json()
    guide_task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
    if not guide_task:
        raise GuideRefreshError("RefreshGuide task not found in Jellyfin")
    resp = requests.post(f"{jf_url}/ScheduledTasks/Running/{guide_task['Id']}",
                         headers=headers, timeout=10)
    resp.raise_for_status()
