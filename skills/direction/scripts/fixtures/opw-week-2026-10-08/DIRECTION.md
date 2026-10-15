# OPW reporting fixture

## Order

1. Breakage in a live sales system comes first: the website, eBay listings,
   the ads feed, and anything that could sell a part twice or sell one OPW
   doesn't have. Odoo breakage goes to Chris.
2. Then the listed milestones, in their order. When one is waiting on people
   (Chris, staff, or Mike), work moves to the next one, not to tooling.
3. Other tooling moves ahead only when it removes a stop that has happened at
   least twice, with both linked.
4. Sales-channel work (ads, pricing, SEO) that no milestone names gets the
   share below.
5. Own projects: none. All of this work is OPW's; the line is here so the
   weekly check can count it.

## Capacity

At most a quarter of each week's merged pull requests go to sales-channel work
outside the milestones, and at most a fifth to tooling. The weekly check counts
merged pull requests by kind and raises an alarm, not a quota.

## Reporting map

```json
[
  {"name": "Direction", "evidence": "direction-only"},
  {"name": "live fix", "labels": ["live-fix"]},
  {"name": "Milestone", "evidence": "milestone"},
  {"name": "sales-channel", "labels": ["sales-channel"]},
  {"name": "tooling", "labels": ["tooling"]},
  {"name": "own projects", "evidence": "own-project"}
]
```
