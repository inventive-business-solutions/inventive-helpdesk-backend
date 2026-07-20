# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from inventive_helpdesk_backend.access import revoke_login_if_orphaned


class POC(Document):
    def validate(self):
        # Cross-field integrity: a POC's division must belong to its client —
        # tenant isolation scopes portal users by POC.division, so a mismatched
        # pair would scope a user to another client's division.
        if self.client and self.division:
            div_client = frappe.db.get_value("Division", self.division, "client")
            if div_client != self.client:
                frappe.throw(
                    _("Division {0} belongs to {1}, not {2}").format(self.division, div_client, self.client)
                )

    def on_trash(self):
        # Deleting a POC (directly, or cascaded when its division/client is removed)
        # revokes the portal login — unless the same person still covers another
        # division, in which case their account stays active.
        revoke_login_if_orphaned(self.user, "POC", self.name)
