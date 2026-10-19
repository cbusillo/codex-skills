# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Standing Director decisions, followed by Context Panel's account choice."""

import os
import shutil
import tomllib
from pathlib import Path

from skills.supervisor.scripts import account_choice


class NoProvider(ValueError):
    def __init__(self, skipped):
        self.skipped = skipped
        detail = "; ".join(f"{p['provider']}: {p['reason']}" for p in skipped)
        super().__init__("no allowed independent review provider has a Context Panel account choice; "
                         + (detail or "policy has no provider other than the author"))


class PolicyConflict(ValueError):
    def __init__(self, provider, skipped, message):
        self.provider, self.skipped = provider, skipped
        super().__init__(message)


def policy_path():
    # Shared Code home, never the provider account home or the reviewed repository.
    return Path(os.environ.get("CODE_HOME", "~/.code")).expanduser() / "skill-data/model-review.toml"


def load_policy(path=None):
    path = path or policy_path()
    try:
        policy = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("cannot read standing review decisions (skill-data/model-review.toml); "
                         "record the Director's decisions using references/provider-selection.md") from error
    if (set(policy) - {"version", "decision", "providers", "supplementary", "provider"}
            or type(policy.get("version")) is not int or policy["version"] != 1
            or not isinstance(policy.get("decision"), str) or not policy["decision"].strip()):
        raise ValueError("review policy needs version = 1 and nonempty decision evidence")
    for key in ("providers", "supplementary"):
        value = policy.get(key, [])
        if (not isinstance(value, list) or any(not isinstance(p, str) or p not in account_choice.PROVIDERS for p in value)
                or len(value) != len(set(value))):
            raise ValueError(f"review policy {key} must list distinct supported providers")
        policy[key] = value
    if not policy["providers"] or set(policy["providers"]) & set(policy["supplementary"]):
        raise ValueError("review policy needs primary providers separate from supplementary providers")
    settings = policy.get("provider", {})
    if not isinstance(settings, dict) or any(p not in account_choice.PROVIDERS for p in settings):
        raise ValueError("review policy provider settings must name supported providers")
    for provider, values in settings.items():
        if not isinstance(values, dict) or set(values) - {"model", "timeout"}:
            raise ValueError(f"review policy {provider} supports only model and timeout")
        if "model" in values and (not isinstance(values["model"], str) or not values["model"].strip()):
            raise ValueError(f"review policy {provider} model must be nonempty")
        if "timeout" in values and (type(values["timeout"]) is not int or values["timeout"] < 1):
            raise ValueError(f"review policy {provider} timeout must be a positive integer")
    return policy


def select(policy, author, requested=None, supplementary=False, model=None, timeout=None, probe=False):
    """Select before inference; never switch providers after a review has started."""
    if author not in account_choice.PROVIDERS:
        raise ValueError("identify the author's provider with --author-provider")
    if timeout is not None and timeout < 1:
        raise ValueError("--timeout must be a positive integer")
    if model and not requested:
        raise ValueError("--model requires --provider so a fallback cannot inherit another provider's model")
    role = "supplementary" if supplementary else "providers"
    allowed = policy[role]
    if supplementary and not requested:
        raise ValueError("--supplementary requires an explicit --provider")
    if requested and (requested not in allowed or requested == author):
        raise ValueError(f"standing review decision does not allow {requested} for this role and author: "
                         + policy["decision"])
    candidates = [requested] if requested else [p for p in allowed if p != author]
    config = account_choice.load_config()
    snapshot, problem = account_choice.read_snapshot(config["snapshot_command"])
    if snapshot is None:
        raise ValueError(f"Context Panel snapshot unavailable ({problem}); no review launched")
    skipped = []
    executables = {"openai": "codex", "anthropic": "claude", "google": "agy"}
    for provider in candidates:
        settings = policy.get("provider", {}).get(provider, {})
        if model and settings.get("model") and model != settings["model"]:
            raise ValueError("--model disagrees with the standing review decision")
        if shutil.which(executables[provider]) is None:
            skipped.append({"provider": provider, "reason": "CLI is not installed"})
            continue
        try:
            choice = account_choice.choose(provider, config, snapshot, None)
        except ValueError as error:
            skipped.append({"provider": provider, "reason": str(error)})
            continue
        if timeout and settings.get("timeout") and timeout > settings["timeout"]:
            raise PolicyConflict(provider, skipped, "--timeout exceeds the standing review limit; no review launched")
        choice.update(account_choice.receipt_route(config["snapshot_command"]))
        budget = settings.get("timeout", 900)
        return {"provider": provider, "model": model or settings.get("model"),
                "timeout": timeout or (min(300, budget) if probe else budget), "choice": choice,
                "selection": {"author_provider": author, "role": "supplementary" if supplementary else "primary",
                              "decision": policy["decision"], "skipped": skipped}}
    raise NoProvider(skipped)
