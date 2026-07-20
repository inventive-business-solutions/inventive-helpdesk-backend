# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPOC(FrappeTestCase):
    def test_division_must_belong_to_client(self):
        # Tenant isolation scopes portal users by POC.division — a POC pointing at
        # another client's division would leak that client's tickets.
        for name, code in (("_Test IC POC Client X", "ZPX"), ("_Test IC POC Client Y", "ZPY")):
            if not frappe.db.exists("Client", name):
                frappe.get_doc({"doctype": "Client", "client_name": name, "client_code": code}).insert(
                    ignore_permissions=True
                )
        div_y = frappe.db.get_value("Division", {"client": "_Test IC POC Client Y", "division_code": "ZDY"})
        if not div_y:
            div_y = frappe.get_doc({
                "doctype": "Division", "client": "_Test IC POC Client Y",
                "division_name": "Yonder", "division_code": "ZDY",
            }).insert(ignore_permissions=True).name

        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "POC", "poc_name": "Mismatched", "email": "_test.ic.mismatch@example.com",
                "client": "_Test IC POC Client X", "division": div_y,
            }).insert(ignore_permissions=True)
