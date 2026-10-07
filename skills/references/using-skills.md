# Using Skills

Use the available skill catalog throughout the task. When the user names a
skill, or the work clearly matches a skill's description, load it before acting.
For other work, use judgment about whether a skill would help; a keyword alone
does not make a match. Respect skills that require explicit user invocation.

Load the skill that owns each step **before that step**, even inside another
skill's task. Reading a direction or planning skill does not load the skills
for its later GitHub, validation, or closeout work.

At the start of each turn, name the steps it will take and the skill that owns
each, then load each skill before its first step. Do it again when the kind of
work changes.

| Step | Owning skill |
| --- | --- |
| Read GitHub through its API, or create, push, update, or merge a PR | `github` |
| Find, read, claim, or update issues and plans | `github-plan` |
| Follow CI, reviews, and mergeability until they settle | `babysit-pr` |
| Run Python scripts, set up Python, or manage its dependencies and tests | `python-uv-workflow` |
| Inspect changed code and resolve IDE findings | `jetbrains-inspection` |
| Inspect or operate live hosts and managed services | `infra-ops` |
| Find unknown private access paths or source-of-truth docs | `docs-lookup` |
| Assess readiness to review, merge, or ship | `repo-readiness` |
| Close out, preserve work, clean up, or assess whether the Director can exit | `work-closeout` |

Use the host's skill invocation tool when available (including the catalog's
namespace, such as `shared:github`). Otherwise open the listed `SKILL.md`.
Read its instructions before acting, announce its first use briefly, and read
linked references only when their conditions apply. Within a turn, one load
covers every step that skill owns. In a later turn, and after the conversation
is compacted or summarized, load it again before its next step. A skill loaded
in an earlier turn, a remembered command, or a previous run's summary is not a
substitute for loading the current owning skill.

Choose the smallest set of skills that covers the work. Follow the user's
instructions and existing task authorization; loading a skill does not grant
permission for a new action. If a needed skill is unavailable, say so and use
an appropriate fallback within that authorization, or name the blocker.
