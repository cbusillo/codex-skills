# Role Words

Use these words for the people and permissions in skills, references,
`DIRECTION.md` files, issues, PRs, and chat. Several people run these skills
against their own direction, so the words name a role, never a particular
person.

- **Director**: the person whose `DIRECTION.md` the agents follow. Agents ask
  the Director at a stop boundary, record the Director's decisions, and hand
  the Director what remains. For anyone running these skills, it is the person
  whose direction the session serves.
- **Client**: the person whose business a product serves. The Client's
  acceptance releases that product to production. One person can be both: a
  Director's own products have the Director as their Client.
- **admin**: a permission, not a role. It covers what Launchplane formerly
  called the operator or policy administrator, such as granting access,
  changing who can merge, and audited control-plane writes. The Director
  normally holds it.
- **repository owner**: GitHub's literal sense only, the user or organization
  in `OWNER/REPO`, including owner-only GitHub settings, `CODEOWNERS`, and
  code-owner review. It is never a role word for a person the agents work for.

"Operator" is not used as a role word. Say Director for the person and admin
for the permission.

## Release Rule

Each product records its Client. The Director's overall `OWNER/direction`
`DIRECTION.md` owns release authority. Read its stop boundaries before a
production action; a role definition grants no release authority.

## Legacy Identifiers

Code identifiers, stored fields, helper-checked text, and file names written
before these words (for example `owner_review` or `operator_contract`) keep
their spelling until they are migrated together with their readers. Quote them
as code when prose must name them, and describe their meaning in these words.
`scripts/validate_role_words.py`
fails when the old role words return in prose.

New question and decision comments use `Director question:` and
`Director decision:`. Readers continue accepting `Owner question:` and
`Owner decision:` as read-only legacy spellings forever. Never edit existing
GitHub comments to migrate them.
