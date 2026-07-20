"""Live ticket updates over Frappe's Socket.IO layer.

publish_ticket_update is wired to Support Ticket `on_update` (hooks.py) and emits two
events, both after_commit (so a re-fetch triggered by the event reads committed data):

  1. `ticket_update` to the DOC room (`doc:Support Ticket/<name>`). Joining that room
     runs Frappe's can_subscribe_doc → our ticket_has_permission, so only the owner,
     owning team, and looped-in collaborators receive it. It carries the name so an open
     detail view re-fetches exactly this ticket.
  2. `ticket_list_dirty` to the DOCTYPE room (`doctype:Support Ticket`) — a CONTENTLESS
     ping so open list/board views refetch their own permission-scoped set. It carries no
     ticket identity, so it can safely fan out to every subscribed staff/portal session
     without leaking a ticket an agent isn't allowed to see.
"""
import frappe


def publish_ticket_update(doc, method=None):
    frappe.publish_realtime(
        "ticket_update",
        {"name": doc.name, "modified": str(doc.modified)},
        doctype="Support Ticket",
        docname=doc.name,
        after_commit=True,
    )
    frappe.publish_realtime(
        "ticket_list_dirty",
        {},
        doctype="Support Ticket",
        after_commit=True,
    )
