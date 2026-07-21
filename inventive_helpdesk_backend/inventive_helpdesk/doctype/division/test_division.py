# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.constants import MAX_CODE_LEN

FIXTURE_CLIENT = "_Test IC Division Fixture Client"


def _client() -> str:
    if not frappe.db.exists("Client", FIXTURE_CLIENT):
        frappe.get_doc({
            "doctype": "Client", "client_name": FIXTURE_CLIENT, "client_code": "ZDF",
        }).insert(ignore_permissions=True)
    return FIXTURE_CLIENT


class TestDivision(IntegrationTestCase):
    def test_code_over_max_length_rejected(self):
        # Mirrors Client.client_code — division_code is the right half of Support
        # Ticket's tabSeries key, so it must respect the same bound.
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "Division", "client": _client(), "division_name": "Overlong",
                "division_code": "Y" * (MAX_CODE_LEN + 1),
            }).insert(ignore_permissions=True)

    def test_non_alnum_code_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({
                "doctype": "Division", "client": _client(), "division_name": "BadCode",
                "division_code": "A_B",
            }).insert(ignore_permissions=True)

    def test_code_is_normalized_to_uppercase(self):
        doc = frappe.get_doc({
            "doctype": "Division", "client": _client(), "division_name": "Lowercase",
            "division_code": "low",
        }).insert(ignore_permissions=True)
        self.assertEqual(doc.division_code, "LOW")
