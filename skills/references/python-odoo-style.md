# Python And Odoo Style

Use this style when you write Python, or Odoo JavaScript, in a repository
whose agent instructions point here. It exists because code that is easy to
understand and has one source of truth is easier to maintain and change, for
people and for agents. Names carry the meaning, functions are small, and
nothing is said twice.

Prefer a rule a tool enforces: Ruff, PyCharm inspections, Odoo's own lint, or
a test. Where this style departs from a standard tool or convention, the rule
says "House rule" and why, in one line; everywhere else the convention wins.

## Names Instead Of Comments

A comment is a second source of truth. It drifts from the code, and it lets
unclear code stand by explaining it.

- Write no docstrings.
- Write no comment that restates the code.
- Write no "why" comment that a name or structure could carry. Extract the
  step and name it for the reason:

  ```python
  # Before
  invoice.action_post()
  # Posting stamps today's date; keep the date from the source system.
  invoice.write({"date": source_date})

  # After
  invoice.action_post()
  self._keep_source_date(invoice, source_date)
  ```

- When a constraint cannot be carried by a name or by structure, enforce it
  with a test whose name states it, such as
  `test_provider_call_never_holds_import_lock`, or a predicate such as
  `_shopify_null_means_unchanged`.
- Keep history out of code: issue numbers, people, and "previously this did"
  belong in the commit and pull request.
- Tool directives are not comments: a narrowly scoped `# noinspection`,
  `# noqa`, or `# type: ignore` for a confirmed false positive stays, without
  a prose justification line. A license header or generated-file marker that a
  tool or license requires also stays.
- The one other comment allowed is a single line that links to an outside
  source no name can hold: a vendor document, a specification, or an upstream
  bug. It is a link, not prose.

## Shape

- One job per function. A function that needs a section comment is two
  functions.
- Return early and `continue` early instead of nesting:

  ```python
  for record in self:
      if not record.attachment_id.store_fname:
          self._reset_image_details(record)
          continue
  ```

- Name predicates as the question they answer, and let them compose:

  ```python
  def _should_skip_product(self, product: "odoo.model.product_template") -> bool:
      return self._matches_any_rule(product, self.excluded_by_rules)
  ```

- Share one implementation of one rule. Two callers that touch the same data
  but need different guarantees keep their own checks; never force a looser
  caller through a stricter caller's preconditions.
- A refactor for readability keeps every guard, handled exception, side
  effect, and the order of steps. A helper and its callers agree on exactly
  which exceptions it raises: a retry loop catches what the helper throws.
- Return a dataclass or `NamedTuple` instead of a tuple of more than two
  values; the field names document the result.
- Use verbs for functions, nouns for values, and no cryptic abbreviations.
  Name constants (`REQUEST_LIMIT`) instead of leaving bare numbers. Odoo names
  a constraint or index `<table>_<attribute>` and replaces anything past
  PostgreSQL's 63-character limit with a hash, so keep the attribute short
  enough that the full name stays readable.
- Leave out explicit defaults and repeated attributes that change nothing.

## Types

- Annotate every function signature, parameters and return. Let locals infer
  unless the type is unclear, for example an empty container or a values dict
  you want checked.
- Use `str | None` and built-in generics such as `list[str]`. Use `Self` from
  `typing`.
- `|` beside a quoted type, as in `-> "odoo.model.product_template" | None`,
  raises `TypeError`: on Python 3.13 when the module is imported, and on 3.14,
  which defers annotations, when anything reads them, such as
  `inspect.signature`. Start any module that has one with
  `from __future__ import annotations`.
- In Odoo code inspected by PyCharm, use the PyCharm Odoo plugin's types as
  string annotations, with no import: recordsets as
  `"odoo.model.product_template"`, values dicts as `"odoo.values.sale_order"`.
  Prefer them to `cast(...)`. Only PyCharm resolves them; mypy and pyright
  report them as unresolved. House rule: they give PyCharm the exact model,
  so completion and inspection check every field.
- Build reusable search domains with `fields.Domain(...)` and combine them
  with `&`, `|`, and `~`. An annotation alone does not make a list a `Domain`.
- In tests, give the addon's `tests/fixtures/base.py` typed properties that
  return `self.env["..."]`, instead of suppressions spread across test files.

## Strings, Errors, And Logging

- Build ordinary strings with f-strings, not `%` or `str.format()` (Ruff
  `UP031`, `UP032`). House rule: Odoo's Ruff settings allow `%`, but an
  f-string reads in order.
- Never build SQL from an f-string or `%`. Use `odoo.tools.SQL`, with
  `SQL.identifier()` for table and column names, or pass values as
  `cr.execute` parameters (Ruff `S608`; Odoo's lint has an SQL injection check).
- Pass a logging template and its arguments separately, never an f-string:
  `_logger.info("Imported %s orders", count)` (Ruff `G004`). Logging formats
  only the messages it emits.
- A translated string's first argument is a literal. Pass values as
  arguments, and name the placeholders when there is more than one, so
  translators can reorder them. Odoo's own lint rejects an f-string here:

  ```python
  raise ValidationError(
      self.env._("%(name)s already uses location %(location)s.", name=other.display_name, location=record.location_id.display_name)
  )
  ```

- Translate every message you write for a `UserError`, `ValidationError`,
  `AccessError`, `AccessDenied`, or `MissingError`: `self.env._(...)` in model
  code, `_(...)` from `odoo` where there is no environment. Pass an
  already-translated message on unchanged.
- Raise the specific exception. Never use a broad `except Exception` that
  neither re-raises nor logs (Ruff `BLE001`), and never use a silent `pass`.
- Diagnostics go through the module's `_logger`. A command-line tool's results
  go to standard output, with `print` where that is the tool's output, and
  keep any machine-readable format its callers read.
- Use `pathlib.Path` for paths.
- `fields.Datetime` holds naive UTC values and rejects a timezone-aware one.
  Store `fields.Datetime.now()`. The web client shows datetime fields in the
  user's time zone, so convert with
  `fields.Datetime.context_timestamp(record, value)` only where the server
  renders the text itself, and never write that result back.

## Odoo 19

- `self` is a recordset of any size in computes, onchanges, constraints and
  actions. Inside `for record in self:`, read each record's fields through
  `record` and assign them there, never through `self`, and never with
  `write()` in a compute or onchange. Every path of a compute assigns every
  computed field on every record. `@api.depends` names every field whose
  change must recompute the result, including fields read by helpers the
  compute calls, since Odoo does not look inside them; `id` is the exception,
  which Odoo refuses. `@api.depends_context` names the context keys it reads.
  Call `self.ensure_one()` where a method handles only one record.
- Query and create in batches: call `search`, `_read_group`, or `create` once
  for the whole recordset, before or after `for record in self:`, never once
  per record inside it.
- Declare SQL constraints as model attributes:
  `_name_uniq = models.Constraint("unique (name)", "Tag name already exists!")`.
  19.0 ignores `_sql_constraints` with a warning. Use `models.Index` and
  `models.UniqueIndex` for indexes.
- Override `create` with `@api.model_create_multi` and `vals_list`.
- Write x2many values with `Command` (`from odoo import Command`):
  `Command.create(values)`, `Command.link(id)`, `Command.set(ids)`,
  `Command.clear()`, never raw tuples such as `(0, 0, values)`.
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
- New relational fields use Odoo's `_id`/`_ids` convention (Odoo 19 coding
  guidelines); existing fields are not renamed, because renames need
  migrations and break other modules.

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
- Format with the Prettier settings Odoo ships in `addons/web/tooling`:
  4-space indentation, double quotes, semicolons, and a print width of 100.

## Tooling

- Run `uv run ruff format` and `uv run ruff check --fix`. Where a repository
  sets no Ruff configuration, use line length 133, select `ANN`, `UP`, `B`,
  `I`, `G`, `BLE`, and `S608`, and ignore `D`, `UP037`, which would strip the
  quotes from the PyCharm types, and `ANN401`, which rejects the `Any` that
  `**kwargs` and Odoo overrides need. House rule: the repositories already
  format at 133.
- Target the Python version our Odoo images run (today 3.13; Ruff
  `target-version` matches it). Upgrade the images first, then raise the
  target in one change, and let Ruff's `UP` rules and `ruff check --fix`
  modernize the code.
- PyCharm inspection is the cleanup list. Leave touched files with no
  actionable warnings. Fix the cause before you add a `noinspection`.
- Follow the [shared test rules](../../instructions/global.md#tests).
- Where the repository provides the shared `test_support` addon, keep it
  generic and put addon-specific fixtures in the addon's `tests/fixtures/`.
  An addon that wants a curated test API gets one small
  `tests/common_imports.py` built with `build_common_imports`.
