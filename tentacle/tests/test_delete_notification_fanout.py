"""A bulk delete must not fire one request — and one ERROR — per item.

`ProcessPendingDeletes` launched a detached `Task.Run` per deleted item with no
concurrency limit, against one `HttpClient` with a 10 s timeout. A live server
produced 15,943 `[ERR]` lines with full stack traces and 15,983 matching `[INF]`
lines from a single library event; 44 of ~16,000 notifications succeeded,
because the requests starved each other behind the connection limit.

There is no C# test host in this repo, so this reads the handler's source the
way tests/test_frontend_state.py reads the dashboard JS.

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


class TestDeleteNotificationFanout(unittest.TestCase):
    def setUp(self):
        self.src = HANDLER.read_text()

    def test_the_fan_out_is_bounded(self):
        self.assertRegex(
            self.src,
            r"const int MaxConcurrentNotifications\s*=\s*([1-9]\d?)\s*;",
            "nothing caps how many DELETE notifications are in flight at once",
        )
        cap = int(re.search(r"MaxConcurrentNotifications\s*=\s*(\d+)", self.src).group(1))
        self.assertLessEqual(cap, 8, f"a cap of {cap} is not a bound worth having")

    def test_the_cap_is_actually_enforced(self):
        self.assertRegex(
            self.src,
            r"new SemaphoreSlim\(\s*MaxConcurrentNotifications",
            "the cap is declared but never gates the requests",
        )

    def test_one_task_is_launched_for_the_batch_not_one_per_item(self):
        proc = method(self.src, "private void ProcessPendingDeletes()")
        launches = re.findall(r"Task\.Run\(", proc)
        self.assertEqual(
            len(launches), 1,
            "ProcessPendingDeletes still launches a detached task per item",
        )
        self.assertNotRegex(
            proc, r"foreach\s*\([^)]*batch\)",
            "the batch is still iterated at launch time rather than inside a "
            "bounded worker",
        )

    def test_a_failure_is_not_logged_once_per_item_with_a_stack_trace(self):
        # LogError(ex, ...) per item was the 15,943-line half of the incident.
        self.assertNotRegex(
            self.src,
            r"LogError\(\s*ex\s*,\s*\"\[Tentacle\] Failed to notify backend of deletion",
            "each failed item still writes an ERROR with a full stack trace",
        )

    def test_detection_is_not_logged_at_information_per_item(self):
        self.assertNotRegex(
            self.src,
            r"LogInformation\(\s*\"\[Tentacle\] Detected deletion",
            "a bulk removal still writes one Information line per item",
        )

    def test_the_batch_reports_a_single_summary(self):
        self.assertRegex(
            self.src,
            r"Notified backend of \{Succeeded\}",
            "there is no per-batch summary line",
        )

    def test_the_summary_carries_the_first_failure(self):
        self.assertRegex(
            self.src.replace("\n", " "),
            r"LogWarning\(\s*firstFailure\s*,",
            "the one summary warning does not carry an exception, so the cause "
            "of a failed batch is lost entirely",
        )


if __name__ == "__main__":
    unittest.main()
