"""Login-revocation invariant for Inventive Helpdesk.

A staff member (Team Member) or client contact (POC) is invited by provisioning a
Frappe User (see api.invite_member / api.invite_poc). When their backing record is
deleted they must lose that login — otherwise a removed person can still sign in.
This module centralises that revocation so every deletion path (the whitelisted API,
the raw REST resource endpoint, and the desk UI) enforces it via the doctypes'
on_trash hooks, and a one-time patch closes the historical gap.
"""
import frappe

# Imported explicitly: `frappe` does not import its own `sessions` submodule, so
# `frappe.sessions` only resolves if something else happened to import it first. That
# holds for a web request (frappe.handler pulls it in) but NOT on the bench migrate path
# — where patches/disable_orphaned_logins.py calls this — so the attribute access would
# AttributeError the first time a patch actually revoked a login.
from frappe.sessions import clear_sessions

from inventive_helpdesk_backend.permissions import MANAGER_ROLES


def revoke_login_if_orphaned(user, exclude_doctype=None, exclude_name=None):
    """Disable `user` and end its live sessions once nothing links it anymore.

    Called from POC.on_trash and TeamMember.on_trash. A re-used account — the same
    person as a POC for two divisions, or on two Team Member rows — stays enabled while
    any other record still links it. Manager/admin accounts are never disabled: their
    access is independent of a directory row. `exclude_*` skips the record currently
    being deleted, whose row may still exist while on_trash runs.
    """
    if not user or not frappe.db.exists("User", user):
        return

    # Never lock out a privileged account that merely also held a directory row.
    if set(frappe.get_roles(user)) & MANAGER_ROLES:
        return

    poc_filters = {"user": user}
    member_filters = {"user": user}
    if exclude_doctype == "POC":
        poc_filters["name"] = ["!=", exclude_name]
    elif exclude_doctype == "Team Member":
        member_filters["name"] = ["!=", exclude_name]

    if frappe.db.count("POC", poc_filters) or frappe.db.count("Team Member", member_filters):
        return  # still backed by another record — keep the login active

    # Disable the login AND invalidate any outstanding invite / password-reset key: a
    # still-valid set-password link would otherwise let a removed user set a password and
    # be signed in, because Frappe's update_password/login_as never re-check `enabled`.
    # Clearing the key makes that link resolve to "used before or invalid". Also end any
    # live session so an already-signed-in user loses access immediately.
    updates = {}
    if frappe.db.get_value("User", user, "enabled"):
        updates["enabled"] = 0
    if frappe.db.get_value("User", user, "reset_password_key"):
        updates["reset_password_key"] = ""
    if updates:
        frappe.db.set_value("User", user, updates)
        clear_sessions(user, force=True)
