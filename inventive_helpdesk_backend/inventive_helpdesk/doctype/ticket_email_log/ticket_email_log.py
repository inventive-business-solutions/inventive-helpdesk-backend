# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TicketEmailLog(Document):
	"""Append-only record of every email this app sent about a ticket.

	Deliberately not derived from Email Queue: frappe purges that after 30 days
	(frappe/hooks.py:508) — the same purge that used to break reply threading — so it
	cannot answer "did we ever actually tell the customer?" about anything older than a
	month. That question is exactly what an audit trail is for.

	Read-only to Support Team in the desk; written only by email._queue_mail.
	"""

	pass
