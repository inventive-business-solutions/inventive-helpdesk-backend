"""Seed stored activation state, for both shapes a login can take.

Activation used to be inferred from signing in — POC.portal compared User.last_login
against POC.invited_on, and Team Member was promoted by an on_login hook. Both are now
stored facts written when a password is actually chosen, and the on_login hook is gone.
Without a backfill every already-activated person would read Invited the moment this
ships, and the obvious remedy (resend the invite) would be exactly wrong: it would force
real users to redeem a link they do not need.

last_login is the stand-in for the activation time. It is the closest thing on record and
the old rule already treated it as proof, so everyone this selects was being shown as
Active yesterday. Anyone who never signed in is left alone and keeps reading Invited,
which is also what they read before.

`User.last_login` is **varchar(140)**, not a datetime — so its empty state is `''`, not
NULL, and feeding that into `POC.activated_on` (a real `datetime(6)`) is a hard error
under MariaDB strict mode. `NULLIF(...,'')` is used rather than a `!= ''` guard because
the optimiser is free to evaluate the SET and the WHERE in either order: a filter that
merely excludes those rows can still be beaten by the assignment being tried first.
Both failure modes were hit on real data before this shape.

Idempotent: only touches rows that have not been stamped.
"""
import frappe


def execute():
    # Contacts: stamp the best-evidenced activation time.
    frappe.db.sql(
        """
        UPDATE `tabPOC` p
        INNER JOIN `tabUser` u ON u.name = p.user
        SET p.activated_on = NULLIF(u.last_login, '')
        WHERE p.activated_on IS NULL
          AND p.user IS NOT NULL AND p.user != ''
          AND NULLIF(u.last_login, '') IS NOT NULL
          AND u.enabled = 1
          AND (p.invited_on IS NULL OR NULLIF(u.last_login, '') > p.invited_on)
        """
    )
    # Staff: the on_login hook that used to promote them at sign-in time is gone, so anyone
    # still sitting at "Invited" with a login behind them would never be promoted at all.
    frappe.db.sql(
        """
        UPDATE `tabTeam Member` m
        INNER JOIN `tabUser` u ON u.name = m.user
        SET m.status = 'Active'
        WHERE m.status = 'Invited'
          AND NULLIF(u.last_login, '') IS NOT NULL
          AND u.enabled = 1
        """
    )
