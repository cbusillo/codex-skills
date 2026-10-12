# Fleet count recording

`fleet-counts.json` records two successful responses to the maintained
`SupervisorFleetCounts` query on October 12, 2026, with a page size of 25,
through the shared automation reader. Repository names were replaced with
`fixture-owner/repository-NNN` to avoid publishing the private fleet inventory.
Counts, connection totals, page flags and cursors retain the recorded values.

Tests derive expected sums from the recording rather than pinning the live
fleet's size or backlog. Mutated copies exercise incomplete reads offline.
