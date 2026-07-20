"""Any Team Member with no linked User has no login at all, so their stored status is
misleading (created with the invite unchecked → wrongly "Active"; or an invite that
never provisioned a login → stuck "Invited"). Relabel every login-less member as
"Not Invited" so the Members list reflects reality. Members with a linked User keep
their Invited/Active status. Idempotent."""
import frappe


def execute():
    frappe.db.sql(
        """
        UPDATE `tabTeam Member`
        SET status = 'Not Invited'
        WHERE (user IS NULL OR user = '') AND status != 'Not Invited'
        """
    )
