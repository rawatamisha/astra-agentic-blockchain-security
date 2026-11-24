"""ASTRA demo console: a web front end over the real ASTRA code.

Run from the ASTRA repository root (with its virtual environment):
    .venv\\Scripts\\python.exe webui\\server.py
then open http://127.0.0.1:8600

The admission service, LangGraph investigation, trust policy, SQLite store and
Layer 2 rolling-window checks all come from the ASTRA package: the parent
folder when webui/ sits inside the repository, else ``ASTRA_CORE``. This file only adds a live ledger
for the post-admission phase, a JSON API for the page and background threads.
"""

import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
if os.getenv("ASTRA_CORE"):
    CORE = Path(os.environ["ASTRA_CORE"]).resolve()
elif (HERE.parent / "astra" / "admission.py").exists():  # webui/ inside the ASTRA repository
    CORE = HERE.parent
else:  # webui/ in the sibling repository, next to a checkout of ASTRA
    CORE = (HERE.parent.parent / "astra-amisha").resolve()
sys.path.insert(0, str(CORE))  # her ``astra`` package must win over ours
sys.path.insert(1, str(HERE))

import networkx as nx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from astra import admission  # noqa: E402
from astra.chain import Chain, Transaction, public  # noqa: E402
from astra.investigation import configured_model  # noqa: E402
from astra.monitoring import CHECKS, WINDOW, monitor, record_observations, synthetic_batch  # noqa: E402
from astra.policy import SPECIALIST_REVIEW_THRESHOLD, TRUST_THRESHOLD  # noqa: E402
from astra.simulation import RELAYS, SCENARIOS, established_topology, signing_key  # noqa: E402
from astra.storage import Store  # noqa: E402

from analyst import StandInAnalyst  # noqa: E402

DB_PATH = HERE / "demo.sqlite3"
LOCK = threading.RLock()
PROGRESS = {}  # node_id -> list of progress messages
ATTACKS = {
    "ordinary": "Honest transactions",
    "concentration": "Majority (51%) block production",
    "conflicts": "Double spend",
    "release": "Selfish mining",
    "pool_gap": "Block withholding",
}


def analyst():
    try:
        model = configured_model()
    except Exception as error:  # misconfigured endpoint: fall back, but say so
        return StandInAnalyst(), {"kind": "stand-in", "label": "Rule-based stand-in",
                                  "detail": f"LLM endpoint error: {type(error).__name__}"}
    if model is not None:
        return model, {"kind": "llm", "label": os.getenv("ASTRA_MODEL"),
                       "detail": os.getenv("ASTRA_BASE_URL") or "configured endpoint"}
    return StandInAnalyst(), {
        "kind": "stand-in", "label": "Rule-based stand-in",
        "detail": "No LLM configured (set ASTRA_MODEL / ASTRA_BASE_URL for Qwen3 via Ollama)."}


class LiveLedger:
    """Relay ring plus approved members, each holding an independently validating Chain."""

    def __init__(self):
        self.treasury = signing_key(2026, "demo-treasury")
        self.allocations = {public(self.treasury): 10_000_000}
        self.treasury_nonce = 0
        self.clock = 1
        self.topology = established_topology([])
        self.chains = {node: Chain(self.allocations) for node in self.topology}
        self.rejected = []  # transactions refused by the replicas
        self.nonces = {}
        for index in range(4):  # some ordinary history so the chain is not empty
            self.publish([self.transfer(self.treasury, public(signing_key(index, "sink")), 50 + index)])

    def tick(self):
        self.clock += 1
        return self.clock

    def transfer(self, key, recipient, amount, nonce=None):
        sender = public(key)
        if nonce is None:
            nonce = self.nonces.get(sender, 0)
            self.nonces[sender] = nonce + 1
        return Transaction.sign(key, recipient, amount, nonce, self.tick())

    def publish(self, transactions):
        block = self.chains["relay-0"].append(transactions, self.tick())
        for _, receiver in nx.bfs_edges(self.topology, "relay-0"):
            self.chains[receiver].receive(block)
        return block

    def sync(self, candidates):
        topology = established_topology(candidates)
        authority = self.chains["relay-0"]
        for node in topology:
            if node not in self.chains:  # a newly approved member downloads and validates the chain
                replica = Chain(self.allocations)
                for block in authority.blocks[1:]:
                    replica.receive(block)
                self.chains[node] = replica
        for node in list(self.chains):
            if node not in topology:
                del self.chains[node]
        self.topology = topology

    def agree(self):
        head = self.chains["relay-0"].blocks[-1]
        return all(chain.blocks[-1] == head for chain in self.chains.values())

    def summary(self):
        blocks = self.chains["relay-0"].blocks
        return {
            "height": len(blocks) - 1,
            "replicas": len(self.chains),
            "replicas_agree": self.agree(),
            "blocks": [{"height": block.height, "hash": block.hash, "previous": block.previous,
                        "transactions": len(block.transactions)} for block in blocks[-6:]],
            "rejected": self.rejected[-3:],
        }


STATE = {"store": None, "ledger": None, "monitor": {}}


def reset():
    with LOCK:
        if DB_PATH.exists():
            DB_PATH.unlink()
        STATE["store"] = Store(str(DB_PATH))
        STATE["ledger"] = LiveLedger()
        STATE["monitor"] = {}
        PROGRESS.clear()


def store():
    return STATE["store"]


def short(candidate):
    report = candidate.get("report") or {}
    trust = report.get("trust") or {}
    return {"id": candidate["id"], "status": candidate["status"], "scenario": candidate["scenario"],
            "label": SCENARIOS[candidate["scenario"]], "seed": candidate["seed"],
            "factor": trust.get("factor"), "recommendation": trust.get("recommendation")}


def network_view(candidates):
    graph = established_topology(candidates)
    nodes = [{"id": node, "kind": "relay" if node in RELAYS else "member"} for node in graph]
    edges = [{"source": a, "target": b, "proposed": False} for a, b in graph.edges]
    for candidate in candidates:  # pending candidates show only their proposed links
        if candidate["status"] in {"APPROVED", "REJECTED", "REVOKED"}:
            continue
        nodes.append({"id": candidate["id"], "kind": "candidate", "status": candidate["status"]})
        observations = ((candidate.get("report") or {}).get("evidence", {})
                        .get("eclipse", {}).get("observations", []))
        for item in observations:
            edges.append({"source": candidate["id"], "target": item["peer"], "proposed": True,
                          "group": item["group"]})
    return {"nodes": nodes, "edges": edges}


app = FastAPI(title="ASTRA demo console")


class NewCandidate(BaseModel):
    scenario: str
    seed: int = Field(default=41, ge=0, le=2**32 - 1)


class Decision(BaseModel):
    action: str
    note: str = Field(min_length=1, max_length=1000)


class Revocation(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


class Tick(BaseModel):
    mode: str = "ordinary"


@app.get("/api/state")
def state():
    with LOCK:
        candidates = store().list()
        _, info = analyst()
        return {
            "analyst": info,
            "thresholds": {"trust": TRUST_THRESHOLD, "specialist": SPECIALIST_REVIEW_THRESHOLD,
                           "window": WINDOW},
            "scenarios": SCENARIOS, "attacks": ATTACKS, "checks": CHECKS,
            "candidates": [short(item) for item in candidates],
            "network": network_view(candidates),
            "ledger": STATE["ledger"].summary(),
        }


@app.post("/api/reset")
def do_reset():
    reset()
    return {"ok": True}


def _investigate(node_id):
    model, info = analyst()

    def progress(message):
        PROGRESS.setdefault(node_id, []).append(message)

    try:
        admission.investigate(store(), node_id, model, progress)
        progress(f"done:{info['kind']}")
    except Exception as error:  # surfaced to the page, never silent
        progress(f"error:{type(error).__name__}: {error}")


@app.post("/api/candidates", status_code=201)
def create(request: NewCandidate):
    if request.scenario not in SCENARIOS:
        raise HTTPException(422, "Unknown scenario")
    with LOCK:
        candidate = admission.register(store(), request.scenario, request.seed)
    PROGRESS[candidate["id"]] = ["Identity registered"]
    threading.Thread(target=_investigate, args=(candidate["id"],), daemon=True).start()
    return short(candidate)


@app.get("/api/candidates/{node_id}")
def candidate(node_id: str):
    item = store().get(node_id)
    if item is None:
        raise HTTPException(404, "Unknown candidate")
    report = item.get("report")
    if report and "evidence" in report:
        evidence = report["evidence"]
        report = {**report, "evidence": {
            key: value for key, value in evidence.items() if key != "blocks"},
            "ledger_blocks": len(evidence.get("blocks", []))}
    return {**short(item), "node_key": item["node_key"], "wallet": item["wallet"],
            "signature": item["signature"], "report": report,
            "progress": PROGRESS.get(node_id, []), "events": store().events(node_id)}


@app.post("/api/candidates/{node_id}/decision")
def decision(node_id: str, request: Decision):
    with LOCK:
        try:
            result = admission.decide(store(), node_id, request.action, request.note)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if result["status"] == "APPROVED":
            ledger = STATE["ledger"]
            ledger.sync(store().list())
            # the treasury funds the new member so it can transact on the live chain
            ledger.publish([ledger.transfer(ledger.treasury, result["wallet"], 5_000)])
        return short(result)


@app.post("/api/members/{node_id}/tick")
def tick(node_id: str, request: Tick):
    if request.mode not in ATTACKS:
        raise HTTPException(422, "Unknown behaviour")
    with LOCK:
        member = store().get(node_id)
        if member is None or member["status"] != "APPROVED":
            raise HTTPException(409, "Only an approved member can be monitored")
        ledger = STATE["ledger"]
        runs = STATE["monitor"].setdefault(node_id, {"seq": 0, "mode": None, "run": 0})
        runs["run"] = runs["run"] + 1 if runs["mode"] == request.mode else 0
        runs["mode"] = request.mode
        runs["seq"] += 1
        # Her fixtures place the double-spend sample at index 9 and the competing tips at
        # indices 8-9; streaming those rows makes each attack visible from its first ticks.
        row = {"conflicts": 9, "release": 8 + runs["run"] % 2}.get(request.mode, runs["run"] % WINDOW)
        observation = synthetic_batch(request.mode, f"tx{runs['seq']}")[row]

        # A real signed transfer from the member's wallet, validated by every replica.
        key = signing_key(member["seed"], "candidate")
        recipient = public(signing_key(runs["seq"] % 5, "sink"))
        tx = ledger.transfer(key, recipient, 7 + runs["seq"] % 13)
        transactions = [{**asdict(tx), "status": "accepted"}]
        block = ledger.publish([tx])
        if observation.conflicting_spends:
            # the same nonce spent again to a different recipient: replicas refuse it
            twin = ledger.transfer(key, public(signing_key(99, "sink")), tx.amount, nonce=tx.nonce)
            try:
                ledger.publish([twin])
                status = "accepted"
            except ValueError as error:
                status = f"rejected: {error}"
            transactions.append({**asdict(twin), "status": status})
            ledger.rejected.append({"nonce": twin.nonce, "status": status})
        report = record_observations(store(), node_id, [observation])
        return {"observation": observation.model_dump(), "transactions": transactions,
                "block": {"height": block.height, "hash": block.hash},
                "monitoring": report["latest"], "count": report["count"],
                "ledger": ledger.summary()}


@app.get("/api/members/{node_id}/monitoring")
def monitoring(node_id: str):
    try:
        return monitor(store(), node_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/members/{node_id}/revoke")
def revoke(node_id: str, request: Revocation):
    with LOCK:
        try:
            result = admission.revoke(store(), node_id, request.note)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        STATE["ledger"].sync(store().list())
        return short(result)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


reset()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8600")))
