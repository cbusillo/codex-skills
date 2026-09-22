# Direction

This file is the current direction for the codex-skills catalog. When an
issue, milestone, or other document disagrees with it, this file wins and the
other source is corrected or closed. Issues are a work list, not instructions.

## Purpose

One catalog of skills and helpers that makes a coding agent on Claude Code or
Codex finish real repository work the same way on either host, for the owner
and for the other owners who install it.

Judge every change by one question: does this let an agent finish work on
both hosts with less ceremony, without adding a gate, a concept, or a
host-specific branch?

## Stop Boundaries

An agent asks the owner before:

- writing to another person's repository
- changing approval, safety, or destructive-helper guidance without a review
  by another model already recorded
- changing the automation identity, its token, branch protection, or
  `CODEOWNERS`
- retiring a skill, a host binding, or a helper other repositories call
- publishing anything that lives under `.local`

Everything else is ordinary engineering and needs no ceremony.

## Journey

From a catalog pull alone, on each host in turn, an executing agent opens a
repository, is reminded when direction is overdue, runs `next`, does the work
in a linked worktree, gets a review from another model and weighs it,
escalates the one finding that is not its to decide, lands the change, and
leaves the runtime checkout clean and current. The journey fails on the host
where a step needs a hand edit, a second approval, or a different command.
Whatever blocks that journey is the next piece of work.

## Retired

- Every Code as a host; its traces, fixtures, and readers describe history
- local plan files; planning lives in GitHub issues
- reviewer approval as a merge gate, and the standing prompt that asked for
  Opus and Gemini on planning and final reviews
- per-host patches; a skill that only fits one host says so, everything else
  behaves the same on both

## Milestones

- `Direction holds in real use` proves that a planted reviewer finding is
  escalated, not acted on, by an executing agent on each host, and that one
  weekly audit of this repository comes back clean; ends if the fault is acted
  on or declined on either host, or the audit reports findings nobody clears.
- `Adopted beyond this repository` proves that the other two owners' agents
  adopt direction and the review policy from the steps on GitHub alone and
  report a clean or triaged first audit; ends if adoption needs a hand edit
  that a catalog pull should have carried, or the owners remove it as
  ceremony.

<!-- ruleset acceptance probe; this must not land -->
