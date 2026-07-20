"""Disable staff/portal logins whose backing Team Member / POC was deleted before the
on_trash revocation existed — e.g. a member removed from the Members page who kept their
Support Team login. Idempotent and re-run safe; skips manager/admin accounts. Going
forward the doctypes' on_trash hooks keep this invariant (see inventive_helpdesk_backend.access)."""
import frappe

from inventive_helpdesk_backend.access import revoke_login_if_orphaned


def execute():
    users = set(
        frappe.get_all(
            "Has Role",
            filters={"role": ["in", ["Support Team", "Support Client"]], "parenttype": "User"},
            pluck="parent",
        )
    )
    for user in users:
        # No exclude: disable only if NO Team Member or POC links this user at all.
        revoke_login_if_orphaned(user)
