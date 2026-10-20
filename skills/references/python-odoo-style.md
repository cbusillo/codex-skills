# Python And Odoo Style

Use this style when you write Python, or Odoo JavaScript, in a repository
whose agent instructions point here. Code should read like prose: names carry
the meaning, functions are small, and nothing is said twice.

## Names Instead Of Comments

A comment is a second source of truth. It drifts from the code, and it lets
unclear code stand by explaining it.

- Write no docstrings.
- Write no comment that restates the code.
- Write no "why" comment that a better function name and parameters could
  carry. Extract the step and name it for the reason:

  ```python
  # Before
  invoice.action_post()
  # Posting stamps today's date; keep the date from the source system.
  invoice.write({"date": source_date})

  # After
  invoice.action_post()
  self._keep_source_date(invoice, source_date)
  ```

- Keep history out of code: issue numbers, people, and "previously this did"
  belong in the commit and pull request.
- Tool directives are not comments: a narrowly scoped `# noinspection`,
  `# noqa`, or `# type: ignore` for a confirmed false positive stays, without
  a prose justification line. A license header or generated-file marker that a
  tool or license requires also stays.
- What remains is a one-line note of a fact outside the code that no name can
  hold, such as a link to an upstream bug. Try the name first; a decision or
  constraint that a name can state is not such a fact.

## Shape

- One job per function. A function that needs a section comment is two
  functions.
- Return early and `continue` early instead of nesting:

  ```python
  for record in self:
      if not record.attachment.store_fname:
          self._reset_image_details(record)
          continue
  ```

- Name predicates as the question they answer, and let them compose:

  ```python
  def _should_skip_product(self, product: "odoo.model.product_template") -> bool:
      return self._matches_any_rule(product, self.excluded_by_rules)
  ```

- Don't repeat yourself. Extract shared logic to a helper where every caller
  can depend on it, such as a shared addon. "Duplicated to avoid coupling"
  is not a reason.
- Return a dataclass or `NamedTuple` instead of a tuple of more than two
  values; the field names document the result.
- Use verbs for functions, nouns for values, and no cryptic abbreviations.
  Name constants (`REQUEST_LIMIT`) instead of leaving bare numbers.
- Leave out explicit defaults and repeated attributes that change nothing.

## Types

- Annotate every function signature, parameters and return. Let locals infer
  unless the type is unclear, for example an empty container or a values dict
  you want checked.
- Use `str | None`, built-in generics such as `list[str]`, and `Self`.
- In Odoo code, use the PyCharm Odoo plugin's types as string annotations,
  with no import: recordsets as `"odoo.model.product_template"`, values dicts
  as `"odoo.values.sale_order"`. Prefer them to `cast(...)`. They are more
  precise than core's own `odoo.orm.types` aliases, which type values as
  `dict[str, Any]`.
- Build reusable search domains with `fields.Domain(...)` and combine them
  with `&`, `|`, and `~`. An annotation alone does not make a list a `Domain`.
- In tests, give the addon's `tests/fixtures/base.py` typed properties that
  return `self.env["..."]`, instead of suppressions spread across test files.

## Strings, Errors, And Logging

- Use f-strings, never `%` formatting or `str.format()`, except inside
  translations.
- A translated string's first argument is a literal. Pass values as
  arguments, and name the placeholders when there is more than one, so
  translators can reorder them. Odoo's own lint rejects an f-string here:

  ```python
  raise ValidationError(
      self.env._("%(name)s already uses location %(location)s.", name=other.display_name, location=record.location)
  )
  ```

- Translate every message you write for a `UserError`, `ValidationError`,
  `AccessError`, `AccessDenied`, or `MissingError`: `self.env._(...)` in model
  code, `_(...)` from `odoo` where there is no environment. Pass an
  already-translated message on unchanged.
- Raise the specific exception. Never use a broad `except Exception` that
  neither re-raises nor logs, and never use a silent `pass`.
- Log through the module's `_logger`; never `print`.
- Use `pathlib.Path` for paths.
- `fields.Datetime` holds naive UTC values and rejects a timezone-aware one.
  Use `fields.Datetime.now()` for stored values, and
  `fields.Datetime.context_timestamp(record, value)` to show a user local time.

## Odoo 19

These rules were checked against Odoo 19.0 source; older patterns still fill
examples online. The last two are house rules, not Odoo requirements.

- Declare SQL constraints as model attributes:
  `_name_uniq = models.Constraint("unique (name)", "Tag name already exists!")`.
  19.0 ignores `_sql_constraints` with a warning. Use `models.Index` and
  `models.UniqueIndex` for indexes.
- Override `create` with `@api.model_create_multi` and `vals_list`.
- Compute `display_name` with `_compute_display_name`; `name_get` is gone.
- Use `self.env.cr`, `self.env.uid`, and `self.env.context`, not `_cr`, `_uid`,
  or `_context`.
- Use `check_access()` and `_filtered_access()`, `_read_group()` or
  `formatted_read_group()`, `aggregator=`, and `action_archive()` or
  `action_unarchive()`. Their older forms are deprecated.
- Guard deletion with `@api.ondelete(at_uninstall=False)`; use `True` only for
  a guard that must also hold while the module is uninstalled. Never raise
  inside an `unlink` override.
- A model method without a leading underscore can be called over RPC. Start
  helpers with `_`, or mark them `@api.private`. A public method takes no
  parameter named `ids` or `context`.
- List views use a `<list>` root, never `<tree>`. Write direct `invisible`,
  `readonly`, `required`, and `column_invisible` expressions; `attrs` and
  `states` fail validation.
- Import another addon as `odoo.addons.<addon>` only when it is a manifest
  dependency. Inside an addon, use relative imports. Versioned migration
  scripts are the exception: Odoo loads them as standalone modules.
- On custom models, name relational fields for the records they hold: `partner`
  and `partners`, not `partner_id` and `partner_ids`. The ORM returns
  recordsets, not ids. Never rename a field on a core model, and keep upstream
  names where another module expects them.

## JavaScript

- Write native ES modules importing from `@web/...` and `@odoo/owl`. Files
  under `static/src` and `static/tests` are modules without an `@odoo-module`
  header; never use `odoo.define`.
- Write Owl 2 components with hooks and `static props`, and test with Hoot.
- Use JSDoc only for types (`@typedef`, `@type`, `@param {Type}`), never for
  prose.
- Use camelCase for values and functions, PascalCase for classes and
  components, and the same naming, early-return, and no-comment rules as
  Python.
- Use 4-space indentation, double quotes, and no semicolons. Leave trailing
  commas to the repository's formatter.

## Tooling

- Run `uv run ruff format` and `uv run ruff check --fix`. Where a repository
  sets no ruff configuration, use line length 133, target `py313`, select
  `ANN`, `UP`, `B`, and `I`, and ignore `D`.
- PyCharm inspection is the cleanup list. Leave touched files with no
  actionable warnings. Fix the cause before you add a `noinspection`.
- Follow the [shared test rules](../../instructions/global.md#tests).
- Where the repository provides the shared `test_support` addon, keep it
  generic and put addon-specific fixtures in the addon's `tests/fixtures/`.
  An addon that wants a curated test API gets one small
  `tests/common_imports.py` built with `build_common_imports`.

## What Changed From The Old Guide

The earlier guide lived in the archived `odoo-ai` repository under
`docs/style/` and `docs/policies/`.

| Rule | Now | Reason |
| --- | --- | --- |
| Prefer code that obviates comments | Kept, made strict | Comments are a second source of truth |
| Reserve comments for "why", constraints, or decision links | Narrowed | A function name and parameters carry the why; only a fact no name can hold remains |
| One-line justification on every `noinspection` | Dropped | The directive names the inspection; the justification is a comment |
| No docstrings (2024 prompt files) | Kept | Names carry the purpose |
| Type hints on every function signature, local inference | Kept | The old guide's boundary meant every signature; ruff `ANN` enforces it |
| PyCharm Odoo plugin magic types over `cast` | Kept | Still resolved by the plugin and used across tenant code |
| `fields.Domain` for reusable domains | Updated | Build with `fields.Domain(...)`; an annotation does not supply `&`, `\|`, `~` |
| Timezone-aware datetimes | Updated | 19.0 `fields.Datetime` rejects aware values; store naive UTC |
| f-strings only | Updated | Translations need a literal and arguments; Odoo's lint rejects f-strings there |
| PEP 604 unions, early returns, small functions, DRY | Kept | The patterns in the hand-written code |
| `<list>` root for list views, no `attrs` or `states` | Kept | 19.0's list view schema and view validation enforce both |
| `_sql_constraints` (used in older code) | Replaced by `models.Constraint` | 19.0 no longer supports it |
| Relational fields without `_id` on custom models | Kept | Records, not ids |
| Local addon import rule | Kept, shortened | Same rule, fewer words |
| Native ESM, no `odoo.define` or `@odoo-module` | Kept | 19.0 treats `static/src` files as modules |
| Owl 2, Hoot | Kept | 19.0 bundles Owl 2.8 and Hoot |
| JSDoc for hints when helpful | Updated | Types only, never prose |
| Trailing commas enabled | Updated | The old guide and the IDE settings disagreed; the formatter decides |
| Ruff format and check, line length 133 | Kept | Still the setting where repositories configure ruff |
| Zero warnings on touched files, narrow suppressions | Kept | Inspection catches what tests miss |
| Pydantic settings through `tools/platform/environment.py` | Dropped | That platform tooling was retired with `odoo-ai` |
| Run Python through `uv run`, prefer MCP tools, container path mapping | Dropped | Tool use belongs to the catalog's skills, not a style page |
| Recorded tours, `platform validate`, validation scenarios | Dropped | `odoo-devkit` and Launchplane own that workflow now |
| `test_support` and `common_imports` | Kept, shortened | Still how shared addons and tenants structure tests |
| Markdown table and doc-page rules | Dropped | Not code style |
