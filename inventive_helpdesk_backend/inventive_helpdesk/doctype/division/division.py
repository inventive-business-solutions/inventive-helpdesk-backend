# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from inventive_helpdesk_backend.constants import MAX_CODE_LEN


class Division(Document):
    def validate(self):
        if not self.division_code:
            return
        code = self.division_code.strip().upper()
        if not code.isalnum():
            frappe.throw(_("Division code must contain only letters and numbers"))
        if len(code) > MAX_CODE_LEN:
            frappe.throw(_("Division code must be {0} characters or fewer").format(MAX_CODE_LEN))
        self.division_code = code
