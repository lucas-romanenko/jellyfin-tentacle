"""Module-level state in the dashboard JS must be declared.

`node --check` only parses. A function that assigns to `_ytPoll` parses fine
whether or not `let _ytPoll` exists anywhere — the ReferenceError comes at
call time, in the browser, after a deploy. One such declaration was removed
along with the functions around it and the page broke on the very action it
was there to support.

The convention here is that shared page state is an underscore-prefixed
identifier (`_ytPoll`, `_autoCategoryOrder`). Anything assigned under such a
name has to be declared with let/var/const somewhere in the file, or be a
parameter of the function doing the assigning.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

JS_FILES = [Path("static/js/pages.js"), Path("static/js/app.js")]

NAME = r"_[A-Za-z][A-Za-z0-9]*"
# `_name = ...`, `_name += ...`, `_name++` — but not `==`/`===`, and not `.x = `
# on some other object.
ASSIGNED = re.compile(rf"(?<![\w.$]){NAME}(?=\s*(?:=(?!=)|\+\+|--|\+=|-=))")
DECLARED = re.compile(rf"\b(?:let|var|const)\s+({NAME})")
# Only genuine parameter lists introduce names: function signatures, arrow
# functions, and catch clauses. Call sites — `clearInterval(_ytPoll)` — do
# not, which is exactly what a looser pattern got wrong.
PARAM_LISTS = re.compile(
    r"(?:\bfunction\b\s*[A-Za-z0-9_$]*\s*\(([^)]*)\)"      # function f(a, b)
    r"|\(([^()]*)\)\s*=>"                                  # (a, b) =>
    r"|(?<![\w.$])(" + NAME + r")\s*=>"                    # _a =>
    r"|\bcatch\s*\(([^)]*)\))"                             # catch (_e)
)


def undeclared_state(src: str) -> list:
    declared = set(DECLARED.findall(src))
    for m in PARAM_LISTS.finditer(src):
        for group in m.groups():
            if group:
                declared |= set(re.findall(NAME, group))
    return sorted(set(ASSIGNED.findall(src)) - declared)


class TestSharedStateIsDeclared(unittest.TestCase):
    def test_every_assigned_underscore_name_is_declared(self):
        missing = {}
        for path in JS_FILES:
            if path.exists():
                names = undeclared_state(path.read_text(encoding="utf-8"))
                if names:
                    missing[str(path)] = names
        self.assertEqual(missing, {}, f"assigned but never declared: {missing}")

    def test_the_check_catches_the_case_it_exists_for(self):
        # A guard that cannot fail on the bug it was written for is decoration.
        src = "let _other = 1;\nfunction go() { _ytPoll = setInterval(f, 1); clearInterval(_ytPoll); }\n"
        self.assertEqual(undeclared_state(src), ["_ytPoll"])
        self.assertEqual(undeclared_state("let _ytPoll = null;\n" + src), [])

    def test_parameters_count_but_call_arguments_do_not(self):
        self.assertEqual(undeclared_state("function f(_x) { _x = 1; }"), [])
        self.assertEqual(undeclared_state("const g = (_x) => { _x = 1; };"), [])
        self.assertEqual(undeclared_state("try {} catch (_e) { _e = null; }"), [])
        self.assertEqual(undeclared_state("function f() { use(_x); _x = 1; }"), ["_x"])
