# Source from interactive zsh only. Nothing is installed on PATH.
[[ -o interactive ]] || return 0
() {
  local helper="${${(%):-%x}:A:h}/interactive_accounts.py"
  local definitions
  definitions=$(command uv run --quiet --no-project --script "$helper" variants) || return
  eval "$definitions"
}
