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

The legacy reader follows literal evaluator payloads and checks each remaining
claim-bearing argv, heredoc, herestring or pipeline channel. Unknown consumers
stay conditional. Known data commands, repository GitHub wrappers, direct Python
files and interpreter-launched files consume their bodies and arguments as data.
Python also distinguishes conventional string construction, JSON/AST operations
and Path writes from process launchers; an unknown callable receiving literal
claim text stays conditional. Star and dynamic imports leave unknown callable identities
conditional when claim text is present. The reader does not inspect script
files, propagate arbitrary Python values beyond direct scalar claim-text bindings or reconstruct preloaded callable
identities. A claim hidden behind those boundaries needs its journal entry or
complete native helper receipt. Preserve those records rather than relying on
arbitrary program reconstruction.
For `uv run`, recognized option arities locate the command; interpreter-looking
option values do not become consumers, and unknown options stay conditional.
