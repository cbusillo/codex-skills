#!/bin/sh
# Shared by the GitHub command and push wrappers after loading local.env.
# GitHub machine users also have type User; the fleet must declare its person.
validate_own_user_login() {
	declared_own_login=${GH_WITH_ENV_TOKEN_OWN_USER_LOGIN:-}
	case "$declared_own_login" in
	'' | *[!a-zA-Z0-9-]* | -* | *-)
		echo "error: permission denied: own-user writes require GH_WITH_ENV_TOKEN_OWN_USER_LOGIN set to the person's personal GitHub login in local.env; do not use an App or machine-user account" >&2
		return 1
		;;
	esac
	if [ "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$declared_own_login" | tr '[:upper:]' '[:lower:]')" ]; then
		echo "error: permission denied: own-user write would run as '${1:-unknown}', expected declared personal account '$declared_own_login'; switch the active gh login to that person before retrying" >&2
		return 1
	fi
}
