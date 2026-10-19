"""Temp directories the suite removes again.

scripts/check fails when a full run leaves anything in TMPDIR (thousands of
t.db / cache / .strm dirs used to pile up in /tmp). Use temp_dir() wherever a
test needs a scratch directory instead of tempfile.mkdtemp().
"""
import shutil
import tempfile
import unittest


def temp_dir(owner=None, **kwargs) -> str:
    """tempfile.mkdtemp(**kwargs), removed again when its owner is done.

    owner: the TestCase (removed after that test), or the TestCase class
    from setUpClass (after the class). Anything else, or none (a module-level
    helper), removes it when the running test module finishes.

    Never call it at import time (module top level): unittest keeps module
    cleanups in one global list and discover imports every module first, so
    such a dir would be removed when the first module finishes.
    """
    path = tempfile.mkdtemp(**kwargs)
    if isinstance(owner, unittest.TestCase):
        owner.addCleanup(shutil.rmtree, path, True)
    elif isinstance(owner, type) and issubclass(owner, unittest.TestCase):
        owner.addClassCleanup(shutil.rmtree, path, True)
    else:
        unittest.addModuleCleanup(shutil.rmtree, path, True)
    return path
