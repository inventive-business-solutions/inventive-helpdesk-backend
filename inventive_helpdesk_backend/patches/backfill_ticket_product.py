"""Tag existing tickets with a product, where there is only one it could be.

Support Ticket gained a `product` field; every ticket that predates it has none. Where the
ticket's division runs exactly ONE product, that is the answer and there is nothing to
choose. Where it runs several — or none — this leaves the field blank and reports the
count, because guessing would put wrong data behind the per-product reporting the field
exists to enable. Those are tagged by an agent at triage.

Idempotent: only ever fills a blank `product`, so a re-run (or a half-finished migrate)
changes nothing already set. Uses db_set rather than doc.save so one ticket that fails an
unrelated validation cannot abort the whole migration — the same reasoning as
backfill_contact_divisions_and_products.
"""
import frappe


def execute():
    tickets = frappe.db.sql(
        """
        SELECT name, client, division
        FROM `tabSupport Ticket`
        WHERE IFNULL(product, '') = '' AND IFNULL(client, '') != ''
        """,
        as_dict=True,
    )
    if not tickets:
        return

    filled, ambiguous, none_available = 0, [], []
    for t in tickets:
        options = _products_for(t.client, t.division)
        if len(options) == 1:
            frappe.db.set_value("Support Ticket", t.name, "product", options[0], update_modified=False)
            filled += 1
        elif options:
            ambiguous.append((t.name, options))
        else:
            none_available.append(t.name)

    print(f"  tagged {filled} ticket(s) with the only product their division runs")
    if ambiguous:
        print(f"  left {len(ambiguous)} blank — division runs more than one, so an agent must choose:")
        for name, options in ambiguous[:20]:
            print(f"      {name}: {', '.join(sorted(options))}")
        if len(ambiguous) > 20:
            print(f"      ... and {len(ambiguous) - 20} more")
    if none_available:
        print(f"  left {len(none_available)} blank — their client runs no product at that division")


def _products_for(client: str, division: str | None) -> list[str]:
    """Products the client runs at this division: engagements naming it, plus any attached
    client-wide (an empty division table). Mirrors SupportTicket._validate_product, so the
    backfill can never write a value that validation would later reject."""
    rows = frappe.db.sql(
        """
        SELECT cp.name, cp.product
        FROM `tabClient Product` cp
        WHERE cp.client = %s
        """,
        (client,),
        as_dict=True,
    )
    out = set()
    for row in rows:
        divisions = frappe.db.sql(
            """
            SELECT division FROM `tabClient Product Division`
            WHERE parent = %s AND parenttype = 'Client Product'
            """,
            (row.name,),
            pluck=True,
        )
        if not divisions or (division and division in divisions):
            out.add(row.product)
    return sorted(out)
