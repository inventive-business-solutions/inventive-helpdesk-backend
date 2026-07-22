# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TicketReadReceipt(Document):
	"""One row per (ticket, user): when that member last opened the ticket.

	Deliberately not a child table on Support Ticket. The unread marker is per agent —
	Neha reading a client reply must not clear it for Arjun — and a child table would
	rewrite the parent's modified timestamp on every read, which the 30s poller and the
	realtime nudge both key off.
	"""

	pass
