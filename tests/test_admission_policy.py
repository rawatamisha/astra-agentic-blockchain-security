import json

import pytest

from astra.admission import register
from astra.policy import apply_admission_policy
from astra.storage import Store


@pytest.mark.parametrize(("factor", "risk", "formula", "decision"), [
    (.802, .85, "ACCEPT", "REVIEW_REQUIRED"),
    (.70, .75, "ACCEPT", "REVIEW_REQUIRED"),
    (.70, .74, "ACCEPT", "ACCEPT"),
    (.69, .85, "REJECT", "REJECT"),
    (None, .85, "MORE_EVIDENCE", "MORE_EVIDENCE"),
])
def test_specialist_review_gate(factor, risk, formula, decision):
    result = apply_admission_policy({"factor": factor, "risks": {"wallet": risk}})
    assert result["factor"] == factor
    assert result["formula_recommendation"] == formula
    assert result["recommendation"] == decision


def test_previous_reports_use_policy_without_rewriting_history(tmp_path):
    store = Store(tmp_path / "astra.sqlite3")
    node_id = register(store, "wallet", 41)["id"]
    original = {"investigation_id": "legacy", "trust": {
        "factor": .802, "risks": {"wallet": .85}, "recommendation": "ACCEPT",
    }}
    store.transition(node_id, ("PENDING_INVESTIGATION",), "APPROVED", {}, original)

    candidate = store.get(node_id)
    assert candidate["status"] == "APPROVED"
    assert candidate["report"]["trust"]["recommendation"] == "REVIEW_REQUIRED"
    with store.connection() as db:
        raw = db.execute("SELECT report FROM investigations WHERE id='legacy'").fetchone()[0]
    assert json.loads(raw) == original


def test_peak_risk_changes_wallet_formula_and_is_idempotent():
    trust = {"factor": .802, "attack_risk": .27, "positive_trust": .85,
             "risks": {"wallet": .85, "sybil": .05, "finney": .05, "eclipse": .25}}
    current = apply_admission_policy(trust)
    assert current["original_factor"] == pytest.approx(.802)
    assert current["adjusted_attack_risk"] == pytest.approx(.85)
    assert current["factor"] == pytest.approx(.570)
    assert current["recommendation"] == "REJECT"
    assert apply_admission_policy(current) == current


def test_observed_spend_conflict_uses_peak_risk():
    trust = {"factor": .7450625, "attack_risk": .27, "positive_trust": .9484375,
             "risks": {"wallet": .05, "sybil": .05, "finney": .85, "eclipse": .25}}
    result = apply_admission_policy(trust, spend_conflict=True)
    assert result["adjusted_attack_risk"] == .85
    assert result["factor"] == pytest.approx(.6290625)
    assert result["recommendation"] == "REJECT"


@pytest.mark.parametrize(("risks", "weighted", "positive", "conflict", "expected", "decision"), [
    ((.05, .05, .05, .05), .05, .9484375, False, .9490625, "ACCEPT"),
    ((.05, .85, .05, .25), .27, .85, False, .57, "REJECT"),
    ((.85, .05, .05, .05), .29, .9484375, False, .6290625, "REJECT"),
    ((.05, .05, .05, .85), .29, .7984375, False, .5390625, "REJECT"),
    ((.05, .05, .85, .25), .27, .9484375, True, .6290625, "REJECT"),
    ((None, None, None, None), None, None, False, None, "MORE_EVIDENCE"),
    ((.75, .05, .05, .05), .26, 1., False, .696, "REJECT"),
], ids=["ordinary", "unusual-wallet", "sybil", "eclipse", "spend-conflict", "sparse",
        "exact-strong-risk-threshold"])
def test_scenario_scores(risks, weighted, positive, conflict, expected, decision):
    factor = None if weighted is None else .6 * positive + .4 * (1 - weighted)
    result = apply_admission_policy({
        "factor": factor, "attack_risk": weighted, "positive_trust": positive,
        "risks": dict(zip(("sybil", "wallet", "finney", "eclipse"), risks)),
    }, spend_conflict=conflict)
    assert result["factor"] == (None if expected is None else pytest.approx(expected))
    assert result["recommendation"] == decision
