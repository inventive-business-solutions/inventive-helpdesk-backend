# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""`delete_product` must name the real blocker, not whichever link Frappe hit first.

The bug this covers: a product with no client engagements sat in the Products page's
"Unassigned" tab and refused to delete, reporting that it was linked with a Client. It
was — through `Client.product`, a superseded single Link that no screen displayed and
that only a migration ever wrote. The page derives "assigned" from Client Product rows
alone, so the two disagreed and the message pointed at a relationship the user could not
see, let alone remove.

That field is gone now (see patches/clear_legacy_client_product). What these tests hold in
place is the property that outlived it: the delete rule is owned by the server, spans
EVERY Link field pointing at Product, and each refusal says which one applies and what to
do about it. Tickets are a permanent block; engagements are a fixable one. A generic
"cannot delete, it is linked" would pass a weaker test and still leave the user stuck.

The message text is asserted, not just the raise. A refusal whose wording does not
identify the blocker is the failure being tested — an exception alone is not the fix.
"""

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.api import delete_product

MANAGER = "_test.delprod.manager@example.com"
AGENT = "_test.delprod.agent@example.com"
CLIENT = "_Test DelProd Client"


def _user(email, roles):
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "DelProd",
            "user_type": "System User", "send_welcome_email": 0,
        }).insert(ignore_permissions=True)
    # Roles re-ensured every run: IntegrationTestCase leaves users behind, so assigning
    # them only on creation would pin the identity to whatever the first run produced.
    doc = frappe.get_doc("User", email)
    have = {r.role for r in doc.roles}
    for role in roles:
        if role not in have:
            doc.append("roles", {"role": role})
    doc.save(ignore_permissions=True)
    return email


def _product(name):
    if not frappe.db.exists("Product", name):
        frappe.get_doc({"doctype": "Product", "product_name": name}).insert(ignore_permissions=True)
    return name


class TestDeleteProduct(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = _user(MANAGER, ["Support Team", "Support Manager"])
        cls.agent = _user(AGENT, ["Support Team"])
        if not frappe.db.exists("Client", CLIENT):
            frappe.get_doc({
                "doctype": "Client", "client_name": CLIENT, "client_code": "TDP",
            }).insert(ignore_permissions=True)
        cls.division = frappe.db.get_value("Division", {"client": CLIENT, "division_code": "TDD"})
        if not cls.division:
            cls.division = frappe.get_doc({
                "doctype": "Division", "client": CLIENT, "division_name": "DelProd Div",
                "division_code": "TDD",
            }).insert(ignore_permissions=True).name

    def setUp(self):
        frappe.set_user(self.manager)

    def tearDown(self):
        frappe.set_user("Administrator")

    def test_unlinked_product_is_deleted(self):
        """The baseline. Without this the other two could pass on a blanket refusal."""
        name = _product("_Test DelProd Free")
        delete_product(name)
        self.assertFalse(frappe.db.exists("Product", name))

    def test_product_on_a_ticket_is_refused_and_says_so(self):
        name = _product("_Test DelProd Ticketed")
        frappe.get_doc({
            "doctype": "Support Ticket", "title": "delprod fixture", "description": "x",
            "ticket_type": "Query", "priority": "Low", "status": "New",
            "client": CLIENT, "division": self.division, "product": name,
        }).insert(ignore_permissions=True)

        with self.assertRaises(frappe.ValidationError) as caught:
            delete_product(name)
        # Names the product and the count — enough to understand it without going digging.
        self.assertIn(name, str(caught.exception))
        self.assertIn("ticket", str(caught.exception).lower())
        self.assertTrue(frappe.db.exists("Product", name))

    def test_product_on_an_engagement_names_the_client(self):
        name = _product("_Test DelProd Engaged")
        frappe.get_doc({
            "doctype": "Client Product", "client": CLIENT, "product": name,
        }).insert(ignore_permissions=True)

        with self.assertRaises(frappe.ValidationError) as caught:
            delete_product(name)
        # The client's NAME, specifically: "still assigned to a client" was the useless
        # half of the original bug. The user has to know which client to go and edit.
        self.assertIn(CLIENT, str(caught.exception))
        self.assertTrue(frappe.db.exists("Product", name))

    def test_agent_cannot_delete(self):
        """_require_manager, not the DocPerms — an agent holds delete on Product."""
        name = _product("_Test DelProd Guarded")
        frappe.set_user(self.agent)
        with self.assertRaises(frappe.PermissionError):
            delete_product(name)
        self.assertTrue(frappe.db.exists("Product", name))
