# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from inventive_helpdesk_backend.access import revoke_login_if_orphaned


class TeamMember(Document):
	def on_trash(self):
		# Deleting a member must revoke their staff login — otherwise they keep the
		# Support Team role their invite provisioned and can still sign in. Handled here
		# (not just in the delete API) so every deletion path is covered.
		revoke_login_if_orphaned(self.user, "Team Member", self.name)
