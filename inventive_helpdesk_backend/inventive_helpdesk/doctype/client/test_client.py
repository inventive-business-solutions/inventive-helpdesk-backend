# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from inventive_helpdesk_backend.constants import MAX_CODE_LEN


class TestClient(FrappeTestCase):
    def test_code_over_max_length_rejected(self):
        # An unbounded client_code becomes the left half of Support Ticket's
        # tabSeries key ("{client_code}-{division_code}-") — this cap is what
        # keeps that key under tabSeries.name's varchar(100) limit.
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "Client", "client_name": "_Test IC Overlong Client",
                "client_code": "X" * (MAX_CODE_LEN + 1),
            }).insert(ignore_permissions=True)

    def test_non_alnum_code_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "Client", "client_name": "_Test IC Bad Code Client",
                "client_code": "AB-C",
            }).insert(ignore_permissions=True)

    def test_code_is_normalized_to_uppercase(self):
        doc = frappe.get_doc({
            "doctype": "Client", "client_name": "_Test IC Lowercase Client",
            "client_code": "zzz",
        }).insert(ignore_permissions=True)
        self.assertEqual(doc.client_code, "ZZZ")
