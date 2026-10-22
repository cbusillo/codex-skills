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
Only structured native denials or explicit helper prewrite outcomes do that.
Raw JavaScript does not prove how often a bare claim command executed. Use an
explicit `--repo OWNER/REPO` or qualified record reference in literal commands;
those targets remain bounded even inside loops. Bare or computed JavaScript
claims, unsupported executable shell payloads and template interpolations stay
conditional even when one helper receipt or rejection is present. Their known
receipt targets are still checked, including receipts in advertised saved output.
Receipt JSON in tool inputs or conversation prose is data. Merged output from
other executable commands cannot resolve a bare claim's repository. Shell
syntax that the maintained parser cannot decode also stays conditional; use
the explicit literal target form above and restore its complete native output.
Interactive input and command strings passed to unclassified launchers cannot
prove complete claim coverage. Their known receipts are still checked. Keep
the claiming host's journal and complete native lineage when using remote shells.

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
In claim-related context, unexamined interpreter, evaluator and source-shaped
arguments also stay conditional, including behind unknown wrappers and in
unsupported attached evaluator forms. They indicate possible execution without
selecting a consumer or proving a target. Ordinary option clusters alone do
not identify programs. Unknown commands need explicit long evaluator flags or
short evaluator flags with program-shaped payloads; arbitrary attached runtime
names cannot be distinguished from data option values. Shell evaluator payloads
skip recognized option operands and terminators; ambiguous options stay conditional.
