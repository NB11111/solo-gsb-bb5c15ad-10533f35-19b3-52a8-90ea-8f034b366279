"""Engine-level tests for counterfactual fact-change search.

Run with either:
    python -m pytest tests/
    python tests/test_counterfactual.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.counterfactual import (  # noqa: E402
    CounterfactualValidationError,
    canonical_json,
    find_counterfactuals,
    validate_counterfactual,
)
from app.validation import validate_policy  # noqa: E402


def P(rules):
    return {"rules": validate_policy({"rules": rules})}


CMP = lambda f, op, v: {"field": f, "op": op, "value": v}


def result(policy, facts, target, candidates, max_changes=5):
    return find_counterfactuals(
        P(policy), facts, target, candidates, max_changes
    )


def changesets(res):
    return [tuple(sorted(c["changes"].items())) for c in res["counterfactuals"]]


def test_original_already_target_returns_empty_change_set():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    res = result(rules, {"x": 1}, "allow", {"x": [2]})
    assert res["count"] == 1
    assert res["counterfactuals"][0]["changes"] == {}
    assert res["counterfactuals"][0]["verdict"]["status"] == "decided"


def test_single_field_change_resolves_unknown():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    res = result(rules, {}, "allow", {"x": [1, 2]})
    assert changesets(res) == [(("x", 1),)]
    entry = res["counterfactuals"][0]
    assert entry["verdict"]["conclusion"] == "allow"
    assert entry["level_trace"]["state"] == "fired"


def test_value_that_keeps_unknown_is_excluded():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    # None leaves the field missing -> the verdict stays unknown.
    res = result(rules, {}, "allow", {"x": [None, 1]})
    assert changesets(res) == [(("x", 1),)]


def test_conflict_is_excluded():
    # Two same-level rules with different conclusions fire together for
    # several values: those must be excluded from the results.
    rules = [
        {"id": "yes", "priority": 10, "conclusion": "yes",
         "expr": CMP("v", "ne", 2)},
        {"id": "no", "priority": 10, "conclusion": "no",
         "expr": CMP("v", "eq", 2)},
    ]
    res = result(rules, {}, "no", {"v": [1, 2, 3]})
    assert changesets(res) == [(("v", 2),)]
    assert res["counterfactuals"][0]["verdict"]["status"] == "decided"


def test_no_match_value_is_excluded():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
        {"id": "b", "priority": 1, "conclusion": "allow",
         "expr": CMP("y", "eq", 1)},
    ]
    # Both fields present but false -> no_match. Values that keep every rule
    # false (3) stay no_match and must be excluded.
    res = result(
        rules, {"x": 2, "y": 2}, "allow",
        {"x": [1, 3], "y": [1, 3]},
    )
    # {x:1} fires rule a while rule b is false -> allow; same for {y:1}.
    # {x:1,y:1} is a superset and must be pruned.
    assert changesets(res) == [(("x", 1),), (("y", 1),)]


def test_inclusion_minimal_supersets_pruned():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
        {"id": "b", "priority": 10, "conclusion": "allow",
         "expr": CMP("y", "eq", 1)},
    ]
    # Even when both fields are required to avoid unknown originally, a single
    # change suffices once the other field is already present-and-false.
    res = result(rules, {"x": 9, "y": 9}, "allow",
                 {"x": [1], "y": [1], "z": [1]}, max_changes=3)
    assert changesets(res) == [(("x", 1),), (("y", 1),)]


def test_superset_of_solution_skipped_even_with_different_values():
    # x=1 solves. {x:1,y:anything} must never appear.
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
        {"id": "b", "priority": 10, "conclusion": "allow",
         "expr": CMP("y", "ne", 9)},
    ]
    res = result(rules, {"x": 0, "y": 9}, "allow",
                 {"x": [1, 2], "y": [1, 2]}, max_changes=2)
    assert all(set(c["changes"]) <= {"x"} or c["changes"].get("x") != 1
               for c in res["counterfactuals"])
    assert ("x", 1) in {
        tuple(sorted(c["changes"].items()))[0]
        for c in res["counterfactuals"] if len(c["changes"]) == 1
    }


def test_max_changes_limits_results():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": {"all": [CMP("x", "eq", 1), CMP("y", "eq", 1)]}},
    ]
    # Facts missing both fields: need both x and y.
    res1 = result(rules, {}, "allow", {"x": [1], "y": [1]}, max_changes=1)
    assert res1["counterfactuals"] == []
    res2 = result(rules, {}, "allow", {"x": [1], "y": [1]}, max_changes=2)
    assert changesets(res2) == [(("x", 1), ("y", 1))]


def test_max_changes_zero_returns_empty_when_not_target():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    res = result(rules, {"x": 2}, "allow", {"x": [1]}, max_changes=0)
    assert res["counterfactuals"] == []


def test_results_sorted_by_count_then_field_then_value():
    rules = [
        # x=1 or z=9 alone fires a single-conclusion rule...
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": {"any": [CMP("x", "eq", 1), CMP("z", "eq", 9)]}},
        # ...y=1 fires another same-conclusion rule.
        {"id": "b", "priority": 10, "conclusion": "allow",
         "expr": CMP("y", "eq", 1)},
    ]
    res = result(
        rules, {"x": 0, "y": 0, "z": 0}, "allow",
        {"y": [1], "x": [1], "z": [9]}, max_changes=2,
    )
    keys = [
        (len(c["changes"]), sorted(c["changes"]),
         [canonical_json(v) for _, v in sorted(c["changes"].items())])
        for c in res["counterfactuals"]
    ]
    assert keys == sorted(keys)
    # All three one-field solutions exist, ordered by field name.
    assert changesets(res)[:3] == [
        (("x", 1),), (("y", 1),), (("z", 9),)
    ]
    # Every two-field superset is pruned.
    assert all(len(c["changes"]) == 1 for c in res["counterfactuals"])


def test_value_ordering_is_canonical_json():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": {"any": [CMP("s", "eq", "a"), CMP("s", "eq", "b")]}},
    ]
    res = result(rules, {"s": "z"}, "allow", {"s": ["b", "a"]})
    assert changesets(res) == [(("s", "a"),), (("s", "b"),)]


def test_lower_priority_level_can_be_targeted():
    rules = [
        {"id": "high", "priority": 100, "conclusion": "A",
         "expr": CMP("x", "eq", 1)},
        {"id": "low", "priority": 1, "conclusion": "B",
         "expr": CMP("y", "eq", 1)},
    ]
    # To reach B, the high level must be skipped: x must be present and false.
    res = result(rules, {}, "B", {"x": [1, 2], "y": [1, 2]}, max_changes=2)
    assert changesets(res) == [(("x", 2), ("y", 1))]
    entry = res["counterfactuals"][0]
    assert entry["verdict"]["decided_priority"] == 1
    assert 100 in entry["verdict"]["skipped_priorities"]
    assert entry["level_trace"]["priority"] == 1


def test_same_value_as_fact_is_noop():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    # The only candidate restores nothing; verdict stays decided-wrong.
    res = result(rules, {"x": 2}, "allow", {"x": [2, 1]})
    assert changesets(res) == [(("x", 1),)]


def test_null_candidate_for_absent_field_is_noop():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    res = result(rules, {}, "allow", {"x": [None, 1]})
    assert changesets(res) == [(("x", 1),)]


def test_unreferenced_field_is_ignored():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    res = result(rules, {}, "allow", {"zzz": [1], "x": [1]})
    assert changesets(res) == [(("x", 1),)]


def test_unknown_target_conclusion_yields_no_results():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    res = result(rules, {}, "never", {"x": [1]})
    assert res["counterfactuals"] == []


def test_duplicate_candidate_values_are_collapsed():
    rules = [
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
    ]
    facts, target, candidates, max_changes = validate_counterfactual({
        "facts": {},
        "target_conclusion": "allow",
        "candidates": {"x": [1, 1.0, 1]},
        "max_changes": 3,
    })
    # Identical JSON text collapses (1 == 1); 1 and 1.0 are canonically
    # distinct ("1" vs "1.0") and stay separate, sorted canonically.
    assert candidates["x"] == [1, 1.0]
    res = find_counterfactuals(
        P(rules), facts, target, candidates, max_changes
    )
    canon = [
        canonical_json(c["changes"]["x"])
        for c in res["counterfactuals"]
    ]
    assert canon == ["1", "1.0"]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _invalid(body):
    try:
        validate_counterfactual(body)
    except CounterfactualValidationError as exc:
        return exc.errors
    raise AssertionError("should have raised 422")


def test_validation_accepts_full_body():
    facts, target, candidates, max_changes = validate_counterfactual({
        "facts": {"a": 1},
        "target_conclusion": "allow",
        "candidates": {"a": [2, 3]},
        "max_changes": 2,
    })
    assert facts == {"a": 1}
    assert target == "allow"
    assert candidates == {"a": [2, 3]}
    assert max_changes == 2


def test_validation_defaults_facts():
    facts, _, _, _ = validate_counterfactual({
        "target_conclusion": "x",
        "candidates": {"a": [1]},
        "max_changes": 0,
    })
    assert facts == {}


def test_validation_rejects_bad_bodies():
    assert any("facts" in e for e in _invalid({
        "facts": [], "target_conclusion": "x",
        "candidates": {"a": [1]}, "max_changes": 1}))
    assert any("target_conclusion" in e for e in _invalid({
        "candidates": {"a": [1]}, "max_changes": 1}))
    assert any("target_conclusion" in e for e in _invalid({
        "target_conclusion": "  ",
        "candidates": {"a": [1]}, "max_changes": 1}))
    assert any("max_changes" in e for e in _invalid({
        "target_conclusion": "x", "candidates": {"a": [1]}}))
    assert any("max_changes" in e for e in _invalid({
        "target_conclusion": "x", "candidates": {"a": [1]},
        "max_changes": -1}))
    assert any("max_changes" in e for e in _invalid({
        "target_conclusion": "x", "candidates": {"a": [1]},
        "max_changes": True}))
    assert any("max_changes" in e for e in _invalid({
        "target_conclusion": "x", "candidates": {"a": [1]},
        "max_changes": 1.5}))
    assert any("candidates" in e for e in _invalid({
        "target_conclusion": "x", "max_changes": 1}))
    assert any("candidates" in e for e in _invalid({
        "target_conclusion": "x", "candidates": {}, "max_changes": 1}))
    assert any("candidates" in e for e in _invalid({
        "target_conclusion": "x", "candidates": {"a": []},
        "max_changes": 1}))
    assert any("candidates" in e for e in _invalid({
        "target_conclusion": "x",
        "candidates": {"a": "not-a-list"}, "max_changes": 1}))
    assert any("JSON literal" in e for e in _invalid({
        "target_conclusion": "x",
        "candidates": {"a": [float("nan")]}, "max_changes": 1}))


def test_validation_rejects_non_object_body():
    assert _invalid([1, 2])


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            import traceback
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
