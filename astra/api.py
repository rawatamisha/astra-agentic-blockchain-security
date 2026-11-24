"""Local FastAPI entry point for synthetic admission and membership."""

import os
from secrets import compare_digest
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from astra.admission import configured_model, database, decide, investigate, register, revoke
from astra.monitoring import Observation, monitor, record_observations

app = FastAPI(title="ASTRA admission and monitoring simulator")


class Registration(BaseModel):
    scenario: Literal["normal", "sybil", "eclipse", "wallet", "conflict", "sparse"]
    seed: int = Field(ge=0, le=2**32 - 1)


class Decision(BaseModel):
    action: Literal["approve", "reject", "more"]
    note: str = Field(min_length=1, max_length=1000)


class Revocation(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


def require_admin(authorization):
    token = os.getenv("ASTRA_ADMIN_TOKEN")
    if not token or not compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(403, "Administrator token required")


@app.get("/candidates")
def candidates():
    return database().list()


@app.get("/members")
def members():
    return [entry["id"] for entry in database().list() if entry["status"] == "APPROVED"]


@app.post("/candidates", status_code=201)
def create(request: Registration):
    return register(database(), request.scenario, request.seed)


@app.post("/candidates/{node_id}/investigate")
def start(node_id: str):
    try:
        return investigate(database(), node_id, configured_model())
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/candidates/{node_id}/decision")
def decision(node_id: str, request: Decision, authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        return decide(database(), node_id, request.action, request.note)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/members/{node_id}/monitoring")
def monitoring_report(node_id: str):
    try:
        return monitor(database(), node_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/members/{node_id}/revoke")
def revoke_membership(node_id: str, request: Revocation, authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        return revoke(database(), node_id, request.note)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/members/{node_id}/monitoring/observations")
def ingest_observation(node_id: str, observation: Observation, authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        return record_observations(database(), node_id, [observation])
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
