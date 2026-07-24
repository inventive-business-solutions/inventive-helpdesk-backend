# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""Only managers may write org master data — for EVERY master, not most of them.

The DocPerms grant Support Team full CRUD on all of these, so `manager_write_gate`
(registered per-doctype in hooks.py) is the only thing withholding create/write/delete
from the agent tier. The whitelisted endpoints call `_require_manager` too, but those are
not the enforcement point: an agent reaches the doctype directly through /api/resource/*.

Written table-driven and deliberately covering the whole list, because the failure mode is
a NEW master shipping without its hooks.py line. That is exactly what happened to Client
Product — it was the one master an agent could write, for as long as it existed.

`frappe.has_permission` is called WITH a doc on purpose. Without one, Frappe consults the
DocPerms only and never invokes the has_permission hooks, so every master reads as
writable and the test would pass while proving nothing.
"""
import frappe
from frappe.tests import IntegrationTestCase

AGENT = "_test.gate.agent@example.com"
MANAGER = "_test.gate.manager@example.com"
CLIENT = "_Test Gate Client"
PRODUCT = "_Test Gate Product"


def _user(email, role):
    if frappe.db.exists("User", email):
        return email
    doc = frappe.get_doc({
        "doctype": "User", "email": email, "first_name": "Gate", "last_name": role,
        "user_type": "System User", "send_welcome_email": 0,
    })
    doc.append("roles", {"role": "Support Team"})
    if role == "manager":
        doc.append("roles", {"role": "Support Manager"})
    doc.insert(ignore_permissions=True)
    return email


class TestManagerWriteGate(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = _user(AGENT, "agent")
        cls.manager = _user(MANAGER, "manager")

        def mk(payload):
            return frappe.get_doc(payload).insert(ignore_permissions=True)

        if not frappe.db.exists("Client", CLIENT):
            mk({"doctype": "Client", "client_name": CLIENT, "client_code": "TGC"})
        if not frappe.db.exists("Product", PRODUCT):
            mk({"doctype": "Product", "product_name": PRODUCT})
        division = frappe.db.get_value("Division", {"client": CLIENT, "division_code": "TGD"}) or mk(
            {"doctype": "Division", "client": CLIENT, "division_name": "Gate Div",
             "division_code": "TGD"}).name
        client_product = frappe.db.get_value("Client Product", {"client": CLIENT}) or mk(
            {"doctype": "Client Product", "client": CLIENT, "product": PRODUCT}).name
        member = frappe.db.get_value("Team Member", {"member_name": "_Test Gate Member"}) or mk(
            {"doctype": "Team Member", "member_name": "_Test Gate Member"}).name
        group = frappe.db.get_value("Assignment Group", {"group_name": "_Test Gate Group"}) or mk(
            {"doctype": "Assignment Group", "group_name": "_Test Gate Group"}).name
        poc = frappe.db.get_value("POC", {"client": CLIENT}) or mk(
            {"doctype": "POC", "poc_name": "Gate POC", "email": "_test.gate.poc@example.com",
             "client": CLIENT, "divisions": [{"division": division}]}).name

        # Every org master, with a real document for each.
        cls.masters = {
            "Client": CLIENT,
            "Division": division,
            "POC": poc,
            "Product": PRODUCT,
            "Client Product": client_product,
            "Team Member": member,
            "Assignment Group": group,
        }

    def test_agent_cannot_write_any_org_master(self):
        for doctype, name in self.masters.items():
            doc = frappe.get_doc(doctype, name)
            for ptype in ("create", "write", "delete"):
                with self.subTest(doctype=doctype, ptype=ptype):
                    self.assertFalse(
                        frappe.has_permission(doctype, ptype, doc=doc, user=self.agent),
                        f"{doctype}: an agent must not be able to {ptype} org master data — "
                        f"is it registered in hooks.py has_permission?",
                    )

    def test_agent_keeps_read_on_every_master(self):
        # The gate denies writes only. Agents need read for ticket context and for the
        # assignee/team pickers, so a gate that also blocked read would break the app.
        for doctype, name in self.masters.items():
            doc = frappe.get_doc(doctype, name)
            with self.subTest(doctype=doctype):
                self.assertTrue(frappe.has_permission(doctype, "read", doc=doc, user=self.agent))

    def test_manager_can_write_every_master(self):
        for doctype, name in self.masters.items():
            doc = frappe.get_doc(doctype, name)
            with self.subTest(doctype=doctype):
                self.assertTrue(
                    frappe.has_permission(doctype, "write", doc=doc, user=self.manager),
                    f"{doctype}: a manager must still be able to write org master data",
                )

    def test_every_master_doctype_is_registered_in_hooks(self):
        # The structural half of the same rule: catches a new master added with DocPerms
        # for Support Team but no gate, even if nobody writes a behavioural test for it.
        registered = frappe.get_hooks("has_permission") or {}
        gate = "inventive_helpdesk_backend.permissions.manager_write_gate"
        for doctype in self.masters:
            with self.subTest(doctype=doctype):
                hooks = registered.get(doctype) or []
                if isinstance(hooks, str):
                    hooks = [hooks]
                self.assertIn(
                    gate, hooks,
                    f"{doctype} is an org master but has no manager_write_gate in hooks.py",
                )
