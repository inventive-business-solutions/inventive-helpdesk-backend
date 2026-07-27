# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""No module may rebind `_`, because in this app `_` is the translator.

Every file here does `from frappe import _`, and `_` is also Python's conventional name for
a value you are deliberately throwing away. Those two conventions collide silently:

    _, status = _resolve_password_key(key)   # `_` is now a User document
    frappe.throw(_("This link has expired")) # TypeError: 'User' object is not callable

Nothing catches that statically. Ruff is happy — the assignment and the call are both valid.
Typing does not apply. It only fails when that branch actually runs, which for an error path
means it fails in front of whoever hit the error. This exact line shipped to a release
pipeline and failed there; the two tests that caught it were the ones exercising refusals.

A grep would work until someone writes `for _ in ...` or `[_ for _ in ...]`, so this walks
the AST for every binding form instead. It needs no site and no database, so it runs in
milliseconds wherever the suite runs.
"""

import ast
import pathlib
import unittest

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _rebinds_gettext(tree: ast.AST) -> list[int]:
    """Line numbers where `_` is assigned, in a module that imported it from frappe."""
    imports_gettext = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "frappe"
        and any(alias.name == "_" for alias in node.names)
        for node in ast.walk(tree)
    )
    if not imports_gettext:
        return []

    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.For | ast.comprehension):
            targets = [node.target]
        elif isinstance(node, ast.withitem):
            targets = [node.optional_vars] if node.optional_vars else []
        else:
            continue
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name) and sub.id == "_":
                    lines.append(sub.lineno)
    return lines


class TestTranslatorNotShadowed(unittest.TestCase):
    def test_no_module_rebinds_the_translator(self):
        offenders = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            for line in _rebinds_gettext(ast.parse(path.read_text())):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{line}")

        self.assertEqual(
            offenders,
            [],
            "`_` is the translator in these modules and must not be used as a throwaway. "
            "Name the unused value instead — `_user, status = ...`. Offenders: "
            + ", ".join(offenders),
        )

    def test_the_check_can_actually_fail(self):
        """A guard nobody has seen fail is a guard nobody should trust."""
        tree = ast.parse("from frappe import _\n_, status = something()\n")
        self.assertEqual(_rebinds_gettext(tree), [2])

        # And it stays quiet for a module that never imported the translator, where `_` as a
        # throwaway is perfectly ordinary Python.
        tree = ast.parse("_, status = something()\n")
        self.assertEqual(_rebinds_gettext(tree), [])
