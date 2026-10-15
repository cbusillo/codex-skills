# OPW seven-day comparison

[Mike's supplied source and hand count](https://github.com/cbusillo/codex-skills/issues/1906#issuecomment-6202349442)
are the evidence for this fixture. The Order and Capacity sections in
`DIRECTION.md` are copied verbatim from that comment, sourced there to merged
commit `9a58a32cfd3bf2322046b6ec4a7d95b348063a8d`. The Reporting map is a
suggestion for Mike, not a declaration already merged in his repository.
Its label names are proposals; Mike must identify the existing labels that
prove those kinds before adopting it. No other sections or Track graph were
provided; the fixture supplies an empty graph, which is sufficient for the
sole direction-only PR and does not validate milestone attribution.

`week.json` records the six-repository scope, including archived OPW history,
and the half-open interval Mike supplied. The PR's seconds are normalized to
zero because the comment gives only the minute. This is reporter-supplied
evidence, not an independently retrieved GitHub inventory. The test sends it
through the real capacity reader with an offline fetch function; it never
runs the weekly audit or writes a marker. Separate synthetic boundary cases
are not part of Mike's hand count.
Matching that six-repository scope in a future real weekly check requires the
supported `--include-archived` option; the default omits archived repositories.

Expected result: one merge, Direction 100%, all other kinds 0%, no unresolved
PRs. This week proves direction-only attribution and zero counts. It cannot
prove the other kinds, overlap handling on real OPW work, or a real weekly
report after map adoption.

The quoted Capacity asks for an alarm. Q343's approved implementation reports
counts and shares only, preserving that prose without interpreting its caps
or adding alarms. Claim admission, execution priority and PR creation remain
unchanged.
