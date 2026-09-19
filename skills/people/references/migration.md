# People Migration

Use this guide to consolidate private person facts into a scoped people index
without leaking local identity data into tracked files. Durable identity context
should usually live in `$CODE_HOME/skills/.local/people.yaml`, with repo-local
`.local/people.yaml` reserved for project-specific contacts, supplements, or
overrides.

## Move Into People

Move durable facts about humans into the global/user people index by default:

- names, nicknames, aliases, and common misspellings
- GitHub, Discord, Slack, email, phone, website, and similar contact surfaces
- company, team, title, role, timezone, and preferred contact method
- stable relationship hints such as user, collaborator, manager, reviewer,
  client, vendor, product contact, or planning manager
- bot aliases, service accounts, and automation usernames that should resolve to
  a known operator or owner
- lightweight actor trust/posture hints, such as whether to verify code extra
  carefully, whether an actor has authority for a workflow, or whether an actor
  is unknown
- short notes that help agents resolve identity or choose the right contact path

Move longer private context into a scoped `people/<id>.md` details file only
when YAML would become hard to read.

## Keep Elsewhere

Keep workflow and repo policy in their owning systems:

- GitHub planning routing stays in the GitHub planning config.
- GitHub Project fields, focus lanes, and planning issue status stay in
  `github-plan` and GitHub.
- Repo quality gates, docs paths, deployment facts, and public-safe metadata stay
  in repo `.github/github.json` files.
- Product/app role concepts stay in product docs unless they identify a real
  person or private contact.
- Unknown GitHub actors stay unknown until verified; do not invent person records
  from a single issue, comment, PR, or commit.
- Secrets, credentials, tokens, private messages, and sensitive personal data do
  not belong in people config.

## Suggested Cleanup

1. Add one person at a time with a stable `id` and minimal aliases/contacts:

   ```sh
   uv run people/scripts/people_index.py upsert \
     --id example-person \
     --display-name "Example Person"
   ```

2. Use `--scope repo` only when the entry is truly project-specific or should
   override global/user context for the active repository.
3. Move private handle-to-name facts out of local planning prose.
4. Shrink local planning prose to workflow policy and point identity lookups to
   the `people` resolver.
5. Keep existing GitHub planning routing working while adding `person:<id>` refs
   where helpers support them.
6. Audit memories and rollout-friction findings for stale person facts; promote
   durable facts to the global/user people index only after verification.
7. Scan local repos for fields like `owner`, `manager`, `reviewer`, `assignee`,
   `contact`, and `handoff`. Migrate only values that identify a real private
   person; leave conceptual or path-like fields in place.
8. Move broadly useful entries out of repo-local people files into the
   global/user index so they follow the agent across repositories.

## Public-Safety Check

Before committing changes to the public skill, confirm that tracked files contain
only placeholders:

```sh
rg -n --hidden --glob '!**/.git/**' \
  '(TOKEN|SECRET|PRIVATE|/Users/|github_pat_|ghp_|sk-[A-Za-z0-9])' people
```

Also confirm local private files remain ignored:

```sh
git status --ignored --short .local people
```
