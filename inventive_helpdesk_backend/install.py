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
    # Clients belong in the portal, never the desk. Enforced on every migrate,
    # not just at creation — see ensure_roles for why that distinction matters.
    {"role_name": "Support Client", "desk_access": 0},
)

# Frappe's own two user types. A site may define custom ones via the User Type
# doctype, and those carry their own role/module rules, so we leave them alone.
_STANDARD_USER_TYPES = ("System User", "Website User")


def ensure_roles():
    """Create the app's roles, and hold existing ones to the spec above.

    Creating-only was not enough. Frappe auto-creates a missing role with its own
    defaults (desk_access = 1) as soon as a DocPerm references it, which happens
    during migrate. Such a role already exists by the time this runs, so the
    declared desk_access = 0 for Support Client never applied — on any site, and
    never would have, because nothing here updated an existing row.

    That was not cosmetic. Desk access makes Frappe classify a portal user as a
    System User, which carries read access to core doctypes: a client POC could
    open /app and list every User on the site, including staff addresses and
    other clients' POCs. The app's own hooks correctly scoped Support Ticket,
    Client, Division, POC and Team Member — but User is Frappe's, not ours.
    """
    corrected = []

    for spec in ROLES:
        role_name = spec["role_name"]
        wanted = {k: v for k, v in spec.items() if k != "role_name"}

        if not frappe.db.exists("Role", role_name):
            frappe.get_doc({"doctype": "Role", **spec}).insert(ignore_permissions=True)
            continue

        drift = {
            field: value
            for field, value in wanted.items()
            if frappe.db.get_value("Role", role_name, field) != value
        }
        if not drift:
            continue

        # Written straight to the database rather than through role.save().
        #
        # Role.on_update reacts to a desk_access change by re-typing every user
        # holding the role, and on Frappe 16.27.1 it does so via
        # frappe.get_lazy_doc("User", ...). Saving that lazy document queues an
        # after-commit job whose kwargs contain the LazyUser object, which cannot
        # be pickled — so frappe.db.commit() raises PicklingError and takes the
        # whole migrate down with it. Since the stack runs migrate on every
        # deploy, that would turn this fix into a broken release.
        #
        # _resync_user_types below does the same re-evaluation safely.
        for field, value in drift.items():
            frappe.db.set_value("Role", role_name, field, value, update_modified=False)
        corrected.append((role_name, drift))

    if corrected:
        frappe.clear_cache()
        retyped = _resync_user_types()
        for role_name, drift in corrected:
            frappe.logger().info(f"ensure_roles: corrected Role {role_name} {drift}")
        for user, before, after in retyped:
            frappe.logger().info(f"ensure_roles: retyped User {user} {before} -> {after}")


def _resync_user_types():
    """Re-apply Frappe's own rule to users holding this app's roles.

    Mirrors User.set_system_user: a user is a System User if any single role they
    hold has desk_access, otherwise a Website User. Only users carrying one of our
    roles are considered — this is a targeted correction, not a site-wide sweep.

    Standard users are skipped because Frappe fixes their type regardless, and
    users on a custom User Type are skipped because that doctype drives its own
    role and module assignment.
    """
    role_names = [spec["role_name"] for spec in ROLES]
    affected = frappe.get_all(
        "Has Role",
        filters={"role": ["in", role_names], "parenttype": "User"},
        pluck="parent",
        distinct=True,
    )

    desk_roles = set(frappe.get_all("Role", filters={"desk_access": 1}, pluck="name"))
    changed = []

    for user in affected:
        if user in ("Administrator", "Guest"):
            continue

        current = frappe.db.get_value("User", user, "user_type")
        if current not in _STANDARD_USER_TYPES:
            continue

        held = set(
            frappe.get_all(
                "Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role"
            )
        )
        wanted = "System User" if (held & desk_roles) else "Website User"
        if wanted != current:
            frappe.db.set_value("User", user, "user_type", wanted, update_modified=False)
            changed.append((user, current, wanted))

    return changed
