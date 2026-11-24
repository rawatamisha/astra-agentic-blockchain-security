"""Admission service shared by the API and dashboard."""

import os
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv

from astra.chain import digest, identity, verify
from astra.investigation import configured_model, run_investigation
from astra.simulation import SCENARIOS, collect, signing_key
from astra.storage import Store

load_dotenv()

REVIEW = "AWAITING_ADMIN_REVIEW"
MORE = "MORE_EVIDENCE_REQUIRED"
PENDING = "PENDING_INVESTIGATION"
RUNNING = "UNDER_INVESTIGATION"
REVOKED = "REVOKED"
DECISIONS = {"approve": "APPROVED", "reject": "REJECTED", "more": MORE}


def database():
    return Store(os.getenv("ASTRA_DB") or "astra.sqlite3")


def register(store, scenario, seed):
    if scenario not in SCENARIOS or type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("Choose a synthetic scenario and an unsigned 32-bit seed")
    candidate = {**identity(signing_key(seed, "candidate")), "scenario": scenario, "seed": seed}
    store.register(candidate)
    return store.get(candidate["node_id"])


def investigate(store, node_id, model, progress=None):
    candidate = store.get(node_id)
    if candidate is None:
        raise LookupError("Unknown candidate")
    investigation_id = str(uuid4())
    store.transition(node_id, (PENDING, MORE), RUNNING, {"investigation_id": investigation_id})
    started = perf_counter()
    try:
        payload = {key: candidate[key] for key in ("node_key", "wallet")}
        valid = node_id == digest(payload["node_key"]) and verify(
            payload["node_key"], candidate["signature"], payload
        )
        if not valid:
            raise ValueError("Invalid candidate identity proof")
        evidence = collect(candidate, candidate["scenario"], candidate["seed"], store.list())
        result = run_investigation(model, evidence, valid, progress)
        report = {**result, "evidence": evidence}
    except Exception as error:
        report = {
            "error": type(error).__name__,
            "trust": {"recommendation": "MORE_EVIDENCE", "factor": None},
        }
    report.update(investigation_id=investigation_id, candidate_id=node_id,
                  elapsed_seconds=round(perf_counter() - started, 3))
    store.transition(node_id, (RUNNING,), REVIEW, {"investigation_id": investigation_id}, report)
    return store.get(node_id)


def decide(store, node_id, action, note):
    if action not in DECISIONS or not note.strip():
        raise ValueError("Select an action and provide an administrator note")
    candidate = store.get(node_id)
    if candidate is None:
        raise LookupError("Unknown candidate")
    if candidate["status"] != REVIEW or candidate["report"] is None:
        raise ValueError("Admission decision requires a completed investigation")
    store.transition(node_id, (REVIEW,), DECISIONS[action],
                     {"note": note.strip(), "investigation_id": candidate["report"]["investigation_id"]})
    return store.get(node_id)


def revoke(store, node_id, note):
    if not note.strip() or len(note.strip()) > 1000:
        raise ValueError("Enter a membership revocation reason of 1 to 1000 characters")
    if store.get(node_id) is None:
        raise LookupError("Unknown candidate")
    store.transition(node_id, ("APPROVED",), REVOKED, {"note": note.strip()})
    return store.get(node_id)
