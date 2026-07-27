# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""Who may hand out admin access, and what they are stopped from doing with it.

Delegation is the one feature where the refusals matter more than the happy path. Two
things must hold no matter what the UI does:

  1. Admin cannot spread on its own. A delegated admin gets the full manager surface, so
     the only thing standing between them and promoting anyone else is that this endpoint
     refuses them — not a hidden button.
  2. Nobody can remove the access that lets them fix a mistake. Revoking your own admin,
     or an owner's, are both routes to a site nobody can administer.

Owner = System Manager/Administrator. Deliberately not a new role: that population was
already unconditionally manager-tier so the site owner could never be locked out, which is
exactly the group entitled to grant access to others.
"""
import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.api import list_admins, set_member_admin

OWNER = "_test.deleg.owner@example.test"
AGENT = "_test.deleg.agent@example.test"
DELEGATE = "_test.deleg.delegate@example.test"


def _user(email, roles):
    if not frappe.db.exists("User", email):
        frappe.get_doc(
            {"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
        ).insert(ignore_permissions=True)
    u = frappe.get_doc("User", email)
    have = {r.role for r in u.roles}
    for r in roles:
        if r not in have:
            u.append("roles", {"role": r})
    u.save(ignore_permissions=True)
    return email


def _member(name, email):
    existing = frappe.db.get_value("Team Member", {"email": email}, "name")
    if existing:
        frappe.db.set_value("Team Member", existing, "user", email)
        return existing
    doc = frappe.get_doc(
        {"doctype": "Team Member", "member_name": name, "email": email, "user": email, "status": "Active"}
    ).insert(ignore_permissions=True)
    return doc.name


class TestAdminDelegation(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        _user(OWNER, ["Support Team", "System Manager"])
        _user(AGENT, ["Support Team"])
        _user(DELEGATE, ["Support Team"])
        self.owner_m = _member("Deleg Owner", OWNER)
        self.agent_m = _member("Deleg Agent", AGENT)
        self.deleg_m = _member("Deleg Delegate", DELEGATE)
        # These tests share a database and each grants roles, so a later test would
        # otherwise inherit whatever an earlier one left behind. Reset the two mutable
        # accounts to a known baseline rather than depending on execution order.
        for email in (AGENT, DELEGATE):
            u = frappe.get_doc("User", email)
            if any(r.role == "Support Manager" for r in u.roles):
                u.roles = [r for r in u.roles if r.role != "Support Manager"]
                u.save(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        frappe.set_user("Administrator")

    # ---- the point of the feature ----------------------------------------
    def test_an_owner_can_grant_and_revoke(self):
        frappe.set_user(OWNER)
        set_member_admin(self.agent_m, True)
        self.assertIn("Support Manager", frappe.get_roles(AGENT))
        set_member_admin(self.agent_m, False)
        self.assertNotIn("Support Manager", frappe.get_roles(AGENT))

    def test_granting_twice_is_a_no_op_rather_than_a_duplicate_role(self):
        frappe.set_user(OWNER)
        set_member_admin(self.agent_m, True)
        second = set_member_admin(self.agent_m, True)
        self.assertFalse(second["changed"])
        roles = [r.role for r in frappe.get_doc("User", AGENT).roles]
        self.assertEqual(roles.count("Support Manager"), 1)

    # ---- escalation ------------------------------------------------------
    def test_a_delegated_admin_cannot_promote_anyone(self):
        """The whole two-tier model rests on this."""
        frappe.set_user(OWNER)
        set_member_admin(self.deleg_m, True)
        frappe.set_user(DELEGATE)
        with self.assertRaises(frappe.PermissionError):
            set_member_admin(self.agent_m, True)
        self.assertNotIn("Support Manager", frappe.get_roles(AGENT))

    def test_a_plain_agent_cannot_promote_anyone(self):
        frappe.set_user(AGENT)
        with self.assertRaises(frappe.PermissionError):
            set_member_admin(self.deleg_m, True)

    def test_a_delegated_admin_cannot_even_read_the_console(self):
        frappe.set_user(OWNER)
        set_member_admin(self.deleg_m, True)
        frappe.set_user(DELEGATE)
        with self.assertRaises(frappe.PermissionError):
            list_admins()

    # ---- lockout ---------------------------------------------------------
    def test_you_cannot_revoke_your_own_admin(self):
        frappe.set_user(OWNER)
        with self.assertRaises(frappe.ValidationError):
            set_member_admin(self.owner_m, False)

    def test_an_owners_access_is_not_managed_here(self):
        other = _user("_test.deleg.owner2@example.test", ["Support Team", "System Manager"])
        m = _member("Deleg Owner2", other)
        frappe.set_user(OWNER)
        with self.assertRaises(frappe.ValidationError):
            set_member_admin(m, False)
        self.assertIn("System Manager", frappe.get_roles(other))

    def test_a_member_with_no_account_cannot_be_promoted(self):
        # Reuse if a previous run left it: these tests share a database.
        name = frappe.db.exists("Team Member", {"email": "_test.deleg.ni@example.test"})
        if not name:
            name = frappe.get_doc(
                {"doctype": "Team Member", "member_name": "Never Invited",
                 "email": "_test.deleg.ni@example.test", "status": "Not Invited"}
            ).insert(ignore_permissions=True).name
        frappe.set_user(OWNER)
        with self.assertRaises(frappe.ValidationError):
            set_member_admin(name, True)

    # ---- the console -----------------------------------------------------
    def test_list_admins_reports_each_members_tier(self):
        frappe.set_user(OWNER)
        set_member_admin(self.deleg_m, True)
        by_email = {r["email"]: r for r in list_admins()}
        self.assertTrue(by_email[DELEGATE]["is_admin"])
        self.assertFalse(by_email[DELEGATE]["is_owner"])
        self.assertTrue(by_email[OWNER]["is_owner"])
        self.assertFalse(by_email[AGENT]["is_admin"])
