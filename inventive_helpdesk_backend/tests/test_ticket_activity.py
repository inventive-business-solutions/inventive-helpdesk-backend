# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase


class TestTicketActivity(IntegrationTestCase):
    """Guards the ticket activity log.

    The log has to be written by the document layer, not the API layer: the frontend
    edits status/priority/assignment straight over the REST resource endpoint, so
    there is no whitelisted method to instrument and a desk edit or script would
    bypass one anyway. These tests therefore drive plain doc.save() — if the log ever
    moves back into the API methods, they fail.
    """

    def setUp(self):
        self.client = _ensure("Client", {"client_name": "Activity Test Co", "client_code": "ATC"})
        self.division = _ensure(
            "Division",
            {"division_name": "Activity Div", "division_code": "ADV", "client": self.client},
        )

    def _ticket(self, **overrides):
        doc = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Activity fixture",
            "description": "fixture",
            "ticket_type": "Query",
            "priority": "Low",
            "status": "New",
            "client": self.client,
            "division": self.division,
            **overrides,
        })
        return doc.insert(ignore_permissions=True)

    def test_a_new_ticket_opens_with_one_created_row(self):
        # One origin row, not a burst of "changed from nothing" lines per tracked field.
        t = self._ticket()
        self.assertEqual([(r.action, r.new_value) for r in t.activity], [("Created", "New")])

    def test_a_tracked_field_change_is_recorded_with_who_and_what(self):
        t = self._ticket()
        t.status = "In Progress"
        t.save(ignore_permissions=True)

        row = t.activity[-1]
        self.assertEqual(row.action, "Status")
        self.assertEqual(row.old_value, "New")
        self.assertEqual(row.new_value, "In Progress")
        self.assertTrue(row.author)
        self.assertTrue(row.acted_on)

    def test_two_fields_changed_in_one_save_produce_two_rows(self):
        t = self._ticket()
        before = len(t.activity)
        t.status = "Acknowledged"
        t.priority = "Critical"
        t.save(ignore_permissions=True)

        self.assertEqual(
            {r.action for r in t.activity[before:]},
            {"Status", "Priority"},
        )

    def test_an_untracked_edit_adds_nothing(self):
        # Description and title churn must not bury the handovers the log exists for.
        t = self._ticket()
        before = len(t.activity)
        t.description = "edited"
        t.save(ignore_permissions=True)

        self.assertEqual(len(t.activity), before)

    def test_resaving_without_a_change_adds_nothing(self):
        # Frappe saves the doc on every note/message append, so a no-op save is the
        # common case — it must not manufacture rows.
        t = self._ticket()
        before = len(t.activity)
        t.save(ignore_permissions=True)

        self.assertEqual(len(t.activity), before)

    def test_clearing_an_empty_link_is_not_a_change(self):
        # "" and None both mean unassigned; flipping between them is not history.
        t = self._ticket()
        before = len(t.activity)
        t.assignee = None
        t.save(ignore_permissions=True)

        self.assertEqual(len(t.activity), before)

    def test_an_empty_link_reads_as_unassigned(self):
        member = _ensure("Team Member", {"member_name": "Activity Tester", "email": "at@example.com"})
        group = _ensure("Assignment Group", {"group_name": "Activity Team"})
        t = self._ticket()
        t.assignment_group = group
        t.assignee = member
        t.save(ignore_permissions=True)

        row = next(r for r in t.activity if r.action == "Assignee")
        self.assertEqual(row.old_value, "Unassigned")
        self.assertEqual(row.new_value, member)

    def test_the_activity_field_is_permlevel_1(self):
        # This single property is what keeps the log off every client POC response —
        # the same mechanism that hides internal work notes. If the permlevel is ever
        # dropped, clients start seeing who the ticket was passed between.
        meta = frappe.get_meta("Support Ticket")
        self.assertEqual(meta.get_field("activity").permlevel, 1)
        self.assertEqual(meta.get_field("notes").permlevel, 1)


def _ensure(doctype: str, values: dict) -> str:
    """Get-or-create a fixture row, returning its docname."""
    key = {k: v for k, v in values.items() if k in ("client_name", "division_name", "member_name", "group_name")}
    existing = frappe.db.get_value(doctype, key)
    if existing:
        return existing
    return frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True).name
