"""Shared constants enforcing an invariant across Client, Division and Support Ticket.

MAX_CODE_LEN bounds Client.client_code and Division.division_code. Support Ticket's
autoname builds a "{client_code}-{division_code}-" prefix and uses it as the
tabSeries primary key (varchar(100)) — two codes at MAX_CODE_LEN plus separators
stays far under that limit, so the series key can never be truncated.
"""

MAX_CODE_LEN = 10
