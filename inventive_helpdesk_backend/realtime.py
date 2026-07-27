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
from frappe.realtime import get_doctype_room

#: The columns a list row actually renders (mirrors TICKET_LIST_FIELDS in the frontend
#: store). A change to anything outside this set cannot alter any open list view, so it
#: has no business waking every connected session.
LIST_VISIBLE_FIELDS = (
    "title",
    "ticket_type",
    "priority",
    "status",
    "client",
    "division",
    "product",
    "raised_by",
    "assignee",
    "assignment_group",
    "due_date",
    "sla_risk",
)


def _list_row_changed(doc) -> bool:
    """Did this save alter anything a list row displays?

    `ticket_list_dirty` is a broadcast: every connected staff and portal session refetches
    its whole ticket list on receipt. Adding a work note or a client message calls
    doc.save(), so before this check a single note told the entire organisation to refetch
    — and a burst of them multiplied by every open tab. Anyone actually *looking* at the
    ticket still gets the doc-room `ticket_update` below, which is the event that carries
    the change to where it is visible.

    No previous version means an insert (or a save outside the normal flow), which must
    always ping: a brand-new ticket is a new row.
    """
    before = doc.get_doc_before_save()
    if before is None:
        return True
    return any(doc.get(f) != before.get(f) for f in LIST_VISIBLE_FIELDS)


def publish_ticket_update(doc, method=None):
    frappe.publish_realtime(
        "ticket_update",
        {"name": doc.name, "modified": str(doc.modified)},
        doctype="Support Ticket",
        docname=doc.name,
        after_commit=True,
    )
    if not _list_row_changed(doc):
        return
    # Pass `room` explicitly: publish_realtime only derives the doctype room for the
    # built-in `list_update` event. For a custom event, doctype-without-docname falls
    # through to the site room ("all"), which Frappe's socket handler joins for System
    # Users only — so portal (Website User) sessions would never receive this.
    frappe.publish_realtime(
        "ticket_list_dirty",
        {},
        room=get_doctype_room("Support Ticket"),
        after_commit=True,
    )
