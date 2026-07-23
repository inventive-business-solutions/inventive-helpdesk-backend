"""Identity invariants for Inventive Helpdesk.

A staff member (Team Member) or client contact (POC) is invited by provisioning a
Frappe User (see api.invite_member / api.invite_poc). Two rules hold this together:

1. **One email, one person.** A Frappe User is keyed by email, so two directory records
   sharing an address resolve to the SAME login — and inviting the second one mints a
   password-reset key against the first one's account. See assert_email_unclaimed.
2. **A deleted record loses its login.** Otherwise a removed person can still sign in.
   See revoke_login_if_orphaned.

Both are enforced at the doctype hook level, so every path (the whitelisted API, the raw
REST resource endpoint, and the desk UI) is covered rather than just the app's own forms.
"""
import frappe
from frappe import _

# Imported explicitly: `frappe` does not import its own `sessions` submodule, so
# `frappe.sessions` only resolves if something else happened to import it first. That
# holds for a web request (frappe.handler pulls it in) but NOT on the bench migrate path
# — where patches/disable_orphaned_logins.py calls this — so the attribute access would
# AttributeError the first time a patch actually revoked a login.
from frappe.sessions import clear_sessions

from inventive_helpdesk_backend.permissions import MANAGER_ROLES


# Directory doctypes that can own a login, with the noun to use when reporting a clash.
_DIRECTORY = (("Team Member", "team member"), ("POC", "client contact"))


def assert_email_unclaimed(doctype, name, email):
    """Refuse an email address that another person's directory record already holds.

    A Frappe User's docname IS its email, so two records sharing an address are two
    people pointing at ONE login. api._ensure_login_user then re-uses that account and
    api._send_invite_mail resets its password — handing the second person control of the
    first person's login, including any elevated roles it carries.

    `Team Member.email` carries no unique index (it is named by `member_name`), so nothing
    below this stopped the collision; `POC` is already unique via `autoname: field:email`,
    but is checked here too so the two directories cannot collide with each other.

    Comparison is a plain `=`, which is case-insensitive under the site's `..._ci`
    collation — matching how POC's own unique index already behaves.
    """
    email = (email or "").strip()
    if not email:
        return

    for dt, noun in _DIRECTORY:
        filters = {"email": email}
        if dt == doctype:
            filters["name"] = ("!=", name)
        holder = frappe.db.get_value(dt, filters, "name")
        if holder:
            frappe.throw(
                _("{0} is already used by the {1} “{2}”. Each person needs their own email address — sharing one would let either of them reset the other's password.").format(
                    email, noun, holder
                ),
                title=_("Email already in use"),
            )


def assert_user_unclaimed(user, doctype, name):
    """Refuse to hand an existing login to a record that is not already linked to it.

    Defence in depth behind assert_email_unclaimed: that guard runs in `validate`, which
    `ignore_validate`/`db_set` paths can skip, and it cannot see a User that no directory
    record points at yet (a desk account created by hand, say). Provisioning re-uses an
    account by design — a resend must not mint a second one — so the test is not "does
    this User exist" but "does it already belong to somebody else".
    """
    if not user:
        return
    for dt, noun in _DIRECTORY:
        filters = {"user": user}
        if dt == doctype:
            filters["name"] = ("!=", name)
        holder = frappe.db.get_value(dt, filters, "name")
        if holder:
            frappe.throw(
                _("The login {0} already belongs to the {1} “{2}”. Use a different email address.").format(
                    user, noun, holder
                ),
                title=_("Login already in use"),
            )


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
