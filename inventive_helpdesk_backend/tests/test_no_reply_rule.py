"""No Reply Rule validation — the operator override that decides who we stop writing to.

Two reasons this doctype earns its own tests. It is layer 1 of sender.no_reply_reason and
wins over the built-in conventions, so a wrong rule silently stops a customer hearing
anything. And its Regex mode runs an operator-supplied pattern from before_save, on every
ticket save, with no timeout available in Python's `re`.
"""
import time

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.inventive_helpdesk.doctype.no_reply_rule.no_reply_rule import (
    MAX_PATTERN_LENGTH,
)


def _rule(pattern, match_type="Regex"):
    return frappe.get_doc({
        "doctype": "No Reply Rule",
        "pattern": pattern,
        "match_type": match_type,
        "enabled": 1,
    })


class TestNoReplyRuleValidation(IntegrationTestCase):
    def tearDown(self):
        frappe.db.rollback()

    def test_a_valid_regex_is_accepted(self):
        doc = _rule(r"^donotreply\d*@")
        doc.insert(ignore_permissions=True)
        self.assertTrue(frappe.db.exists("No Reply Rule", doc.name))

    def test_an_invalid_regex_is_rejected_with_the_reason(self):
        with self.assertRaises(frappe.ValidationError):
            _rule("([unclosed").insert(ignore_permissions=True)

    def test_an_empty_pattern_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            _rule("   ", match_type="Exact").insert(ignore_permissions=True)

    def test_an_overlong_pattern_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            _rule("a" * (MAX_PATTERN_LENGTH + 1), match_type="Exact").insert(ignore_permissions=True)

    def test_a_catastrophically_backtracking_pattern_is_rejected(self):
        """The one that matters. `(a+)+$` compiles perfectly and is exponential on a
        non-matching string — and sender.classify runs from Support Ticket.before_save, so
        storing it would hang a worker on every ticket save until someone found it."""
        with self.assertRaises(frappe.ValidationError):
            _rule(r"(a+)+$").insert(ignore_permissions=True)

    def test_rejection_happens_quickly(self):
        """The guard must not itself be the stall. It probes with a 33-character string,
        so the exponential case is caught in milliseconds rather than by waiting it out."""
        start = time.perf_counter()
        with self.assertRaises(frappe.ValidationError):
            _rule(r"(x+x+)+y$").insert(ignore_permissions=True)
        self.assertLess(time.perf_counter() - start, 2.0, "the guard took too long to reject")

    def test_ordinary_patterns_are_not_caught_by_the_timing_probe(self):
        """A false positive here costs an operator a legitimate rule, so the budget has to
        clear anything realistic by a wide margin."""
        for pattern in (
            r"^no[-_]?reply@",
            r"^(bounce|bounces)@",
            r".*@notifications\.example\.com$",
            r"^(?:auto|do-not)-reply\d{0,4}@[a-z0-9.-]+$",
        ):
            with self.subTest(pattern=pattern):
                doc = _rule(pattern)
                doc.insert(ignore_permissions=True)
                self.assertTrue(frappe.db.exists("No Reply Rule", doc.name))

    def test_non_regex_modes_skip_the_regex_checks(self):
        """A Domain or Prefix pattern is matched by string comparison, so characters that
        would be regex metacharacters are literal and must not be rejected."""
        for match_type, pattern in (
            ("Domain", "example.co.in"),
            ("Prefix", "noreply+"),
            ("Exact", "a.b(c)@example.com"),
        ):
            with self.subTest(match_type=match_type):
                doc = _rule(pattern, match_type=match_type)
                doc.insert(ignore_permissions=True)
                self.assertTrue(frappe.db.exists("No Reply Rule", doc.name))
