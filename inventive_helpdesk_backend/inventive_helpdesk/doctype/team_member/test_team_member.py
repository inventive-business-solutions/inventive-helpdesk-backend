# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


def _make_user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "User",
		"email": email,
		"first_name": email.split("@")[0].title(),
		"user_type": "System User",
		"send_welcome_email": 0,
		"roles": [{"role": r} for r in roles],
	}).insert(ignore_permissions=True)


def _make_member(name, email, user):
	return frappe.get_doc({
		"doctype": "Team Member",
		"member_name": name,
		"email": email,
		"status": "Active",
		"user": user,
	}).insert(ignore_permissions=True)


class TestTeamMember(FrappeTestCase):
	def test_deleting_member_disables_linked_login(self):
		email = "orphan.member@example.com"
		user = _make_user(email, ["Support Team"])
		member = _make_member("Orphan Member", email, user.name)

		member.delete(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("User", email, "enabled"), 0)

	def test_manager_login_survives_member_deletion(self):
		# A member row whose user also holds a manager role must NOT be disabled —
		# their access is independent of the directory row.
		email = "manager.member@example.com"
		user = _make_user(email, ["Support Team", "Support Manager"])
		member = _make_member("Manager Member", email, user.name)

		member.delete(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("User", email, "enabled"), 1)

	def test_login_survives_while_another_member_row_links_it(self):
		# Two member rows sharing one login: deleting one keeps the account enabled.
		email = "shared.member@example.com"
		user = _make_user(email, ["Support Team"])
		m1 = _make_member("Shared One", email, user.name)
		_make_member("Shared Two", email, user.name)

		m1.delete(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("User", email, "enabled"), 1)
