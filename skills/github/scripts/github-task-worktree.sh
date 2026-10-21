require_task_worktree() {
	expected_worktree=${1:-}
	if [ -z "$expected_worktree" ]; then
		echo "error: expected task worktree is missing; pass --worktree /absolute/task/path first" >&2
		exit 1
	fi
	case "$expected_worktree" in
	/*) ;;
	*) echo "error: expected task worktree must be absolute: $expected_worktree" >&2; exit 1 ;;
	esac
	for task_override in GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE; do
		eval "task_override_value=\${$task_override:-}"
		if [ -n "$task_override_value" ]; then
			echo "error: unset $task_override before using expected task worktree $expected_worktree" >&2
			exit 1
		fi
	done
	expected_path=$(CDPATH='' cd -- "$expected_worktree" 2>/dev/null && pwd -P) || {
		echo "error: expected task worktree is unavailable: $expected_worktree" >&2
		exit 1
	}
	current_path=$(pwd -P)
	case "$current_path/" in
	"$expected_path/"*) ;;
	*) echo "error: run from your expected task worktree: $expected_worktree (current: $current_path)" >&2; exit 1 ;;
	esac
	task_top=$(git rev-parse --show-toplevel 2>/dev/null) || task_top=
	task_git_dir=$(git rev-parse --absolute-git-dir 2>/dev/null) || task_git_dir=
	task_common_dir=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || task_common_dir=
	if [ "$task_top" != "$expected_path" ] || [ -z "$task_git_dir" ] || [ "$task_git_dir" = "$task_common_dir" ]; then
		echo "error: expected your linked task worktree at $expected_worktree; primary and runtime checkouts cannot commit or push here" >&2
		exit 1
	fi
	task_branch=$(git symbolic-ref -q --short HEAD 2>/dev/null) || task_branch=
	task_default=$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null) || task_default=origin/main
	case "$task_branch" in
	"${task_default#origin/}" | main | master)
		echo "error: expected a task branch in $expected_worktree; current branch is $task_branch" >&2
		exit 1 ;;
	esac
}
