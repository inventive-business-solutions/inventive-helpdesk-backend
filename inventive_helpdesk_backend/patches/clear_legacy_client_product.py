"""Retire `Client.product`, the single Link superseded by the Client Product model.

`backfill_contact_divisions_and_products` copied every value into a `Client Product` row
and deliberately left the originals in place, so a rollback would not be lossy. That was
one release. This is the release where they come out.

Leaving them cost a real bug. The Products page derives "assigned" purely from Client
Product rows, but Frappe's delete check scans EVERY Link field pointing at Product — so a
product whose only surviving reference was this hidden field sat in the "Unassigned" tab
and refused to delete, blaming a client that the UI showed no connection to. Found in
production on client `Amazon` -> product `Amzen`; invisible on any freshly seeded site,
because nothing but a migration ever writes this field.

Runs in pre_model_sync: the field is removed from client.json in this same release, and
once the DocField is gone the value is no longer reachable through the ORM.

Self-sufficient rather than order-dependent — it re-creates a missing engagement instead
of assuming the earlier backfill ran. A value is only cleared once its Client Product row
is confirmed to exist, so an unmigrated row is skipped and reported rather than dropped.
"""

import frappe


def execute():
    if not frappe.db.table_exists("Client"):
        return  # fresh install: nothing to migrate
    if not _has_column("Client", "product"):
        return  # already retired — this patch is idempotent

    rows = frappe.db.sql(
        "SELECT name, product FROM `tabClient` WHERE IFNULL(product, '') != ''",
        as_dict=True,
    )
    if not rows:
        return

    if not frappe.db.table_exists("Client Product"):
        # Can only happen on a site that never reached the new model. Clearing here would
        # destroy the only copy, so refuse and leave it for a later migrate.
        print(f"  Client Product table missing — left {len(rows)} legacy value(s) untouched")
        return

    cleared, skipped = 0, []
    for row in rows:
        if not frappe.db.exists("Client Product", {"client": row.name, "product": row.product}):
            try:
                # No divisions: the old single field meant "this client runs this product",
                # which is exactly what an engagement with an empty divisions table means.
                frappe.get_doc(
                    {"doctype": "Client Product", "client": row.name, "product": row.product}
                ).insert(ignore_permissions=True)
            except Exception as exc:
                # A dangling Link (product since deleted) lands here. Reported, not cleared.
                skipped.append(f"{row.name} -> {row.product} ({exc})")
                continue
        frappe.db.set_value("Client", row.name, "product", None, update_modified=False)
        cleared += 1

    print(f"  cleared Client.product on {cleared} client(s)")
    for note in skipped:
        print(f"  SKIPPED (still holds a legacy value): {note}")


def _has_column(doctype, column):
    """`frappe.db.has_column` is the documented API, but it is not present on every
    version line this app has to migrate through. Fall back to the information schema
    rather than let a missing helper abort the whole migrate."""
    try:
        return frappe.db.has_column(doctype, column)
    except AttributeError:
        cols = frappe.db.sql(f"DESC `tab{doctype}`", as_dict=True)
        return column in [c.get("Field") or c.get("column_name") for c in cols]
