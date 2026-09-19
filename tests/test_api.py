"""End-to-end API tests via ASGI transport (no network needed).

Run with: python tests/test_api.py
Requires: fastapi, httpx (see requirements-dev.txt).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import store  # noqa: E402


client = TestClient(app)


def setup_function():
    store.clear()


POLICY = {
    "rules": [
        {
            "id": "adult-allow",
            "priority": 100,
            "conclusion": "allow",
            "expr": {
                "all": [
                    {"field": "age", "op": "ge", "value": 18},
                    {"field": "country", "op": "eq", "value": "NL"},
                ]
            },
        },
        {
            "id": "minor-deny",
            "priority": 100,
            "conclusion": "deny",
            "expr": {"field": "age", "op": "lt", "value": 18},
        },
        {
            "id": "manual-review",
            "priority": 10,
            "conclusion": "review",
            "expr": {"any": [
                {"field": "vip", "op": "eq", "value": True},
                {"field": "flag", "op": "eq", "value": True},
            ]},
        },
    ]
}


def create_policy():
    r = client.post("/api/v1/policies", json=POLICY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    r2 = client.get("/health/ready")
    assert r2.json()["status"] == "ok"


def test_policy_crud():
    pid = create_policy()
    r = client.get(f"/api/v1/policies/{pid}")
    assert r.status_code == 200
    assert r.json()["rule_count"] == 3

    r = client.get("/api/v1/policies")
    assert any(p["id"] == pid for p in r.json()["policies"])

    r = client.put(f"/api/v1/policies/{pid}", json={
        "rules": [{"priority": 1, "conclusion": "x",
                   "expr": {"field": "a", "op": "eq", "value": 1}}]
    })
    assert r.status_code == 200 and r.json()["rule_count"] == 1

    r = client.delete(f"/api/v1/policies/{pid}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get(f"/api/v1/policies/{pid}").status_code == 404


def test_validation_error_format():
    r = client.post("/api/v1/policies", json={"rules": [{}]})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Policy structure validation failed"
    assert isinstance(body["errors"], list) and body["errors"]


def test_evaluate_decided():
    pid = create_policy()
    r = client.post(f"/api/v1/policies/{pid}/evaluate",
                    json={"facts": {"age": 25, "country": "NL"}})
    body = r.json()
    assert body["status"] == "decided"
    assert body["conclusion"] == "allow"
    assert body["decided_priority"] == 100
    assert "level_trace" in body and body["level_trace"]["state"] == "fired"


def test_evaluate_conflict():
    pid = create_policy()
    # age >= 18 AND country NL  vs age < 18 cannot both hold; craft direct one.
    client.put(f"/api/v1/policies/{pid}", json={"rules": [
        {"id": "a", "priority": 1, "conclusion": "yes",
         "expr": {"field": "v", "op": "ge", "value": 10}},
        {"id": "b", "priority": 1, "conclusion": "no",
         "expr": {"field": "v", "op": "ne", "value": 5}},
    ]})
    body = client.post(f"/api/v1/policies/{pid}/evaluate",
                       json={"facts": {"v": 10}}).json()
    assert body["status"] == "conflict"
    assert set(body["conflicting_conclusions"]) == {"yes", "no"}


def test_evaluate_unknown_minimal_sets_and_trace():
    pid = create_policy()
    body = client.post(f"/api/v1/policies/{pid}/evaluate", json={"facts": {}}).json()
    assert body["status"] == "unknown"
    assert body["decided_priority"] == 100
    sets = [tuple(s) for s in body["minimal_missing_field_sets"]]
    # Supplying age alone decides the level: age>=18 vs age<18 covers every
    # value, and when age<18 the allow rule short-circuits to false.
    assert sets == [("age",)]
    trace = body["level_trace"]
    assert {r["rule_id"] for r in trace["rules"]} == {
        "adult-allow", "minor-deny"}
    assert {a["field"] for a in trace["atoms"]} <= {"age", "country"}
    assert trace["rules"][0]["expr"]["op"] == "all"


def test_evaluate_unknown_then_supplement_facts_decides():
    pid = create_policy()
    body = client.post(f"/api/v1/policies/{pid}/evaluate",
                       json={"facts": {"age": 25, "country": "NL"}}).json()
    assert body["status"] == "decided" and body["conclusion"] == "allow"

    body = client.post(f"/api/v1/policies/{pid}/evaluate",
                       json={"facts": {"age": 10, "country": "US"}}).json()
    # deny fires on age<18 (country false makes allow false).
    assert body["status"] == "decided" and body["conclusion"] == "deny"


def test_evaluate_falls_through_lower_level():
    pid = create_policy()
    body = client.post(f"/api/v1/policies/{pid}/evaluate",
                       json={"facts": {"age": 25, "country": "US",
                                       "vip": True}}).json()
    assert body["status"] == "decided"
    assert body["conclusion"] == "review"
    assert 100 in body["skipped_priorities"]


def test_evaluate_no_match():
    pid = create_policy()
    body = client.post(f"/api/v1/policies/{pid}/evaluate",
                       json={"facts": {"age": 25, "country": "US",
                                       "vip": False, "flag": False}}).json()
    assert body["status"] == "no_match"


def test_evaluate_empty_body():
    pid = create_policy()
    r = client.post(f"/api/v1/policies/{pid}/evaluate")
    assert r.status_code == 200 and r.json()["status"] == "unknown"


def test_evaluate_unknown_policy_404():
    r = client.post("/api/v1/policies/nope/evaluate", json={"facts": {}})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Counterfactuals
# --------------------------------------------------------------------------- #

def cf(pid, facts, target, candidates, max_changes=5):
    return client.post(
        f"/api/v1/policies/{pid}/counterfactuals",
        json={
            "facts": facts,
            "target_conclusion": target,
            "candidates": candidates,
            "max_changes": max_changes,
        },
    )


def test_counterfactual_resolves_unknown_to_target():
    pid = create_policy()
    r = cf(pid, {}, "allow",
           {"age": [10, 18], "country": ["NL", "US"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy_id"] == pid
    assert body["target_conclusion"] == "allow"
    assert body["count"] == 1
    entry = body["counterfactuals"][0]
    assert entry["changes"] == {"age": 18, "country": "NL"}
    assert entry["verdict"]["status"] == "decided"
    assert entry["verdict"]["conclusion"] == "allow"
    assert entry["level_trace"]["state"] == "fired"


def test_counterfactual_already_target_empty_changes():
    pid = create_policy()
    r = cf(pid, {"age": 25, "country": "NL"}, "allow", {"age": [10]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["counterfactuals"][0]["changes"] == {}
    assert body["counterfactuals"][0]["verdict"]["conclusion"] == "allow"


def test_counterfactual_stable_sorting():
    pid = create_policy()
    # Two independent one-field ways to reach the same target conclusion:
    # add a new single-rule policy with an OR over two fields.
    client.put(f"/api/v1/policies/{pid}", json={"rules": [
        {"id": "r", "priority": 1, "conclusion": "go",
         "expr": {"any": [
             {"field": "a", "op": "eq", "value": 1},
             {"field": "b", "op": "eq", "value": 1},
         ]}},
    ]})
    r = cf(pid, {"a": 0, "b": 0}, "go",
           {"b": [1, 2], "a": [2, 1]}, max_changes=2)
    body = r.json()
    assert [c["changes"] for c in body["counterfactuals"]] == [
        {"a": 1}, {"b": 1}
    ]
    # Two-field supersets are pruned by inclusion minimality.
    assert all(len(c["changes"]) == 1 for c in body["counterfactuals"])


def test_counterfactual_excludes_conflict_and_no_match():
    pid = create_policy()
    # v=10 -> conflict (yes and no together); v=5 -> only no; other -> no_match;
    # v in [7, 10) -> only yes.
    client.put(f"/api/v1/policies/{pid}", json={"rules": [
        {"id": "yes", "priority": 1, "conclusion": "yes",
         "expr": {"field": "v", "op": "ge", "value": 7}},
        {"id": "no", "priority": 1, "conclusion": "no",
         "expr": {"any": [
             {"field": "v", "op": "eq", "value": 5},
             {"field": "v", "op": "ge", "value": 10},
         ]}},
    ]})
    r = cf(pid, {"v": 0}, "yes", {"v": [10, 7, 5]})
    body = r.json()
    assert [c["changes"] for c in body["counterfactuals"]] == [{"v": 7}]
    assert body["counterfactuals"][0]["verdict"]["conclusion"] == "yes"


def test_counterfactual_max_changes():
    pid = create_policy()
    client.put(f"/api/v1/policies/{pid}", json={"rules": [
        {"id": "r", "priority": 1, "conclusion": "go",
         "expr": {"all": [
             {"field": "a", "op": "eq", "value": 1},
             {"field": "b", "op": "eq", "value": 1},
         ]}},
    ]})
    r1 = cf(pid, {}, "go", {"a": [1], "b": [1]}, max_changes=1)
    assert r1.status_code == 200 and r1.json()["counterfactuals"] == []
    r2 = cf(pid, {}, "go", {"a": [1], "b": [1]}, max_changes=2)
    assert r2.json()["counterfactuals"][0]["changes"] == {"a": 1, "b": 1}


def test_counterfactual_404():
    r = client.post("/api/v1/policies/nope/counterfactuals", json={
        "facts": {}, "target_conclusion": "x",
        "candidates": {"a": [1]}, "max_changes": 1,
    })
    assert r.status_code == 404


def test_counterfactual_validation_422():
    pid = create_policy()
    base = {
        "facts": {},
        "target_conclusion": "allow",
        "candidates": {"age": [18]},
        "max_changes": 1,
    }

    def expect_422(payload):
        r = client.post(f"/api/v1/policies/{pid}/counterfactuals", json=payload)
        assert r.status_code == 422, payload
        body = r.json()
        assert body["detail"] == "Counterfactual request validation failed"
        assert isinstance(body["errors"], list) and body["errors"]

    expect_422({**base, "target_conclusion": ""})
    expect_422({**base, "target_conclusion": None})
    expect_422({**base, "candidates": {}})
    expect_422({**base, "candidates": {"age": []}})
    expect_422({**base, "candidates": {"age": "x"}})
    expect_422({k: v for k, v in base.items() if k != "candidates"})
    expect_422({**base, "max_changes": -1})
    expect_422({**base, "max_changes": "2"})
    expect_422({**base, "max_changes": True})
    expect_422({**base, "facts": [1, 2]})

    # Non-object body and malformed JSON.
    r = client.post(f"/api/v1/policies/{pid}/counterfactuals", json=[1])
    assert r.status_code == 422
    r = client.post(
        f"/api/v1/policies/{pid}/counterfactuals",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        setup_function()
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
