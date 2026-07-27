# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""Which saves are allowed to wake every connected session.

`ticket_list_dirty` is a BROADCAST. Every staff and portal browser holding a socket
receives it and refetches its entire ticket list — so the cost of publishing it is
(sessions x list size), not one message.

Before the guard in realtime.py, every doc.save() published it. add_message, add_note and
upload_attachment all save the ticket, so one agent typing a work note told the whole
organisation to refetch, and an email burst multiplied that by the number of tickets AND
the number of open tabs. Load that rises with both the surge and the audience is the shape
of an outage rather than a slowdown.

The rule under test: broadcast only when a column a LIST ROW renders actually changed.
Anyone looking at the ticket still gets the doc-room `ticket_update`, which is the event
that carries the change to where it is visible.
"""
import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.realtime import LIST_VISIBLE_FIELDS, _list_row_changed

CLIENT = "_Test Fanout Client"


def _ticket():
    if not frappe.db.exists("Client", CLIENT):
        frappe.get_doc({"doctype": "Client", "client_name": CLIENT, "client_code": "TFC"}).insert(
            ignore_permissions=True
        )
    doc = frappe.get_doc(
        {
            "doctype": "Support Ticket",
            "title": "Fanout probe",
            "client": CLIENT,
            "ticket_type": "Bug",
            "priority": "Medium",
            "status": "New",
            "description": "probe",
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    return doc


class TestRealtimeFanout(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.doc = _ticket()

    def test_a_brand_new_ticket_always_pings(self):
        # No previous version = an insert. A new row must reach every open list.
        fresh = frappe.get_doc("Support Ticket", self.doc.name)
        self.assertIsNone(fresh.get_doc_before_save())
        self.assertTrue(_list_row_changed(fresh))

    def test_a_work_note_does_not_ping(self):
        """The regression this exists for: notes are the highest-frequency save there is."""
        doc = frappe.get_doc("Support Ticket", self.doc.name)
        doc.append("notes", {"author": "Administrator", "body": "internal thinking"})
        doc.last_activity_on = frappe.utils.now_datetime()
        doc.save(ignore_permissions=True)
        self.assertFalse(_list_row_changed(doc))

    def test_a_client_message_does_not_ping(self):
        doc = frappe.get_doc("Support Ticket", self.doc.name)
        doc.append("conversation", {"author": "Administrator", "body": "reply to client"})
        doc.last_activity_on = frappe.utils.now_datetime()
        doc.save(ignore_permissions=True)
        self.assertFalse(_list_row_changed(doc))

    def test_a_status_change_does_ping(self):
        doc = frappe.get_doc("Support Ticket", self.doc.name)
        doc.status = "In Progress"
        doc.save(ignore_permissions=True)
        self.assertTrue(_list_row_changed(doc))

    def test_an_assignment_change_does_ping(self):
        doc = frappe.get_doc("Support Ticket", self.doc.name)
        doc.priority = "Critical"
        doc.save(ignore_permissions=True)
        self.assertTrue(_list_row_changed(doc))

    def test_every_guarded_field_is_one_the_list_actually_renders(self):
        """Derived, not hand-checked: a field added to the guard that the doctype does not
        have would silently compare None to None and suppress broadcasts forever."""
        meta = frappe.get_meta("Support Ticket")
        missing = [f for f in LIST_VISIBLE_FIELDS if not meta.get_field(f)]
        self.assertEqual(missing, [], f"guard lists fields the doctype lacks: {missing}")
