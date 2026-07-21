# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.install import ensure_roles


class TestEnsureRoles(IntegrationTestCase):
    """Guards the role spec in install.py.

    These exist because the spec was silently not applied for the whole life of the
    app: ensure_roles only created a role when it was missing, and Frappe auto-creates
    a missing role with desk_access 1 as soon as a DocPerm references it during
    migrate. Support Client was therefore stuck with desk access despite being
    declared with none, and clients could open /app and list every User on the site.
    """

    def test_support_client_has_no_desk_access(self):
        # Desk access is not just the /app route. It sets User.user_type, which drives
        # the automatic "Desk User" role, which core doctypes — User and File among
        # them — use to decide what a session may read.
        self.assertEqual(frappe.db.get_value("Role", "Support Client", "desk_access"), 0)

    def test_staff_roles_keep_desk_access(self):
        for role in ("Support Team", "Support Manager"):
            with self.subTest(role=role):
                self.assertEqual(frappe.db.get_value("Role", role, "desk_access"), 1)

    def test_ensure_roles_corrects_an_existing_role(self):
        # The actual regression. Creating-only left drift in place forever, so this
        # asserts the repair path rather than just the end state: put the role back
        # into the broken shape and confirm ensure_roles pulls it into line.
        frappe.db.set_value("Role", "Support Client", "desk_access", 1)
        self.assertEqual(frappe.db.get_value("Role", "Support Client", "desk_access"), 1)

        ensure_roles()

        self.assertEqual(frappe.db.get_value("Role", "Support Client", "desk_access"), 0)

    def test_a_client_only_user_is_retyped_to_website_user(self):
        # Fixing the role is not enough on its own: users created while it still
        # granted desk access stay System Users, and keep the access, until something
        # re-evaluates them. ensure_roles does that for users holding this app's roles.
        email = "_test_ensure_roles_poc@example.com"
        if not frappe.db.exists("User", email):
            user = frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": "Ensure",
                "last_name": "Roles",
                "send_welcome_email": 0,
                "user_type": "Website User",
            })
            user.append("roles", {"role": "Support Client"})
            user.flags.ignore_password_policy = True
            user.insert(ignore_permissions=True)

        # Reproduce the broken state: role grants desk access, user promoted to match.
        frappe.db.set_value("Role", "Support Client", "desk_access", 1)
        frappe.db.set_value("User", email, "user_type", "System User")

        ensure_roles()

        self.assertEqual(frappe.db.get_value("User", email, "user_type"), "Website User")

    def test_a_user_with_a_desk_role_is_left_alone(self):
        # Managers hold Support Client's opposite number alongside it. Re-evaluation
        # must not sweep them out of the desk: one desk-access role is enough.
        email = "_test_ensure_roles_manager@example.com"
        if not frappe.db.exists("User", email):
            user = frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": "Ensure",
                "last_name": "Manager",
                "send_welcome_email": 0,
                "user_type": "System User",
            })
            user.append("roles", {"role": "Support Team"})
            user.append("roles", {"role": "Support Manager"})
            user.flags.ignore_password_policy = True
            user.insert(ignore_permissions=True)

        frappe.db.set_value("Role", "Support Client", "desk_access", 1)

        ensure_roles()

        self.assertEqual(frappe.db.get_value("User", email, "user_type"), "System User")
