# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from inventive_helpdesk_backend.access import assert_email_unclaimed, revoke_login_if_orphaned


class TeamMember(Document):
	def validate(self):
		# Two members sharing an email resolve to ONE Frappe User, and inviting the second
		# resets the first one's password — an account takeover, not just a duplicate row.
		# Team Member is named by `member_name`, so the schema never stopped this.
		#
		# Only checked when the address is actually being set or changed: sites that
		# already carry a historical duplicate must stay editable (renaming, re-assigning,
		# deleting one of the pair) instead of failing every save until someone untangles
		# them. New collisions are refused outright.
		if self.email and (self.is_new() or self.has_value_changed("email")):
			assert_email_unclaimed(self.doctype, self.name, self.email)

	def on_trash(self):
		# Deleting a member must revoke their staff login — otherwise they keep the
		# Support Team role their invite provisioned and can still sign in. Handled here
		# (not just in the delete API) so every deletion path is covered.
		revoke_login_if_orphaned(self.user, "Team Member", self.name)
