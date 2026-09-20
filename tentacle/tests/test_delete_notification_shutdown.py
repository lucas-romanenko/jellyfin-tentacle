"""Shutdown must wait for deletion notifications, and the cap is a total.

Two leftovers from the delete-notification fan-out fix in
tentacle-plugin/Services/LibraryDeleteHandler.cs:

1. `StopAsync` flushed the pending batch with a fire-and-forget `Task.Run` and
   returned `Task.CompletedTask`. The host then calls `Dispose()`, which disposes
   the `HttpClient` the batch is still using: every remaining DELETE fails with
   ObjectDisposedException and the backend is never told about those deletions.
2. The semaphore that caps concurrent DELETEs at four was created inside
   `ProcessBatchAsync`, one per batch. Batches overlap whenever the backend is
   slower than the 2 s debounce, and each brought its own four slots.

There is no C# test host in this repo, so this reads the source the way
tests/test_frontend_state.py reads the dashboard JS.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

HANDLER = Path("../tentacle-plugin/Services/LibraryDeleteHandler.cs")


def _code(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def method(src: str, signature: str) -> str:
    start = src.index(signature)
    rest = src[start:]
    end = re.search(r"\n    (?:private|public|internal|/// <summary>)", rest[1:])
    return rest[: end.start() + 1] if end else rest


class TestShutdownWaitsForTheLastBatch(unittest.TestCase):
    def setUp(self):
        self.src = _code(HANDLER.read_text())
        m = re.search(r"public\s+(async\s+)?Task\s+StopAsync\(", self.src)
        self.stop = method(self.src, m.group(0))
        self.process = method(self.src, "private void ProcessPendingDeletes()")

    def test_the_batch_task_is_not_thrown_away(self):
        self.assertNotRegex(
            self.process, r"_\s*=\s*Task\.Run\(",
            "the batch is launched fire-and-forget: nothing can ever wait for it")

    def test_stop_awaits_outstanding_work(self):
        self.assertRegex(self.stop, r"\bawait\b",
                         "StopAsync returns before the flushed batch has been sent; "
                         "Dispose() then disposes the HttpClient underneath it")
        self.assertNotRegex(self.stop, r"return\s+Task\.CompletedTask")

    def test_what_stop_awaits_is_what_process_launched(self):
        launched = re.search(r"(\w+)\s*=\s*Task\.Run\(", self.process)
        self.assertIsNotNone(launched)
        tracked = re.search(r"(_\w+)\.Add\(\s*%s\s*\)" % launched.group(1), self.process)
        self.assertIsNotNone(tracked, "the launched batch is not recorded anywhere")
        self.assertIn(tracked.group(1), self.stop,
                      f"StopAsync never looks at {tracked.group(1)}")

    def test_the_wait_is_bounded_by_the_shutdown_token(self):
        self.assertRegex(self.stop, r"cancellationToken",
                         "StopAsync ignores the host's shutdown token; a hung "
                         "backend would hold Jellyfin's shutdown")

    def test_finished_batches_are_forgotten(self):
        self.assertRegex(self.src, r"_\w+\.Remove\(",
                         "completed batches accumulate for the life of the process")


class TestTheCapIsATotal(unittest.TestCase):
    def setUp(self):
        self.src = _code(HANDLER.read_text())
        self.batch = method(self.src, "private async Task ProcessBatchAsync(")

    def test_the_semaphore_is_not_created_per_batch(self):
        self.assertNotRegex(
            self.batch, r"new SemaphoreSlim\(",
            "each batch creates its own semaphore, so N overlapping batches run "
            "N x MaxConcurrentNotifications requests")

    def test_one_semaphore_belongs_to_the_handler(self):
        fields = re.findall(
            r"private\s+(?:static\s+)?readonly\s+SemaphoreSlim\s+(\w+)\s*=\s*new SemaphoreSlim\(\s*MaxConcurrentNotifications",
            self.src)
        self.assertEqual(len(fields), 1)
        self.assertRegex(self.batch, rf"\b{fields[0]}\b",
                         "the shared semaphore is declared but the batch does not use it")


if __name__ == "__main__":
    unittest.main()
