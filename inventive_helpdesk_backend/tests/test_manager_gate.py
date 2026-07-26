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
        # The structural half of the same rule, over the hand-written list above.
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

    def test_no_agent_writable_doctype_is_left_ungated(self):
        """The same rule again, but DERIVED — and this is the one that matters.

        The test above iterates a dict maintained by hand, so it only ever checks the
        doctypes somebody remembered to add to it. That is precisely how the gap recurs:
        Client Product was missing once, and No Reply Rule was missing again afterwards,
        while this file reported the rule as fully covered both times. A structural test
        whose coverage is itself hand-maintained inherits the bug it exists to catch.

        So enumerate the app's own doctypes, find every one whose DocPerms grant Support
        Team a write of any kind, and require that each has SOME has_permission hook. A new
        doctype added with the usual copied permissions now fails here on the first run,
        without anyone thinking to extend a list.

        The exceptions are named explicitly, with the reason, so that "agents may write
        this" stays a decision someone made rather than an omission nobody noticed.
        """
        # Doctypes an agent may legitimately write, and what constrains them instead.
        deliberately_agent_writable = {
            # Scoped per agent by ticket_has_permission (assigned / own / team / triage /
            # collaborating), not by rank — working tickets IS the agent role.
            "Support Ticket": "ticket_has_permission",
            # Their own row only — own_read_receipt_gate.
            "Ticket Read Receipt": "own_read_receipt_gate",
        }

        registered = frappe.get_hooks("has_permission") or {}
        app_doctypes = frappe.get_all(
            "DocType",
            filters={"module": "Inventive Helpdesk", "istable": 0},
            pluck="name",
        )
        self.assertTrue(app_doctypes, "no app doctypes found — the filter is wrong, not the code")

        for doctype in sorted(app_doctypes):
            writes = frappe.get_all(
                "DocPerm",
                filters={"parent": doctype, "role": "Support Team"},
                fields=["`create`", "`write`", "`delete`"],
            )
            if not any(p.get("create") or p.get("write") or p.get("delete") for p in writes):
                continue  # read-only for agents; nothing to gate

            hooks = registered.get(doctype) or []
            if isinstance(hooks, str):
                hooks = [hooks]

            with self.subTest(doctype=doctype):
                self.assertTrue(
                    hooks,
                    f"{doctype} grants Support Team write in its DocPerms and has NO "
                    f"has_permission hook — an agent can create, edit and delete it through "
                    f"/api/resource/*. Add a gate in hooks.py, or, if that is intended, add "
                    f"it to deliberately_agent_writable here with the reason.",
                )
                if doctype in deliberately_agent_writable:
                    expected = deliberately_agent_writable[doctype]
                    self.assertTrue(
                        any(expected in h for h in hooks),
                        f"{doctype} is documented as agent-writable via {expected}, "
                        f"but that hook is not the one registered: {hooks}",
                    )

    def test_an_agent_cannot_write_another_agents_read_receipt(self):
        """Ticket Read Receipt is agent-writable, so the gate is ownership rather than
        rank. The receipts hold no ticket content, but the unread dot is how the team
        divides work — silently clearing a colleague's markers is a way to lose a reply."""
        receipt = frappe.get_doc({
            "doctype": "Ticket Read Receipt",
            "ticket": frappe.get_all("Support Ticket", limit=1, pluck="name")[0],
            "user": self.manager,
            "read_on": frappe.utils.now_datetime(),
        })
        self.assertFalse(
            frappe.has_permission("Ticket Read Receipt", "write", doc=receipt, user=self.agent),
            "an agent must not write a receipt belonging to someone else",
        )
        receipt.user = self.agent
        self.assertTrue(
            frappe.has_permission("Ticket Read Receipt", "write", doc=receipt, user=self.agent),
            "an agent must still write their OWN receipt, or the unread marker breaks",
        )
