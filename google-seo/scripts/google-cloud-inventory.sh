#!/usr/bin/env bash
set -euo pipefail

projects=()

while [[ $# -gt 0 ]]; do
	case "$1" in
	--project)
		if [[ $# -lt 2 ]]; then
			printf 'error: --project requires a value\n' >&2
			exit 2
		fi
		projects+=("$2")
		shift 2
		;;
	-h | --help)
		printf 'Usage: %s [--project PROJECT_ID ...]\n' "${0##*/}"
		exit 0
		;;
	*)
		printf 'error: unknown argument: %s\n' "$1" >&2
		exit 2
		;;
	esac
done

if [[ ${#projects[@]} -eq 0 ]]; then
	while IFS= read -r project_id; do
		[[ -n "$project_id" ]] && projects+=("$project_id")
	done < <(gcloud projects list \
		--filter='lifecycleState=ACTIVE' \
		--format='value(projectId)')
fi

printf 'Active account:\n'
gcloud auth list --filter='status:ACTIVE' --format='value(account)' || true

for project_id in "${projects[@]}"; do
	printf '\nProject: %s\n' "$project_id"

	printf 'Billing:\n'
	gcloud billing projects describe "$project_id" \
		--format='table(projectId,billingEnabled,billingAccountName)' || true

	printf 'Enabled APIs:\n'
	gcloud services list --enabled --project "$project_id" \
		--format='table(config.name)' || true

	printf 'API keys:\n'
	gcloud services api-keys list --project "$project_id" \
		--format='table(displayName,name,uid)' || true

	printf 'Service accounts:\n'
	gcloud iam service-accounts list --project "$project_id" \
		--format='table(email,displayName,disabled)' || true

	printf 'IAM binding count:\n'
	(gcloud projects get-iam-policy "$project_id" \
		--format='value(bindings.scope())' | wc -l | tr -d ' ') || true
	printf '\n'
done
