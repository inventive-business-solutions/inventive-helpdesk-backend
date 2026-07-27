# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""The response-time promise made in the acknowledgement email.

This is a COMMITMENT TO A CLIENT, sent once and never corrected — re-triaging a ticket
later does not re-send the ack. So the mapping has to be right the first time, and a
priority that silently falls through to a shorter turnaround than intended is a promise
the team never agreed to make.

Nothing measures or enforces these targets yet (`sla_risk` and `due_date` are fields that
nothing populates). RESPONSE_TARGETS is the single place the numbers live, so an SLA engine
arriving later reads from it rather than growing a second table beside it.
"""
import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.email import (
    DEFAULT_RESPONSE_TARGET,
    RESPONSE_TARGETS,
    _ack_email_html,
    response_target,
)


class TestResponseTarget(IntegrationTestCase):
    def test_every_priority_the_doctype_offers_has_a_target(self):
        """Derived from the doctype, not hand-listed: a priority added to the Select
        without a target here would silently fall back to the generic phrase, quietly
        under- or over-promising on a whole band of tickets."""
        options = frappe.get_meta("Support Ticket").get_field("priority").options or ""
        priorities = [p.strip() for p in options.split("\n") if p.strip()]
        self.assertTrue(priorities, "priority field has no options")
        missing = [p for p in priorities if p not in RESPONSE_TARGETS]
        self.assertEqual(missing, [], f"priorities with no response target: {missing}")

    def test_targets_are_ordered_by_urgency(self):
        """Critical must not promise a slower reply than Low. Ordering is the thing a
        careless edit breaks, and it is invisible in a dict literal."""
        self.assertEqual(
            list(RESPONSE_TARGETS),
            ["Critical", "High", "Medium", "Low"],
            "RESPONSE_TARGETS must stay ordered most-urgent first",
        )

    def test_unset_or_unknown_priority_falls_back(self):
        for value in (None, "", "   ", "Whenever"):
            self.assertEqual(response_target(value), DEFAULT_RESPONSE_TARGET)

    def test_known_priority_returns_its_own_target(self):
        self.assertEqual(response_target("Critical"), RESPONSE_TARGETS["Critical"])
        self.assertEqual(response_target(" High "), RESPONSE_TARGETS["High"])

    def test_the_promise_actually_reaches_the_email_body(self):
        """The mapping being right is worth nothing if the template drops it."""
        html = _ack_email_html("TKT-0001", "Valve stuck", "", priority="Critical")
        self.assertIn(RESPONSE_TARGETS["Critical"], html)
        self.assertIn("expect a reply", html)

    def test_an_untriaged_ticket_is_promised_the_medium_band(self):
        """Emailed-in tickets carry the doctype default of Medium. Pinned because the
        promise made at insert is the one that stands — re-triaging does not re-send it."""
        default = frappe.get_meta("Support Ticket").get_field("priority").default
        self.assertEqual(default, "Medium")
        self.assertIn(RESPONSE_TARGETS["Medium"], _ack_email_html("TKT-2", "x", "", priority=default))

    def test_the_subject_line_is_still_escaped(self):
        """The new block sits next to user-supplied text; escaping must not have regressed."""
        html = _ack_email_html("TKT-3", "<script>alert(1)</script>", "", priority="Low")
        self.assertNotIn("<script>", html)
