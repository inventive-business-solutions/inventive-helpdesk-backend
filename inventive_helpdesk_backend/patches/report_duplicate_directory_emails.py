"""Report directory records that already share an email address.

Until access.assert_email_unclaimed landed, `Team Member.email` carried no uniqueness of
any kind (the doctype is named by `member_name`), so two members could hold one address.
That is not a cosmetic duplicate: a Frappe User is keyed by email, so both records point
at ONE login, and inviting the second one resets the first one's password and hands over
their account.

The new validation refuses fresh collisions but deliberately leaves existing ones
editable, so any pair created before this patch is still there. Resolving one means
deciding which human owns the login and re-inviting the other on their own address —
a judgement call, so this patch only reports.

Writes nothing and never raises: a deploy must not fail over historical data, and the
records stay usable either way.
"""
import frappe


def execute():
    for doctype in ("Team Member", "POC"):
        try:
            rows = frappe.db.sql(
                f"""
                SELECT LOWER(TRIM(email)) AS addr, COUNT(*) AS n,
                       GROUP_CONCAT(name ORDER BY creation SEPARATOR ' | ') AS holders
                FROM `tab{doctype}`
                WHERE IFNULL(TRIM(email), '') != ''
                GROUP BY LOWER(TRIM(email))
                HAVING COUNT(*) > 1
                """,
                as_dict=True,
            )
        except Exception:
            # A doctype missing on some site must not take the migration down with it.
            frappe.log_error(title=f"Duplicate-email audit failed for {doctype}")
            continue

        for row in rows:
            frappe.log_error(
                title=f"Duplicate {doctype} email: {row.addr}",
                message=(
                    f"{row.n} {doctype} records share {row.addr}: {row.holders}\n\n"
                    "They resolve to one Frappe User, so an invite to either one resets "
                    "the other's password. Decide which person owns the login, then give "
                    "the other their own address and re-invite."
                ),
            )

        if rows:
            print(f"  {len(rows)} duplicated email(s) across {doctype} — see the Error Log")


def _cross_directory_clashes():
    """Emails held by BOTH a Team Member and a POC. Kept separate from execute() so it can
    be called by hand; invite_poc/invite_member already refuse to provision these, so they
    are inert records rather than a live takeover path."""
    return frappe.db.sql(
        """
        SELECT LOWER(TRIM(tm.email)) AS addr, tm.name AS member, p.name AS poc
        FROM `tabTeam Member` tm
        JOIN `tabPOC` p ON LOWER(TRIM(p.email)) = LOWER(TRIM(tm.email))
        WHERE IFNULL(TRIM(tm.email), '') != ''
        """,
        as_dict=True,
    )
