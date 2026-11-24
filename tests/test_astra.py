import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI

from astra.admission import decide, investigate, register
from astra.api import app
from astra.chain import Chain, Transaction, public
from astra.investigation import LocalAssessment, Plan, Review
from astra.simulation import collect
from astra.storage import Store


class TestModel:
    __test__ = False

    def __init__(self, failure=False, followup=False):
        self.failure = failure
        self.followup = followup

    def with_structured_output(self, schema, **_):
        return type("StructuredReply", (), {"invoke": lambda _, prompt: (
            Plan(order=["sybil", "wallet"], rationale="Test dispatch") if schema is Plan
            else Review(concerns=[], request_followup=self.followup, summary="Test review")
        )})()

    def bind_tools(self, tools):
        def invoke(messages):
            if self.failure:
                raise TimeoutError
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(content="", tool_calls=[{
                    "name": "Assessment", "id": "assessment",
                    "args": {"status": "completed", "risk": 0.1, "confidence": 0.8,
                             "references": ["evidence"], "explanation": "Test response",
                             "indicators": []},
                }])
            return AIMessage(content="", tool_calls=[
                {"name": tools[0].name, "id": "evidence", "args": {}}
            ])
        return type("ToolReply", (), {"invoke": lambda _, messages: invoke(messages)})()


def test_signed_ledger_rejects_mutation_and_replay():
    key = Ed25519PrivateKey.generate()
    recipient = public(Ed25519PrivateKey.generate())
    chain = Chain({public(key): 25})
    tx = Transaction.sign(key, recipient, 10, 0, 1)
    with pytest.raises(ValueError):
        chain.append([replace(tx, amount=11)], 1)
    chain.append([tx], 1)
    with pytest.raises(ValueError):
        chain.append([tx], 2)
    assert chain.replay() == {public(key): 15, recipient: 10}
    assert len(chain.blocks) == 2


@pytest.mark.parametrize("scenario", ["normal", "sybil", "eclipse", "wallet", "conflict", "sparse"])
def test_observed_scenarios(tmp_path, scenario):
    store = Store(tmp_path / "astra.sqlite3")
    candidate = register(store, scenario, 41)
    evidence = collect(candidate, scenario, 41)
    assert evidence["network"]["replicas_agree"]
    assert len(evidence["sybil"]["same_timestamp_recipients"]) == (3 if scenario == "sybil" else 0)
    assert evidence["eclipse"]["distinct_groups"] == (
        0 if scenario == "sparse" else 1 if scenario == "eclipse" else 4
    )
    assert evidence["finney"]["conflict"] == (scenario == "conflict")
    if scenario == "wallet":
        assert evidence["wallet"]["model"]["features"]["count"] == 28
        assert evidence["wallet"]["model"]["lower_tail_percentile"] < .05
    if scenario == "sparse":
        assert evidence["wallet"]["model"] is None


@pytest.mark.parametrize(("scenario", "model", "complete"), [
    ("normal", TestModel(), True),
    ("conflict", TestModel(), True),
    ("sparse", TestModel(), False),
    ("normal", TestModel(failure=True), False),
    ("normal", TestModel(followup=True), False),
    ("normal", None, False),
])
def test_investigation_and_admin_review(tmp_path, scenario, model, complete):
    path = tmp_path / "astra.sqlite3"
    store = Store(path)
    candidate = register(store, scenario, 41)
    with pytest.raises(ValueError):
        decide(store, candidate["id"], "approve", "Premature")
    investigated = investigate(store, candidate["id"], model)
    report = investigated["report"]
    assert "error" not in report, report
    assert investigated["status"] == "AWAITING_ADMIN_REVIEW"
    assert (report["trust"]["factor"] is not None) == complete
    if complete:
        assert report["trust"]["attack_risk"] == pytest.approx(.25 if scenario == "conflict" else .1)
        if scenario == "conflict":
            finney = report["results"]["finney"]
            assert finney["assessment"]["risk"] == .85
            assert finney["assessment"]["finney_attribution"] == "unverified"
            assert finney["assessment"]["references"] == [finney["calls"][0]["id"]]
            assert report["trust"]["recommendation"] != "ACCEPT"
        assert report["order"][:2] == ["sybil", "wallet"]
        assert report["results"]["wallet"]["calls"][0]["tool"] == "get_wallet_evidence"
    else:
        assert report["trust"]["recommendation"] == "MORE_EVIDENCE"
    decided = decide(Store(path), candidate["id"], "approve" if complete else "more", "Reviewed")
    assert decided["status"] == ("APPROVED" if complete else "MORE_EVIDENCE_REQUIRED")
    assert len(store.events(candidate["id"])) == 4


def test_api_membership_requires_admin_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_DB", str(tmp_path / "astra.sqlite3"))
    monkeypatch.setenv("ASTRA_ADMIN_TOKEN", "local-test-token")
    monkeypatch.delenv("ASTRA_MODEL", raising=False)
    client = TestClient(app)
    created = client.post("/candidates", json={"scenario": "normal", "seed": 41})
    assert created.status_code == 201
    path = f"/candidates/{created.json()['id']}"
    assert client.post(path + "/investigate").status_code == 200
    result = {"action": "approve", "note": "Synthetic demonstration"}
    assert client.post(path + "/decision", json=result).status_code == 403
    assert client.get("/members").json() == []
    assert client.post(path + "/decision", json=result,
                       headers={"Authorization": "Bearer local-test-token"}).status_code == 200
    assert client.get("/members").json() == [created.json()["id"]]


def test_dashboard_registration_investigation_and_admin(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("ASTRA_DB", str(tmp_path / "astra.sqlite3"))
    monkeypatch.delenv("ASTRA_MODEL", raising=False)
    dashboard = Path(__file__).resolve().parents[1] / "dashboard.py"
    page = AppTest.from_file(dashboard, default_timeout=15).run()
    assert not page.exception
    page.sidebar.button[0].click().run()
    assert not page.exception
    next(button for button in page.button if button.label == "Run investigation").click().run()
    assert not page.exception
    page.text_area[0].set_value("Reviewed synthetic dashboard run")
    next(button for button in page.button if button.label == "Record decision").click().run()
    assert not page.exception
    assert Store(tmp_path / "astra.sqlite3").list()[0]["status"] == "APPROVED"
    assert any("7 active replicas" in caption.value for caption in page.caption)


def test_approved_node_relays_blocks_in_later_investigations(tmp_path):
    path = tmp_path / "astra.sqlite3"
    store = Store(path)
    first = register(store, "normal", 41)
    investigate(store, first["id"], None)
    decide(store, first["id"], "approve", "Synthetic demo")
    rejected = register(store, "normal", 42)
    investigate(store, rejected["id"], None)
    decide(store, rejected["id"], "reject", "Synthetic demo")
    second = register(Store(path), "normal", 43)
    report = investigate(Store(path), second["id"], None)["report"]
    network = report["evidence"]["network"]

    assert network["replica_count"] == 7
    assert network["replicas_agree"]
    assert first["id"] in network["nodes"]
    assert rejected["id"] not in network["nodes"]
    edges = [edge for edge in network["edges"] if first["id"] in (edge["source"], edge["target"])]
    assert len(edges) == 4
    assert all(not edge["proposed"] for edge in edges)


def test_chat_adapter_tool_contract(tmp_path):
    seen = []

    def respond(request):
        body = json.loads(request.content)
        tools = [item["function"]["name"] for item in body["tools"]]
        seen.append(tools)
        if tools == ["Plan"]:
            name, arguments = "Plan", {"order": ["wallet"], "rationale": "Inspect wallet first"}
        elif tools == ["Review"]:
            name, arguments = "Review", {"concerns": [], "request_followup": False,
                                         "summary": "Synthetic adapter check"}
        elif body["messages"][-1]["role"] == "tool":
            name, arguments = "Assessment", {
                "status": "completed", "risk": .1, "confidence": .8,
                "references": [body["messages"][-1]["tool_call_id"]],
                "explanation": "Synthetic adapter check", "indicators": [],
            }
        else:
            name, arguments = tools[0], {}
        return httpx.Response(200, json={
            "id": "chat-test", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"tool-{len(seen)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            }}],
        })

    store = Store(tmp_path / "astra.sqlite3")
    candidate = register(store, "normal", 41)
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        model = ChatOpenAI(model="test-model", api_key="test-key",
                           base_url="http://test.invalid/v1", http_client=transport,
                           use_responses_api=False)
        report = investigate(store, candidate["id"], model)["report"]
    assert report["trust"]["attack_risk"] == pytest.approx(.1), report
    assert len(seen) == 10


def test_local_assessment_links_real_evidence(tmp_path):
    class SmallModel(TestModel):
        model_name = "astra-qwen3-small"

        def with_structured_output(self, schema, **kwargs):
            if schema is LocalAssessment:
                return type("StructuredReply", (), {"invoke": lambda _, prompt: LocalAssessment(
                    finding="no_indicator", confidence=.8, explanation="Test assessment based on tool evidence"
                )})()
            return super().with_structured_output(schema, **kwargs)

    store = Store(tmp_path / "astra.sqlite3")
    candidate = register(store, "normal", 41)
    stages = []
    result = investigate(store, candidate["id"], SmallModel(), progress=stages.append)
    report = result["report"]
    assert report["trust"]["factor"] is not None, report
    assert stages[0] == "Planning the investigation"
    assert stages[-1] == "Reviewing the specialist results"
    for dimension in report["results"].values():
        assert dimension["assessment"]["references"] == [dimension["calls"][0]["id"]]


def test_richer_specialist_evidence_and_conflict_escalation(tmp_path):
    store = Store(tmp_path / "astra.sqlite3")
    for scenario in ("normal", "wallet", "sybil", "eclipse", "conflict"):
        candidate = register(store, scenario, 41)
        report = investigate(store, candidate["id"], None)["report"]
        evidence = report["evidence"]
        if scenario == "wallet":
            assert evidence["wallet"]["signals"]["burst_share_under_10_ticks"] == 1
        if scenario == "sybil":
            assert len(evidence["sybil"]["same_timestamp_recipients"]) == 3
        if scenario == "eclipse":
            assert evidence["eclipse"]["signals"]["group_hhi"] == 1
        if scenario == "conflict":
            assert evidence["finney"]["signals"]["conflicting_spend_pairs"] == 1
            assert report["trust"]["escalations"][0]["dimension"] == "finney"
        else:
            assert report["trust"]["escalations"] == []
