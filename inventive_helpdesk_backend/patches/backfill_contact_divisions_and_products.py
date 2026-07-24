"""Move to the divisions-table / Client-Product model without losing anything.

Three backfills, all idempotent so a re-run (or a half-finished migrate) is safe:

1. `POC.division` (single Link) -> the `POC Division` child table. This one is
   load-bearing: permissions._poc now reads ONLY the table, so a POC missed here loses
   access to their own tickets until someone re-adds them by hand.
2. `Client.product` -> a `Client Product` row with no divisions, i.e. attached to the
   client as a whole, which is what a single client-wide product meant.
3. `Client.status` -> "Active" for every existing client. They are all long past
   onboarding; the field defaults to "Onboarding", which would be wrong for them.

The old columns are deliberately left populated. Dropping them in the same release as the
readers that replace them would make a rollback lossy — they come out later, once the new
model has run in production.
"""
import frappe


def execute():
    _backfill_poc_divisions()
    _backfill_client_products()
    _backfill_client_status()


def _backfill_poc_divisions():
    rows = frappe.db.sql(
        """
        SELECT p.name, p.division
        FROM `tabPOC` p
        WHERE IFNULL(p.division, '') != ''
          AND NOT EXISTS (
            SELECT 1 FROM `tabPOC Division` d
            WHERE d.parent = p.name AND d.parenttype = 'POC' AND d.division = p.division
          )
        """,
        as_dict=True,
    )
    for row in rows:
        # db_insert rather than doc.append + save: saving would re-run POC.validate, and a
        # site carrying a legacy duplicate email would abort the whole migration on a row
        # that is not what we are here to fix.
        frappe.get_doc({
            "doctype": "POC Division",
            "parent": row.name,
            "parenttype": "POC",
            "parentfield": "divisions",
            "division": row.division,
            "idx": 1,
        }).db_insert()
    if rows:
        print(f"  moved {len(rows)} POC division link(s) into the divisions table")


def _backfill_client_products():
    rows = frappe.db.sql(
        """
        SELECT c.name, c.product
        FROM `tabClient` c
        WHERE IFNULL(c.product, '') != ''
          AND NOT EXISTS (
            SELECT 1 FROM `tabClient Product` cp
            WHERE cp.client = c.name AND cp.product = c.product
          )
        """,
        as_dict=True,
    )
    for row in rows:
        doc = frappe.get_doc({
            "doctype": "Client Product",
            "client": row.name,
            "product": row.product,
            # No divisions: the old single field meant "this client runs this product",
            # which is exactly what an empty division list means now.
            "divisions": [],
        })
        doc.insert(ignore_permissions=True)
    if rows:
        print(f"  created {len(rows)} Client Product row(s) from Client.product")


def _backfill_client_status():
    """Stamp pre-existing clients as Active.

    They cannot be detected by a blank status: adding the column stamped every row with the
    field default ("Onboarding"), so there is nothing empty to find. Every client that
    existed before this patch is therefore indistinguishable from a genuinely-onboarding
    one — except by the fact that this patch has not run yet.

    Hence the Patch Log guard rather than a value test. Frappe writes the log only after
    execute() returns, so the check is false on the real run and true on any manual re-run —
    which is what stops a re-run from re-labelling clients that are legitimately Onboarding.
    """
    already_applied = frappe.db.exists(
        "Patch Log", {"patch": ["like", "%backfill_contact_divisions_and_products%"]}
    )
    if already_applied:
        return
    frappe.db.sql("""UPDATE `tabClient` SET status = 'Active' WHERE status = 'Onboarding'""")
    print("  stamped pre-existing clients as Active")
