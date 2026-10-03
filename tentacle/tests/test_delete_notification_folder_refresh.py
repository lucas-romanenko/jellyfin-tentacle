"""A title Jellyfin drops while it re-reads a folder is not a user deletion.

tentacle-plugin/Services/LibraryDeleteHandler.cs forwards Jellyfin's ItemRemoved
for a Movie or Series to the backend's DELETE /api/library/item, which drops the
catalogue row, the title's download requests, its duplicate decisions and its
playlist entries. Jellyfin also removes every item whose files it cannot see
when it re-reads a folder (a pool branch or nested mount that dropped out, a
folder not written back yet), and the path check cannot tell that from a
delete, because the files really are gone. #49 held those removals back during
the "Scan Media Library" task.

The task is not the only way Jellyfin re-reads a folder. The library monitor
(real-time monitoring, and POST /Library/Media/Updated, which Radarr and Sonarr's
Jellyfin connection send after every import) and a scan or metadata refresh of
one library (POST /Items/{library}/Refresh?Recursive=true, which Tentacle itself
sends) go through Jellyfin's refresh queue with the task idle, and remove
missing items the same way (Folder.ValidateChildren). Those removals were
forwarded, and the backend's Confirm call got 200. Live, on Jellyfin 10.11.8,
with one title's folder missing: the task held it back; a one-library scan and
an import announced with /Library/Media/Updated forwarded it.

Folder.ValidateChildren registers the folder with the provider manager for as
long as it runs (OnRefreshStart / OnRefreshComplete) and reports that folder as
the removal's parent, so the handler asks IProviderManager about that folder.

There is no C# test host in this repo, so this reads the source the way
tests/test_delete_notification_shutdown.py does.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

HANDLER = Path("../tentacle-plugin/Services/LibraryDeleteHandler.cs")


def _code(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def method(src: str, name: str) -> str:
    m = re.search(r"\n    (?:private|public|internal|protected)[^\n(]*\b%s\(" % re.escape(name), src)
    if not m:
        return ""
    rest = src[m.start() + 1:]
    end = re.search(r"\n    (?:private|public|internal|protected|/// <summary>)", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestRemovalsWhileAFolderIsReRead(unittest.TestCase):
    def setUp(self):
        self.src = _code(HANDLER.read_text())
        body = method(self.src, "OnItemRemoved")
        cut = body.find("RecentDeletions.Record(")
        self.assertGreater(cut, 0, "test out of date: OnItemRemoved no longer records the deletions it forwards")
        # Everything that runs before a removal is recorded (which arms the
        # backend's Confirm) and queued, plus the handler's methods it calls.
        self.before_record = body[:cut]
        called = set(re.findall(r"\b([A-Z]\w+)\(", self.before_record)) - {"OnItemRemoved"}
        self.guard = self.before_record + "".join(method(self.src, n) for n in sorted(called))

    def test_the_handler_gets_the_provider_manager(self):
        ctor = re.search(r"public LibraryDeleteHandler\(([^)]*)\)", self.src)
        self.assertIsNotNone(ctor)
        self.assertIn(
            "IProviderManager", ctor.group(1),
            "the handler only has ITaskManager: a one-library scan or a library-monitor refresh "
            "runs with the \"RefreshLibrary\" task idle, and nothing else tells it a folder is being re-read")

    def test_a_removal_from_a_folder_being_re_read_is_not_forwarded(self):
        self.assertIsNotNone(
            re.search(r"GetRefreshProgress\(", self.guard),
            "OnItemRemoved decides 'scan' from the scheduled task alone: a title removed because its "
            "folder is missing during a refresh-queue re-read is recorded and sent to "
            "DELETE /api/library/item as if a user had deleted it")

    def test_the_folder_asked_about_is_the_one_the_item_was_removed_from(self):
        # Jellyfin reports the folder it was re-reading as the removal's parent.
        # Asking about that folder, rather than "is anything refreshing", keeps a
        # delete in one library from being held back by a refresh of another.
        self.assertIsNotNone(
            re.search(r"\(\s*e\.Parent(?:\.Id)?\s*\)", self.before_record),
            "the refresh check is not about the folder the item was removed from")

    def test_the_scan_task_guard_stays(self):
        self.assertIn("IsLibraryScanRunning()", self.before_record,
                      "the #49 guard for the Scan Media Library task is gone")


if __name__ == "__main__":
    unittest.main()
