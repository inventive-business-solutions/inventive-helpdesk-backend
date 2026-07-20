"""Install/migrate helpers for Inventive Helpdesk."""
import frappe

# The app's custom roles. Shipped in code (idempotent, runs after install and
# every migrate) so a fresh site always has them before DocPerms reference them.
ROLES = (
    # Base staff role — "agents": work tickets, read-only on org masters.
    {"role_name": "Support Team", "desk_access": 1},
    # Manager tier — additionally manage clients, POCs, members, teams, products.
    # Granted on top of Support Team (an owner also holds System Manager).
    {"role_name": "Support Manager", "desk_access": 1},
    {"role_name": "Support Client", "desk_access": 0},
)


def ensure_roles():
    for spec in ROLES:
        if not frappe.db.exists("Role", spec["role_name"]):
            frappe.get_doc({"doctype": "Role", **spec}).insert(ignore_permissions=True)
