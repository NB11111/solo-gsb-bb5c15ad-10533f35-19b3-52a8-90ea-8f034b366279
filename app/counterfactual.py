"""Counterfactual search: inclusion-minimal fact changes forcing a verdict.

Given a policy, the current facts and, per field, a list of concrete candidate
values, :func:`find_counterfactuals` enumerates the assignments under which the
priority / three-valued adjudication engine :mod:`app.engine` returns
``decided`` with *exactly* the requested ``target_conclusion``.

Rules of the search
-------------------
* a candidate set picks **at most one candidate value per field**;
* only assignments with no more than ``max_changes`` changed fields are tried;
* assignments whose verdict stays ``unknown`` / ``conflict`` / ``no_match``
  (or decides a different conclusion) are discarded;
* results are inclusion-minimal on the set of *modified fields* (mirroring
  ``minimal_missing_field_sets``): once changing field set ``S`` suffices,
  every superset of ``S`` is pruned;
* when the original facts already yield the target conclusion, the single
  result is the empty change set;
* results are stably sorted by changed-field count, then field name and the
  canonical JSON form of each value.

Candidate values that leave a field effectively unchanged (strict equality
with the present fact, including absent-field/``null`` equivalence) are
skipped because they cannot participate in a minimal modification set.
Fields never referenced by the policy are ignored as well.
"""
from __future__ import annotations

import itertools
import json
import math
from typing import Any

from .engine import adjudicate
from .logic import _strict_eq

# Request-size guards keeping the combinatorial search bounded; mirror the
# family-expansion cap in app.logic.
MAX_CANDIDATE_FIELDS = 50
MAX_CANDIDATE_OPTIONS = 2000
MAX_CHANGES_LIMIT = 20
MAX_TARGET_LENGTH = 500


class CounterfactualValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --------------------------------------------------------------------------- #
# Canonical JSON (ordering and duplicate detection)
# --------------------------------------------------------------------------- #

def canonical_json(value: Any) -> str:
    """Deterministic JSON text used for value de-duplication and sorting."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_literal(value: Any) -> bool:
    """JSON literal check that also rejects non-finite floats (NaN/Infinity)."""
    if isinstance(value, bool) or isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_json_literal(v) for v in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _json_literal(v) for k, v in value.items()
        )
    return False


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def validate_counterfactual(
    payload: Any,
) -> tuple[dict, str, dict[str, list[Any]], int]:
    """Validate a counterfactual request body into normalized components."""
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise CounterfactualValidationError(["request body must be an object"])

    facts = payload.get("facts", {})
    if "facts" in payload and not isinstance(facts, dict):
        errors.append("facts: must be an object")
        facts = {}

    target = payload.get("target_conclusion")
    if not isinstance(target, str) or not target.strip():
        errors.append("target_conclusion: must be a non-empty string")
    elif len(target) > MAX_TARGET_LENGTH:
        errors.append(
            f"target_conclusion: must be at most {MAX_TARGET_LENGTH} characters"
        )

    max_changes = payload.get("max_changes")
    if isinstance(max_changes, bool) or not isinstance(max_changes, int):
        errors.append("max_changes: must be a non-negative integer")
    elif max_changes < 0:
        errors.append("max_changes: must be a non-negative integer")
    elif max_changes > MAX_CHANGES_LIMIT:
        errors.append(f"max_changes: must be at most {MAX_CHANGES_LIMIT}")

    candidates: dict[str, list[Any]] = {}
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, dict) or not raw_candidates:
        errors.append(
            "candidates: must be a non-empty object mapping field names to "
            "non-empty candidate-value arrays"
        )
    else:
        if len(raw_candidates) > MAX_CANDIDATE_FIELDS:
            errors.append(
                f"candidates: at most {MAX_CANDIDATE_FIELDS} fields are allowed"
            )
        total_options = 0
        for field_name in sorted(raw_candidates):
            values = raw_candidates[field_name]
            path = f"candidates[{field_name!r}]"
            if not isinstance(values, list) or not values:
                errors.append(f"{path}: must be a non-empty array of values")
                continue
            if any(not _json_literal(v) for v in values):
                errors.append(f"{path}: every candidate value must be a JSON literal")
                continue
            unique: list[Any] = []
            seen: set[str] = set()
            for value in values:
                key = canonical_json(value)
                if key not in seen:
                    seen.add(key)
                    unique.append(value)
            candidates[field_name] = unique
            total_options += len(unique)
        if total_options > MAX_CANDIDATE_OPTIONS:
            errors.append(
                f"candidates: at most {MAX_CANDIDATE_OPTIONS} distinct candidate "
                "values in total are allowed"
            )

    if errors:
        raise CounterfactualValidationError(errors)

    return facts, target, candidates, max_changes


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def _referenced_fields(rules: list[dict]) -> set[str]:
    """All field names touched by a policy's comparison leaves."""
    found: set[str] = set()

    def walk(node: dict) -> None:
        if "all" in node:
            for child in node["all"]:
                walk(child)
        elif "any" in node:
            for child in node["any"]:
                walk(child)
        elif "not" in node:
            walk(node["not"])
        else:
            found.add(node["field"])

    for rule in rules:
        walk(rule["expr"])
    return found


def _hits_target(verdict: dict, target: str) -> bool:
    return (
        verdict["status"] == "decided"
        and verdict["conclusion"] == target
    )


def _entry(changes: dict[str, Any], verdict: dict) -> dict:
    return {
        "changes": changes,
        "verdict": verdict,
        "level_trace": verdict.get("level_trace"),
    }


def _sort_key(changes: dict[str, Any]) -> tuple:
    tokens = tuple(
        (name, canonical_json(changes[name])) for name in sorted(changes)
    )
    return (len(changes), tokens)


def find_counterfactuals(
    policy: dict,
    facts: dict,
    target_conclusion: str,
    candidates: dict[str, list[Any]],
    max_changes: int,
) -> dict:
    """Return all inclusion-minimal change sets forcing ``target_conclusion``."""
    referenced = _referenced_fields(policy["rules"])
    fields = sorted(name for name in candidates if name in referenced)
    options = {
        name: sorted(candidates[name], key=canonical_json) for name in fields
    }

    def is_noop(field_name: str, value: Any) -> bool:
        """A candidate is a no-op when the same strict fact is already present."""
        return field_name in facts and _strict_eq(facts[field_name], value)

    def adjudicate_with(changes: dict[str, Any]) -> dict:
        merged = dict(facts)
        merged.update(changes)
        return adjudicate(policy, merged)

    original_verdict = adjudicate(policy, dict(facts))

    # The original facts already reach the target: report the empty change set.
    if _hits_target(original_verdict, target_conclusion):
        entries = [_entry({}, original_verdict)]
    else:
        successes: list[tuple[frozenset[str], dict[str, Any], dict]] = []
        minimal_field_sets: list[frozenset[str]] = []
        limit = min(max_changes, len(fields))

        for size in range(1, limit + 1):
            found_at_size: list[tuple[frozenset[str], dict[str, Any], dict]] = []
            for combo in itertools.combinations(fields, size):
                field_set = frozenset(combo)
                # A subset field set already succeeded: this combo cannot be
                # inclusion-minimal, no matter which values it picks.
                if any(kept <= field_set for kept in minimal_field_sets):
                    continue
                value_options = [
                    [(name, value)
                     for value in options[name]
                     if not is_noop(name, value)]
                    for name in combo
                ]
                for assignment in itertools.product(*value_options):
                    changes = {name: value for name, value in assignment}
                    verdict = adjudicate_with(changes)
                    if _hits_target(verdict, target_conclusion):
                        found_at_size.append((field_set, changes, verdict))
            successes.extend(found_at_size)
            minimal_field_sets.extend(
                field_set for field_set, _, _ in found_at_size
            )

        successes.sort(key=lambda item: _sort_key(item[1]))
        entries = [_entry(changes, verdict) for _, changes, verdict in successes]

    return {
        "target_conclusion": target_conclusion,
        "original_status": original_verdict["status"],
        "original_conclusion": original_verdict["conclusion"],
        "max_changes": max_changes,
        "count": len(entries),
        "counterfactuals": entries,
    }
