"""A removal made by any library re-read is not forwarded as a user deletion (#448).

The plugin held a removal back only while the Scan Media Library task
(`RefreshLibrary`) was running. Jellyfin 10.11 removes items whose files it
cannot see in `Folder.ValidateChildren`, and that also runs from the provider
refresh queue with the task idle: the library monitor (real-time monitoring,
`POST /Library/Media/Updated` from Radarr/Sonarr) and a one-library "Scan
library" (`POST /Items/{id}/Refresh`). Those removals reached the backend as
"Deleted via Jellyfin native UI" and took the catalogue row, download requests,
duplicate decisions and playlist entries with them.

`Folder.ValidateChildrenInternal` registers the folder with
`ProviderManager.OnRefreshStart` for as long as it runs and passes it as the
removal's parent, so the handler asks `GetRefreshProgress(e.Parent.Id)`.

There is no C# test host in this repo, so this reads the handler's source the
way test_delete_notification_fanout.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

HANDLER = Path("../tentacle-plugin/Services/LibraryDeleteHandler.cs")


def method(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|protected|internal|/// )", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestDeleteHandlerRereads(unittest.TestCase):
    def setUp(self):
        self.src = re.sub(r"//[^\n]*", "", HANDLER.read_text(encoding="utf-8"))
        self.on_removed = method(self.src, "private void OnItemRemoved(")

    def test_the_handler_gets_the_provider_manager(self):
        ctor = method(self.src, "public LibraryDeleteHandler(")
        self.assertRegex(ctor, r"IProviderManager\s+providerManager",
                         "the handler has no way to see a re-read the scan task didn't start")
        self.assertRegex(ctor, r"_providerManager\s*=\s*providerManager")

    def test_a_removal_from_a_folder_being_validated_is_held_back(self):
        self.assertRegex(self.src, r"_providerManager\.GetRefreshProgress\(\s*parent\.Id\s*\)\.HasValue",
                         "the refresh state of the removal's parent folder is never read")
        self.assertRegex(self.on_removed, r"IsParentBeingValidated\(\s*e\.Parent\s*\)",
                         "OnItemRemoved does not check the folder the item was removed from")

    def test_a_missing_parent_does_not_throw(self):
        check = method(self.src, "private bool IsParentBeingValidated(")
        self.assertRegex(check, r"if\s*\(\s*parent\s*==\s*null\s*\)\s*\{?\s*return false;")

    def test_the_guard_runs_before_the_deletion_is_armed_and_queued(self):
        guard = self.on_removed.index("IsParentBeingValidated(e.Parent)")
        self.assertLess(guard, self.on_removed.index("RecentDeletions.Record("),
                        "Confirm is armed before the re-read guard runs")
        self.assertLess(guard, self.on_removed.index("_pendingDeletes.Add("))

    def test_the_scan_task_guard_stays(self):
        self.assertRegex(self.on_removed, r"IsLibraryScanRunning\(\)")

    def test_items_without_a_tmdb_id_are_dropped_before_the_guards_log(self):
        # YouTube and recordings have no TMDB id; they must not write an
        # Information line per item on every re-read.
        tmdb = self.on_removed.index('TryGetValue("Tmdb"')
        self.assertLess(tmdb, self.on_removed.index("IsParentBeingValidated(e.Parent)"))
        self.assertLess(tmdb, self.on_removed.index("IsLibraryScanRunning()"))


if __name__ == "__main__":
    unittest.main()
