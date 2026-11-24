import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from astra.admission import decide, investigate, register
from astra.api import app
from astra.monitoring import (MonitoringReview, Observation, explain_finding, monitor,
                             record_observations, synthetic_batch)
from astra.storage import Store


def admit(store):
    candidate = register(store, "normal", 41)
    investigate(store, candidate["id"], None)
    return decide(store, candidate["id"], "approve", "Verified synthetic member")["id"]


@pytest.mark.parametrize(("scenario", "expected"), [
    ("ordinary", set()),
    ("concentration", {"majority"}),
    ("conflicts", {"double_spend"}),
    ("release", {"selfish_mining"}),
    ("pool_gap", {"withholding"}),
])
def test_rolling_window(tmp_path, scenario, expected):
    path = tmp_path / "astra.sqlite3"
    store = Store(path)
    node_id = admit(store)
    first = synthetic_batch(scenario, "first")
    assert record_observations(store, node_id, first[:-1])["latest"]["status"] == "observing"
    result = record_observations(Store(path), node_id, first[-1:])
    assert result["count"] == 10
    findings = result["latest"]["findings"]
    assert {name for name, item in findings.items() if item["status"] == "indicator"} == expected
    assert all(len(item["observation_ids"]) == 10 for item in findings.values())
    assert findings["majority"]["metrics"]["wilson_lower_95"] < findings["majority"]["metrics"]["block_share"]
    assert findings["double_spend"]["metrics"]["reversed_confirmations"] == int(scenario == "conflicts")
    assert findings["selfish_mining"]["metrics"]["coincident_samples"] == 2 * int(scenario == "release")
    assert findings["withholding"]["metrics"]["yield_ratio"] == int(scenario != "pool_gap")
    result = record_observations(store, node_id, synthetic_batch("ordinary", "second"))
    assert result["count"] == 20
    assert result["latest"]["status"] == "clear"
    assert result["observations"][0]["observation_id"] == "second-0"
    assert store.get(node_id)["status"] == "APPROVED"


def test_monitoring_coverage_and_idempotency(tmp_path):
    store = Store(tmp_path / "astra.sqlite3")
    node_id = admit(store)
    observations = [Observation(observation_id=str(i)) for i in range(9)]
    observations.append(Observation(observation_id="9", conflicting_spends=1))
    result = record_observations(store, node_id, observations)
    assert result["latest"]["status"] == "review"
    assert result["latest"]["findings"]["majority"]["status"] == "insufficient"
    assert result["latest"]["findings"]["double_spend"]["observation_ids"] == ["9"]
    assert record_observations(store, node_id, observations)["count"] == 10
    with pytest.raises(ValueError, match="different telemetry"):
        record_observations(store, node_id, [Observation(observation_id="new"),
                                             Observation(observation_id="9", conflicting_spends=2)])
    assert monitor(store, node_id)["count"] == 10


def test_monitoring_api_restricts_ingestion(tmp_path, monkeypatch):
    path = tmp_path / "astra.sqlite3"
    monkeypatch.setenv("ASTRA_DB", str(path))
    monkeypatch.setenv("ASTRA_ADMIN_TOKEN", "test-token")
    store = Store(path)
    pending = register(store, "normal", 42)["id"]
    approved = admit(store)
    headers = {"Authorization": "Bearer test-token"}
    client = TestClient(app)
    pending_url = f"/members/{pending}/monitoring/observations"
    url = f"/members/{approved}/monitoring"
    assert client.post(pending_url, json={"observation_id": "test"}, headers=headers).status_code == 409
    assert client.post(url + "/observations", json={"observation_id": "test"}).status_code == 403
    invalid = {"observation_id": "test", "member_blocks": 2, "total_blocks": 1}
    assert client.post(url + "/observations", json=invalid, headers=headers).status_code == 422
    assert client.post(url + "/observations", json={"observation_id": "test"}, headers=headers).status_code == 200
    assert client.get(url).json()["count"] == 1


def test_dashboard_can_record_monitoring_batch(tmp_path, monkeypatch):
    path = tmp_path / "astra.sqlite3"
    monkeypatch.setenv("ASTRA_DB", str(path))
    node_id = admit(Store(path))
    dashboard = AppTest.from_file("../dashboard.py", default_timeout=15).run()
    assert not dashboard.exception
    next(button for button in dashboard.button if button.label == "Record 10 synthetic observations").click().run()
    assert not dashboard.exception
    assert monitor(Store(path), node_id)["latest"]["status"] == "clear"
    dashboard.text_area[0].set_value("Removed during synthetic review")
    next(button for button in dashboard.button if button.label == "Revoke membership").click().run()
    assert not dashboard.exception
    assert any("6 active replicas" in caption.value for caption in dashboard.caption)
    assert monitor(Store(path), node_id)["count"] == 10
    assert not any(button.label == "Record 10 synthetic observations" for button in dashboard.button)


def test_revocation_api_preserves_history_and_removes_member(tmp_path, monkeypatch):
    path = tmp_path / "astra.sqlite3"
    monkeypatch.setenv("ASTRA_DB", str(path))
    monkeypatch.setenv("ASTRA_ADMIN_TOKEN", "test-token")
    store = Store(path)
    node_id = admit(store)
    record_observations(store, node_id, synthetic_batch("conflicts", "flag"))
    client = TestClient(app)
    url = f"/members/{node_id}"
    headers = {"Authorization": "Bearer test-token"}
    assert client.post(url + "/revoke", json={"note": "Review"}).status_code == 403
    assert client.post(url + "/revoke", json={"note": " "}, headers=headers).status_code == 409
    decision = client.post(url + "/revoke", json={"note": "Reviewed conflict"}, headers=headers)
    assert decision.json()["status"] == "REVOKED"
    assert node_id not in client.get("/members").json()
    assert client.get(url + "/monitoring").json()["latest"]["status"] == "review"
    assert client.post(url + "/monitoring/observations", json={"observation_id": "new"},
                       headers=headers).status_code == 409
    assert store.events(node_id)[-1]["detail"]["note"] == "Reviewed conflict"
    next_node = register(store, "normal", 42)
    network = investigate(store, next_node["id"], None)["report"]["evidence"]["network"]
    assert network["replica_count"] == 6 and node_id not in network["nodes"]


@pytest.mark.parametrize("dimension", ["majority", "double_spend", "selfish_mining", "withholding"])
def test_llm_monitoring_review_uses_real_finding(tmp_path, dimension):
    from langchain_core.messages import AIMessage

    class Model:
        def bind_tools(self, tools, tool_choice):
            assert tool_choice == tools[0].name == f"get_{dimension}_finding"
            return type("ToolReply", (), {"invoke": lambda _, prompt: AIMessage(
                content="", tool_calls=[{"name": tool_choice, "args": {}, "id": "call-1"}]
            )})()

        def with_structured_output(self, schema, **kwargs):
            assert schema is MonitoringReview
            return type("StructuredReply", (), {"invoke": lambda _, prompt: MonitoringReview(
                explanation="Synthetic result", alternative_explanation="Sample variance",
                evidence_to_collect="Independent observer records"
            )})()

    store = Store(tmp_path / "astra.sqlite3")
    node_id = admit(store)
    result = record_observations(store, node_id, synthetic_batch("ordinary", "batch"))
    review = explain_finding(Model(), dimension, result["latest"])
    assert review["tool_call_id"] == "call-1"
    assert review["observation_ids"] == [f"batch-{i}" for i in range(10)]
