"""Rewrite pre-formatted display strings ("10 July 2026, 9:14 AM") stored in the
Ticket Message / Work Note timestamp columns as canonical datetimes, so the schema
sync that follows can alter those columns from varchar to DATETIME without data
loss. Registered under [pre_model_sync] — must run while the columns are still
varchar. Fresh installs skip patches entirely, so this only ever runs on sites
that hold the legacy string data.

Any value that matches none of FORMATS is logged (not silently dropped) via
frappe.log_error before being written as NULL — that data can no longer be typed
as a Datetime, but the operator gets a record of exactly what was lost and where.
"""
import datetime

import frappe

# Try ISO first so a re-run (or already-clean rows) passes values through unchanged.
FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%d %B %Y, %I:%M %p",
    "%d %B %Y",
)


def _parse(value):
    # A forced re-run (e.g. `bench execute` for support/debugging) after the
    # schema sync has already run selects native datetime objects, not strings —
    # only strings need normalizing; anything else passes through untouched.
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value:
        return None
    for fmt in FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def execute():
    for doctype, column in (("Ticket Message", "message_on"), ("Work Note", "note_on")):
        table = f"tab{doctype}"
        if not frappe.db.sql("show tables like %s", table):
            continue
        rows = frappe.db.sql(f"select name, `{column}` as val from `{table}`", as_dict=True)
        unparsed = []
        for row in rows:
            dt = _parse(row.val)
            if dt is None and isinstance(row.val, str) and row.val.strip():
                # Non-empty but matched none of FORMATS — flag it rather than
                # silently nulling data the operator may want to recover by hand.
                unparsed.append((row.name, row.val))
            frappe.db.sql(
                f"update `{table}` set `{column}` = %s where name = %s",
                (dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None, row.name),
            )
        if unparsed:
            frappe.log_error(
                title=f"convert_child_timestamps: could not parse {len(unparsed)} {column} value(s) on {doctype}",
                message="\n".join(f"{name}: {val!r}" for name, val in unparsed),
            )
