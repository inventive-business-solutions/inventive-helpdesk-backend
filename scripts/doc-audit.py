#!/usr/bin/env python3
"""Does ARCHITECTURE.md still describe the app that exists?

A reference doc fails quietly. Nothing errors when an endpoint is added and the table is
not, so the doc keeps reading as authoritative while it silently stops being true -- and
the moment it is doubted once, it is worth nothing. This checks the parts that can be
derived, so drift is a test failure rather than a discovery.

Audited: whitelisted endpoints, DocTypes, patches, doc_events, scheduler jobs, permission
hooks, and the test count. Prose is not checkable and is not checked.

    python3 scripts/doc-audit.py          # from the app root

Exits non-zero listing anything in the code but not in the doc.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "inventive_helpdesk_backend"
DOC = ROOT / "docs" / "ARCHITECTURE.md"


def _hook_block(text: str, key: str) -> str:
    """The literal assigned to `key` in hooks.py, braces balanced."""
    m = re.search(rf"^{key}\s*=\s*\{{", text, re.M)
    if not m:
        return ""
    depth, i = 0, m.end() - 1
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    return ""


def collect() -> dict[str, set[str]]:
    hooks = (APP / "hooks.py").read_text()
    found: dict[str, set[str]] = {}

    # Whitelisted endpoints. Decorators may stack (rate_limit), so allow them between.
    endpoints = set()
    for py in APP.glob("*.py"):
        endpoints |= set(
            re.findall(
                r"@frappe\.whitelist\([^)]*\)(?:\s*@[^\n]+\n)*\s*def\s+(\w+)", py.read_text()
            )
        )
    found["endpoint"] = endpoints

    doctypes = set()
    for path in (APP / "inventive_helpdesk" / "doctype").glob("*/*.json"):
        d = json.loads(path.read_text())
        if isinstance(d, dict) and d.get("doctype") == "DocType":
            doctypes.add(d["name"])
    found["doctype"] = doctypes

    found["patch"] = {
        line.rsplit(".", 1)[-1]
        for line in (APP / "patches.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith(("#", "["))
    }

    # Hook targets: the function name each hook points at.
    for key in ("doc_events", "scheduler_events", "permission_query_conditions", "has_permission"):
        block = _hook_block(hooks, key)
        found[key] = {t.rsplit(".", 1)[-1] for t in re.findall(r'"([\w.]+\.[\w.]+)"', block)}

    return found


def main() -> int:
    doc = DOC.read_text()
    found = collect()

    # Test count, stated in prose but derivable.
    real_tests = sum(
        len(re.findall(r"^\s*def test_", p.read_text(), re.M))
        for p in (APP / "tests").glob("test_*.py")
    )
    stated = re.search(r"^(\d+) tests across (\d+) modules", doc, re.M)
    real_modules = len(list((APP / "tests").glob("test_*.py")))

    problems: list[str] = []
    for kind, names in sorted(found.items()):
        # Match the bare name anywhere: the doc legitimately writes DocTypes in bold
        # (**Client**), hook targets inside code fences, and endpoints in backticks.
        # "is it mentioned at all" is the honest bar for a mechanical check -- whether the
        # surrounding sentence is any good is a review question, not a script's.
        # The lookbehind excludes word characters but NOT a dot: the doc writes these as
        # dotted paths (`email.send_ticket_ack`, `permissions.ticket_query`), so rejecting a
        # preceding dot would report every one of them as missing.
        missing = sorted(n for n in names if not re.search(rf"(?<!\w){re.escape(n)}\b", doc))
        if missing:
            problems.append(f"{kind}: {len(missing)} not in the doc -> {', '.join(missing)}")
        else:
            print(f"  ok  {kind:28} {len(names)}/{len(names)}")

    if not stated:
        problems.append("test count: no '<n> tests across <m> modules' line found")
    elif (int(stated.group(1)), int(stated.group(2))) != (real_tests, real_modules):
        problems.append(
            f"test count: doc says {stated.group(1)}/{stated.group(2)}, "
            f"code has {real_tests}/{real_modules}"
        )
    else:
        print(f"  ok  {'test count':28} {real_tests} tests, {real_modules} modules")

    if problems:
        print("\nARCHITECTURE.md is out of date:\n")
        for p in problems:
            print("  " + p)
        return 1
    print("\nARCHITECTURE.md matches the code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
