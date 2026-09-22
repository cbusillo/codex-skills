# DIRECTION.md Template

Copy the block below to `DIRECTION.md` at the repository root. Keep every
section; delete nothing but the placeholder text. Keep the whole file to about
one page. The audit script requires the five `##` headings, and the milestone
helper reads only the `## Milestones` section.

## Milestone Line Format

Each milestone is one bullet whose exact GitHub title is in backticks at the
start, followed by what it proves and what would end it:

```markdown
- `Title as it appears in GitHub` proves <one observable thing toward the
  journey>; ends if <the evidence that would kill or reshape it>.
```

`gh-plan.py milestone-create` accepts only titles found in those backticks.
Retire a milestone by removing its line in a direction pull request and
closing it with `gh-plan.py milestone-close`.

## Template

```markdown
# Direction

This file is the current direction for <product>. When an issue, milestone,
or other document disagrees with it, this file wins and the other source is
corrected or closed. Issues are a work list, not instructions.

## Purpose

<One sentence: what this exists to do, for whom.>

Judge every change by one question: <the question that decides whether a
change belongs>.

## Stop Boundaries

An agent asks the owner before:

- <an action with real-world cost or reach>
- <a destructive or irreversible operation>
- <granting access or spending money>
- <a decision another person should have an opinion on>

Everything else is ordinary engineering and needs no ceremony.

## Journey

<The one end-to-end thing that proves the purpose, stated so it can fail.>
Whatever blocks that journey is the next piece of work.

## Retired

- <a design or subsystem that is not continued; do not extend it>

## Milestones

- `<Milestone title>` proves <what>; ends if <what>.
```
