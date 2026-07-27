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

from inventive_helpdesk_backend.api import (
    admin_candidates,
    invite_admin,
    list_admins,
    revoke_account,
    set_member_admin,
)

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
    def test_list_admins_shows_only_people_who_hold_admin(self):
        """The list answers "who can manage this org". A plain agent is not an answer to
        that question and padding the list with them makes it one you have to search."""
        frappe.set_user(OWNER)
        set_member_admin(self.deleg_m, True)
        by_email = {r["email"]: r for r in list_admins()}
        self.assertIn(DELEGATE, by_email)  # delegated admin
        self.assertIn(OWNER, by_email)  # lead admin
        self.assertNotIn(AGENT, by_email)  # plain agent — excluded
        self.assertTrue(by_email[DELEGATE]["is_admin"])
        self.assertFalse(by_email[DELEGATE]["is_owner"])
        self.assertTrue(by_email[OWNER]["is_owner"])

    def test_a_revoked_admin_leaves_the_list(self):
        frappe.set_user(OWNER)
        set_member_admin(self.deleg_m, True)
        self.assertIn(DELEGATE, {r["email"] for r in list_admins()})
        set_member_admin(self.deleg_m, False)
        self.assertNotIn(DELEGATE, {r["email"] for r in list_admins()})

    def test_candidates_are_exactly_who_can_be_promoted(self):
        frappe.set_user(OWNER)
        emails = {r["email"] for r in admin_candidates()}
        self.assertIn(AGENT, emails)  # promotable
        self.assertNotIn(OWNER, emails)  # yourself
        self.assertNotIn("_test.deleg.ni@example.test", emails)  # no linked account
        set_member_admin(self.deleg_m, True)
        self.assertNotIn(DELEGATE, {r["email"] for r in admin_candidates()})  # already admin

    def test_candidates_is_owner_only(self):
        frappe.set_user(AGENT)
        with self.assertRaises(frappe.PermissionError):
            admin_candidates()


class TestInviteAdmin(IntegrationTestCase):
    """Inviting someone straight in as an Administrator.

    An administrator is not necessarily an agent — the person running the org may never
    work a ticket — so requiring them to join a team first would be a step that exists
    only because staff logins hang off Team Member.
    """

    def setUp(self):
        frappe.set_user("Administrator")
        _user(OWNER, ["Support Team", "System Manager"])
        _user(AGENT, ["Support Team"])
        _member("Deleg Owner", OWNER)
        frappe.db.commit()

    def tearDown(self):
        frappe.set_user("Administrator")

    def test_a_brand_new_person_becomes_an_administrator(self):
        frappe.set_user(OWNER)
        email = "_test.fresh.admin@example.test"
        out = invite_admin("Fresh Admin", email)
        self.assertTrue(out["member"])
        self.assertIn("Support Manager", frappe.get_roles(out["user"]))
        # And they show up in the console, which lists only people who hold access.
        self.assertIn(email, {r["email"] for r in list_admins()})

    def test_inviting_the_same_address_twice_re_invites_rather_than_failing(self):
        frappe.set_user(OWNER)
        email = "_test.fresh.twice@example.test"
        first = invite_admin("Twice Over", email)
        second = invite_admin("Twice Over", email)
        self.assertEqual(first["member"], second["member"])
        # The second call must not re-provision: invite_member refuses an address that
        # already holds Support Manager, so re-running it would fail on its own success.
        self.assertTrue(first["email_sent"] or first["user"])
        self.assertFalse(second["email_sent"])
        self.assertEqual(
            frappe.db.count("Team Member", {"email": email}), 1, "must not create a duplicate member"
        )

    def test_only_a_lead_administrator_may_invite_one(self):
        frappe.set_user(AGENT)
        with self.assertRaises(frappe.PermissionError):
            invite_admin("Sneaky", "_test.fresh.sneaky@example.test")

    def test_a_malformed_address_is_refused_before_anything_is_created(self):
        frappe.set_user(OWNER)
        with self.assertRaises(frappe.ValidationError):
            invite_admin("No Address", "not-an-email")
        self.assertFalse(frappe.db.exists("Team Member", {"member_name": "No Address"}))


class TestRevokeAccount(IntegrationTestCase):
    """Removing someone from the system, as opposed to demoting them.

    Deleting a Team Member only ever removed the record — the User stayed enabled with its
    roles, so the person could sign back in and land in the app with no member link. That
    reads as a broken account rather than a closed one, and it is the difference between
    bookkeeping and actually revoking access.
    """

    def setUp(self):
        frappe.set_user("Administrator")
        _user(OWNER, ["Support Team", "System Manager"])
        _user(AGENT, ["Support Team"])
        self.owner_m = _member("Deleg Owner", OWNER)
        self.agent_m = _member("Deleg Agent", AGENT)
        frappe.db.get_value("User", AGENT, "enabled") or frappe.db.set_value("User", AGENT, "enabled", 1)
        frappe.db.set_value("User", AGENT, "enabled", 1)
        frappe.db.commit()

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.set_value("User", AGENT, "enabled", 1)
        frappe.db.commit()

    def test_the_login_is_disabled_not_merely_unlinked(self):
        frappe.set_user(OWNER)
        out = revoke_account(self.agent_m)
        self.assertTrue(out["disabled"])
        self.assertEqual(frappe.db.get_value("User", AGENT, "enabled"), 0)

    def test_app_roles_are_stripped_so_re_enabling_is_a_deliberate_re_grant(self):
        frappe.set_user(OWNER)
        revoke_account(self.agent_m)
        roles = frappe.get_roles(AGENT)
        self.assertNotIn("Support Team", roles)
        self.assertNotIn("Support Manager", roles)

    def test_live_sessions_are_ended_rather_than_left_to_expire(self):
        frappe.db.delete("Sessions", {"user": AGENT})
        frappe.get_doc({"doctype": "Sessions", "user": AGENT, "sid": "_test_sid_revoke",
                        "status": "Active"}).insert(ignore_permissions=True) if frappe.db.exists("DocType", "Sessions") else None
        frappe.set_user(OWNER)
        revoke_account(self.agent_m)
        self.assertEqual(frappe.db.count("Sessions", {"user": AGENT}), 0)

    def test_you_cannot_remove_your_own_account(self):
        frappe.set_user(OWNER)
        with self.assertRaises(frappe.ValidationError):
            revoke_account(self.owner_m)
        self.assertEqual(frappe.db.get_value("User", OWNER, "enabled"), 1)

    def test_only_a_lead_administrator_may_do_it(self):
        frappe.set_user(AGENT)
        with self.assertRaises(frappe.PermissionError):
            revoke_account(self.owner_m)
