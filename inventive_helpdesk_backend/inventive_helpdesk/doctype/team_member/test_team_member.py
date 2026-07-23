# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase


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


def _make_member(name, email, user, legacy=False):
	"""`legacy=True` skips validate to forge a duplicate-email row the way pre-existing
	production data holds one. access.assert_email_unclaimed refuses these on save now, but
	rows created before that guard are still out there, so the revocation logic has to keep
	handling them."""
	doc = frappe.get_doc({
		"doctype": "Team Member",
		"member_name": name,
		"email": email,
		"status": "Active",
		"user": user,
	})
	if legacy:
		doc.flags.ignore_validate = True
	return doc.insert(ignore_permissions=True)


class TestTeamMember(IntegrationTestCase):
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
		# Only reachable as legacy data now — see _make_member(legacy=True) — but such rows
		# exist in production, so the invariant still has to hold for them.
		email = "shared.member@example.com"
		user = _make_user(email, ["Support Team"])
		m1 = _make_member("Shared One", email, user.name, legacy=True)
		_make_member("Shared Two", email, user.name, legacy=True)

		m1.delete(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("User", email, "enabled"), 1)

	def test_duplicate_email_is_rejected(self):
		# The account-takeover guard. Two people on one address means one Frappe User, and
		# inviting the second mints a password-reset key against the FIRST one's account —
		# handing over their login. Refused at save, before any invite can be sent.
		email = "dupe.member@example.com"
		user = _make_user(email, ["Support Team"])
		_make_member("Dupe First", email, user.name)

		with self.assertRaises(frappe.ValidationError):
			_make_member("Dupe Second", email, None)

	def test_duplicate_email_is_rejected_case_insensitively(self):
		# Addresses are case-insensitive; a differently-cased duplicate resolves to the same
		# Frappe User and so must be refused too.
		email = "case.member@example.com"
		user = _make_user(email, ["Support Team"])
		_make_member("Case First", email, user.name)

		with self.assertRaises(frappe.ValidationError):
			_make_member("Case Second", email.upper(), None)

	def test_member_keeps_its_own_email_on_edit(self):
		# The guard must exclude the record being saved, or every subsequent edit of a
		# member would collide with itself.
		email = "self.member@example.com"
		user = _make_user(email, ["Support Team"])
		member = _make_member("Self Edit", email, user.name)

		member.title = "Senior Engineer"
		member.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("Team Member", member.name, "title"), "Senior Engineer")

	def test_poc_cannot_claim_a_member_email(self):
		# The two directories must not collide either: a POC on a staff address could never
		# be invited (invite_poc rejects it at the client/staff line), so it is refused at
		# save rather than left as a record that silently cannot be activated.
		from inventive_helpdesk_backend.access import assert_email_unclaimed

		email = "crossover.member@example.com"
		user = _make_user(email, ["Support Team"])
		_make_member("Crossover Member", email, user.name)

		with self.assertRaises(frappe.ValidationError):
			assert_email_unclaimed("POC", "some-new-poc", email)
