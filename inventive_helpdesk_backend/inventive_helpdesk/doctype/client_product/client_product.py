# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ClientProduct(Document):
    def validate(self):
        # An empty `divisions` table is meaningful, not missing data: it means the product is
        # attached to the client as a whole. That is the only shape available to a client with
        # no divisions yet, so it must stay legal.
        seen = set()
        for row in self.divisions or []:
            if not row.division:
                continue
            # Same integrity rule POC.validate enforces: a division must belong to this
            # client, or the product would show up under someone else's tenant.
            div_client = frappe.db.get_value("Division", row.division, "client")
            if div_client != self.client:
                frappe.throw(
                    _("Division {0} belongs to {1}, not {2}").format(row.division, div_client, self.client)
                )
            if row.division in seen:
                frappe.throw(_("{0} is listed twice on this product").format(row.division))
            seen.add(row.division)
