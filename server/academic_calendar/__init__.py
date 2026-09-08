"""Academic calendar: semester date ranges and school holidays.

Operator-authored data that every client needs and no client can derive.
Until this landed, both apps carried the current term as a hardcoded
constant (`AppConstants.CurrentTerm`), which meant a release every term and
no way at all to express "no class today" for a public holiday.
"""
