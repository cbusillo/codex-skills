# Direction

This file is the current direction for the codex-skills catalog. When an
issue, milestone, or other document disagrees with it, this file wins and the
other source is corrected or closed. Issues are a work list, not instructions.

## Purpose

One catalog of skills and helpers that makes a coding agent on Claude Code or
Codex finish real repository work the same way on either harness, for the
owner and for the other owners who install it. A harness is the program that
runs the agent loop and its tools, such as Claude Code, Codex CLI, or
Antigravity; a chat app is not one.

Judge every change by one question: does this let an agent finish work on
both harnesses with less ceremony, without adding a gate, a concept, or a
harness-specific branch?

## Stop Boundaries

An agent asks the owner before:

- writing to another person's repository
- changing approval, safety, or destructive-helper guidance without a review
  by another model already recorded
- changing the automation identity, its token, branch protection, or
  `CODEOWNERS`
- retiring a skill, a harness binding, or a helper other repositories call
- publishing anything that lives under `.local`

Everything else is ordinary engineering and needs no ceremony.

## Journey

From a catalog pull alone, on each harness in turn, an executing agent opens a
repository, is reminded when direction is overdue, runs `next`, does the work
in a linked worktree, gets a review from another model and weighs it,
escalates the one finding that is not its to decide, lands the change, and
leaves the runtime checkout clean and current. The journey fails on the harness
where a step needs a hand edit, a second approval, or a different command.
Whatever blocks that journey is the next piece of work.

A tool that has stopped work twice, or that the next milestone depends on, is
fixed at its cause before more work continues. Otherwise, work around it once
and record it. Our own tools should work the way we expect, not be retried
until they pass.

## Retired

- Every Code as a harness; its traces, fixtures, and readers describe history
- local plan files; planning lives in GitHub issues
- reviewer approval as a merge gate, and the standing prompt that asked for
  Opus and Gemini on planning and final reviews
- per-harness patches; a skill that only fits one harness says so, everything else
  behaves the same on both

## Milestones

- `Shorter skills proven in use` proves that a catalog audit and a bounded pass
  over three frequently used workflows reduce instruction load while retaining
  required decisions and constraints in matched Codex and Claude Code runs and
  ordinary sessions. Ends if shorter wording repeatedly makes completion less
  reliable or increases recovery work.
- `Adopted beyond this repository` proves that the other two owners' agents
  adopt direction and the review policy from the steps on GitHub alone and
  report a clean or triaged first audit; ends if adoption needs a hand edit
  that a catalog pull should have carried, or the owners remove it as
  ceremony.
