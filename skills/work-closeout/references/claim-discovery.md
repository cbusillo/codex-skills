# Recorded Claim Discovery

Read this when closeout reports `unresolved_invocations`, when recovering older
helper or remote-host evidence, or when changing the legacy reader.

Native evaluated shell commands, older shell-tool arguments and decoded literal
commands provide targets when output was truncated. No shell or JavaScript is
executed. Unresolved claim commands may read their own advertised saved or
background output; unrelated artifacts are ignored. Copied foreign-session
artifacts are excluded. A missing own artifact, orphan artifact without its
invocation, computed command without sufficient native provenance, or unknown
write stays conditional. The report lists `unresolved_invocations` with its
session, transcript, call ID and recovery, while still verifying every known
target. Restore the missing native command/output on the claiming host. A
plain nonzero exit or traceback never proves that an old helper did not post.
Structured native denials and explicit helper prewrite outcomes prove refusal.
A single literal shell call wrapped in `text(await ...)`, `text((await
...).output)` or `const result = await ...; text(result)` can use its own
receipt or prewrite refusal. Its options must be literal and its shell must
contain only claim invocations. Successful receipts still identify targets
whose claims need exact releases. Additional commands or possible repetitions
cannot borrow that refusal.
A shortened helper display can retain its target through the structured
`release_command` field when it is the helper's canonical `uv run gh-plan.py`
release route with an exact repository, positive record/comment IDs, matching
native session and handoff placeholder. This adds a target to verify; it never
proves release or excuses other execution. A conflicting full receipt refuses.
Missing or noncanonical target data still refuses; prose is not a release route.
Other raw JavaScript does not prove how often a bare claim command executed. Use an
explicit `--repo OWNER/REPO` or qualified record reference in literal commands;
those targets remain bounded even inside loops. Bare or computed JavaScript
claims, unsupported executable shell payloads and unresolved template interpolations stay
conditional even when one helper receipt or rejection is present. Their known
receipt targets are still checked, including receipts in advertised saved output.
Receipt JSON in tool inputs or conversation prose is data. Merged output from
other executable commands cannot resolve a bare claim's repository. Shell
syntax that the maintained parser cannot decode also stays conditional; use
the explicit literal target form above and restore its complete native output.
Interactive input and command strings passed to unclassified launchers cannot
prove complete claim coverage. Their known receipts are still checked. Keep
the claiming host's journal and complete native lineage when using remote shells.

The exact native zero-time `Script failed` / `SyntaxError: Invalid or unexpected
token` result proves no execution only for one supported literal shell-call
wrapper, or a complete sequence of text-wrapped literal shell calls, with an
unescaped physical newline inside an ordinary quoted string. That
string cannot parse in either a script or module. Template newlines and escaped
line continuations do not supply that proof. Other failures, runtime `eval`
errors and differences between JavaScript engines stay conditional. Known
receipt targets are still checked; no recorded JavaScript is executed.

Command fields and their literal bindings use the same syntax tree. The reader
expands immutable constant strings/arrays, literal `for-of` loops, and maps of
literal arrays, including destructured tuple parameters and numeric map indices.
Template substitutions support plain string/integer bindings and exact integer
addition, under the existing literal shell word and expansion bounds. Workdir
or output templates do not change the command's bindings. Each expanded command
goes through shell discovery and canonical release checks. Unknown bindings,
inexact integers, expansion overflow, unshared string escapes and escaped
templates stay conditional. Duplicate command fields, spreads, computed keys
and accessors also stay conditional. Quoted command strings support only escapes
shared by JavaScript and Python. This is literal decoding, never execution.

In claim-related code-mode cells, a Tree-sitter JavaScript syntax tree checks
that every effect belongs to a bounded data language. It accepts direct native
shell/output calls, literal arrays, plain loops and conditions, and immutable
native or JSON data reads. Read-only string/array methods support the recorded
JSON parsing, filtering, mapping and output-formatting shapes. Inline callbacks
are checked under their own data bindings. Calls are limited to supported
non-constructor builtins and checked inline callbacks; callback references stay
conditional except `forEach(text)`. Function literals cannot enter data directly.
Only numeric `let` counters may be incremented. Explicit property writes,
mutating method names and loop assignment targets stay conditional. Literal
arrays used to decode targets remain immutable. Prototype/constructor access, capability aliases, unknown calls,
named functions, executable options, computed callees, division syntax,
identifier escapes and unsupported syntax keep coverage conditional. A parse
error alone never proves nonexecution. The reader assumes native results,
`load` values and tool metadata obey the harness's serializable-data contract;
after claim-related code may have stored data, every later opaque cell stays
conditional because hidden or computed access cannot prove it ignored that data.
Keep subsequent diagnostics bounded or use direct literal shell/output calls;
keeping `store` separate from claim-related code also avoids that uncertainty.
Complete output alone does not resolve older opaque evidence; use the existing
Director-decision route when native evidence cannot establish coverage. Use direct
literal shell calls and `text(result)` outside this subset. Known shell calls
and their argument objects come from syntax-tree locations, including recovered
nodes in unbounded cells. Any code-mode cell outside the bounded subset retains
pending interactive claim uncertainty, including aliases, computed constructors
and escaped shorthand keys. Every decoded target remains subject to release
verification even when the surrounding cell is conditional. A direct expression-body
predicate in a checked inline data callback may call a literal regex's `test`
with one checked data argument and no gap before `.test`. Its pattern contains only letters, digits,
underscore, space, dot, pipe or hyphen, and its flags are empty or `i`.
Regex values, aliases, constructors, other methods and other syntax remain
conditional. The serializable-data and unchanged-builtins assumption also
covers `RegExp.prototype.exec`; no recorded regex is executed by the reader.

`interactive_uncertain` means a prior invocation could have left claim input in
an interactive process and this later cell is outside that subset. Complete
output alone does not resolve it. Preserve the originating command, process
session ID and subsequent input/output on the owning issue; keep coverage
conditional when that evidence cannot establish it. The existing Director-decision
route applies to irrecoverable older evidence. For new cells, use the supported
direct literal shell calls and `text(result)`; use `.includes` for plain string
filters; the narrow literal-regex predicate above is also supported. Literal
string replacement arguments are also supported, including
bounded output filenames derived from literal command lists or object entries.

Process IDs alone do not establish an input channel. Starting with stable Codex
0.87.0, non-PTY `exec_command` uses closed stdin; see the upstream
[process manager](https://raw.githubusercontent.com/openai/codex/rust-v0.87.0/codex-rs/core/src/unified_exec/process_manager.rs)
and [pipe implementation](https://raw.githubusercontent.com/openai/codex/rust-v0.87.0/codex-rs/utils/pty/src/pipe.rs).
The reader admits this contract only when every native session metadata record
has a supported stable version. Earlier, missing, malformed or prerelease
versions remain conservative. A bounded code-mode cell cannot seed input
uncertainty when every shell call has a plain argument object, known option
names and absent or literal `tty:false`; empty `write_stdin` polls also cannot
introduce claim input. Duplicates, spreads, escaped or computed keys, unknown
TTY values, other shell APIs, borrowed `load` results and opaque code retain
conservative coverage. One-shot shell APIs keep their existing result rules.
Possible TTY launches seed uncertainty even if their process ID was not printed
or their runtime metadata is missing. Explicit claim input remains subject to the
existing checks. This proof concerns native `write_stdin`, not files or sockets.

A structurally terminal native `CommandExecution` event, or the supported native
parse-failure proof, cannot initiate a new interactive context. Neither clears
an earlier possible context. Printed exit codes never retire one. A non-PTY
claim with an unknown target still needs a conclusive receipt; known targets
require GitHub verification. Closed stdin never proves that a claim was not written.

Known targets also need evidence that their attempt ended before GitHub is read.
An unfinished native process, missing terminal result, background Bash task or
asynchronous shell claim stays conditional even when its repository is known.
A bound canonical helper outcome ends that helper attempt; an unknown write
outcome still requires checking its known target on GitHub. A terminal result
belongs only to a whole native result, not printed stdout or a projected exit
field. Later polls can finish their originating process only while its
process generation remains unambiguous. Earlier polls, reused IDs and partially
printed intervening launches cannot supply that proof.

Serial `text(await ...)` wrappers can combine literal shell calls and empty
`write_stdin` polls. Their whole results must match the complete command count
in order; later terminal polls still require the matching process generation.
This completion proof permits the existing delayed GitHub-search reconciliation
for an opaque command, without treating its stdout as proof that no claim exists.

For bounded wrappers that project or combine results, native terminal events
can prove completion when the exact command started during that wrapper's
recorded timestamp interval, belongs to the same native session and matches
the full possible execution count. Overlapping or still-yielded tool calls,
unbounded repetitions, missing timestamps, foreign events and duplicate event
IDs cannot satisfy that proof. Literal loops and callbacks reuse the existing
bounded literal expansion; no historical code is executed.

A shell variable assigned once to a literal value at the top level, and a `for`
loop over literal words, expand into exact claim targets, so
`for i in 147 148; do ... claim $i; done` checks both records. This applies
only when the whole command uses plain commands, lists, pipelines, redirects,
heredocs, `{ }` groups, literal `for` loops and plain `$NAME` or `${NAME}`
expansions, and every command is an external program or `cd`, `echo`, `true`,
`false`, `pwd`, `exit` or `:` once its own name is expanded. A quoted heredoc
body is data and does not count. Any other shell builtin, function, test,
arithmetic, array, modified expansion, `+=`, `IFS` change or command name that
stays computed keeps every call in it computed, as do a second assignment, a use before
the assignment, a conditional or background assignment, and shell-managed names
such as `SECONDS`, `RANDOM`, `PATH` and `_`. Values with spaces, quotes, glob
or tilde characters, uses inside a heredoc, and zsh modifiers such as `$NAME:h`
stay computed too. Text passed
to `apply_patch` in a JavaScript cell is file data and does not make the
cell's computed commands claim-related.

The legacy reader follows literal evaluator payloads and checks each remaining
claim-bearing argv, heredoc, herestring or pipeline channel. Unknown consumers
stay conditional. Known data commands, repository GitHub wrappers, direct Python
files and unambiguous interpreter-launched files consume their bodies and arguments
as data. An option before the interpreter's file argument leaves its role
unknown; stdin remains a possible program channel. Input redirection to such
an interpreter stays conditional because the file's program is not inspected.
A heredoc reaches the command it is attached to and every stage piped after it,
and each is checked as a consumer. Python also distinguishes conventional string
construction, JSON/AST operations, Path writes and process launchers whose argv
starts with a literal known data command from other process launchers; an
unknown callable receiving literal
claim text stays conditional. Star and dynamic imports leave unknown callable identities
conditional when claim text is present. The reader does not inspect script
files, propagate arbitrary Python values beyond one-hop literal claim-text
bindings or reconstruct preloaded callable identities. Those bindings include
literal fragments in formatted strings and containers, augmented assignments
to the same name, unpacked targets, loop and comprehension targets, context
manager bindings, match captures and stores through attributes or subscripts.
Fragments accumulate across bindings
without execution-order or lexical-scope emulation, so disjoint assignments
can conservatively add uncertainty. A claim hidden beyond these boundaries
needs its journal entry or complete native helper receipt. Preserve those
records rather than relying on arbitrary program reconstruction.
Constructing a shlex parser consumes fixture text as data. A terminal
`parser.parse(...).root_node` argument is a parsed tree object; reading its
text or wrapping it in a string, formatting operation or collection preserves
uncertainty at an unknown callable. Tokenized shlex output also preserves
uncertainty when passed to an unknown callable.
For `uv run`, recognized option arities locate the command; interpreter-looking
option values do not become consumers, and unknown options stay conditional.
For `timeout`, literal durations and recognized `--kill-after`/`-k`,
`--signal`/`-s`, `--preserve-status`, `--foreground` and `--verbose`/`-v`
options locate the inner command before applying those same rules. A trailing
pipe does not hide its claim targets. Unparsed timeout forms in claim-related
context stay conditional; wrapping a command does not grant it data-command trust.
In claim-related context, unexamined interpreter, evaluator and source-shaped
arguments also stay conditional, including behind unknown wrappers and in
unsupported attached evaluator forms. They indicate possible execution without
selecting a consumer or proving a target. Ordinary option clusters alone do
not identify programs. Unknown commands need explicit long evaluator flags or
short evaluator flags with program-shaped payloads; arbitrary attached runtime
names cannot be distinguished from data option values. Shell evaluator payloads
skip recognized option operands and terminators; ambiguous options stay conditional.
