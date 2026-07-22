"""Classify every pre-existing ticket.

`sender_kind` is computed in Support Ticket.before_save, so tickets written before the
field existed have it blank — and a blank shows the agent nothing at exactly the moment
the badge is meant to warn them. This fills them in once.

Deliberately not a bulk SQL UPDATE: the answer depends on POC lookups and the no-reply
rules, so it has to go through sender.classify. Written with db_set/update_modified=False
so a back-fill does not look like every ticket was edited today, and so it cannot fire
before_save (and manufacture an activity-log row) on tickets nobody touched. Idempotent.
"""
import frappe

from inventive_helpdesk_backend import sender


def execute():
    names = frappe.get_all(
        "Support Ticket",
        filters={"sender_kind": ["in", [None, ""]]},
        pluck="name",
    )
    for name in names:
        doc = frappe.get_doc("Support Ticket", name)
        kind, _email, reason = sender.classify(doc)
        frappe.db.set_value(
            "Support Ticket",
            name,
            {"sender_kind": kind, "no_reply_reason": reason},
            update_modified=False,
        )
    if names:
        frappe.db.commit()
