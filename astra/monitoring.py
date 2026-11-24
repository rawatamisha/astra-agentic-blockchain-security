"""Post-admission monitoring of supplied synthetic telemetry."""

import json
from datetime import datetime, timezone
from math import sqrt
from typing import Annotated

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

WINDOW = 10
SCENARIOS = {
    "ordinary": "Ordinary activity",
    "concentration": "Concentrated block production",
    "conflicts": "Conflicting spend observations",
    "release": "Competing tips and clustered arrivals",
    "pool_gap": "Low reported pool yield",
}
CHECKS = {
    "majority": "51% / majority concentration",
    "double_spend": "Double spending",
    "selfish_mining": "Selfish mining",
    "withholding": "Block withholding",
}
Count = Annotated[int, Field(ge=0, le=100_000)]


class Observation(BaseModel):
    """One transaction-linked sample reported by the synthetic observer."""

    model_config = ConfigDict(extra="forbid", strict=True)
    observation_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    member_blocks: Count | None = None
    total_blocks: Count | None = None
    conflicting_spends: Count | None = None
    competing_tips: Count | None = None
    clustered_arrivals: Count | None = None
    expected_pool_blocks: Count | None = None
    reported_pool_blocks: Count | None = None
    reorg_depth: Count | None = None
    reversed_confirmations: Count | None = None
    stale_blocks: Count | None = None
    delayed_publications: Count | None = None

    @model_validator(mode="after")
    def validate_counts(self):
        for first, second in (
            (self.member_blocks, self.total_blocks),
            (self.competing_tips, self.clustered_arrivals),
            (self.expected_pool_blocks, self.reported_pool_blocks),
        ):
            if (first is None) != (second is None):
                raise ValueError("Related measurements must be supplied together")
        if self.member_blocks is not None and self.member_blocks > self.total_blocks:
            raise ValueError("Member blocks cannot exceed total blocks")
        if self.reversed_confirmations is not None and self.conflicting_spends is None:
            raise ValueError("Confirmation reversals require a conflicting spend count")
        return self


class MonitoringReview(BaseModel):
    explanation: str = Field(min_length=1, max_length=900)
    alternative_explanation: str = Field(min_length=1, max_length=400)
    evidence_to_collect: str = Field(min_length=1, max_length=400)


def explain_finding(model, dimension, current_report):
    """Use an evidence tool to explain one already evaluated monitoring check."""
    if model is None:
        raise ValueError("Configure an LLM to explain the monitoring finding")
    if dimension not in current_report["findings"] or dimension not in CHECKS:
        raise ValueError("Select an available monitoring finding")
    finding = current_report["findings"][dimension]
    tool = StructuredTool.from_function(
        func=lambda: finding,
        name=f"get_{dimension}_finding",
        description=f"Read the current {CHECKS[dimension]} metrics and evidence references.",
    )
    assistant = model.bind_tools([tool], tool_choice=tool.name)
    reply = assistant.invoke(
        f"Call the {tool.name} tool to obtain the evidence for the selected monitoring check."
    )
    if len(reply.tool_calls) != 1 or reply.tool_calls[0]["name"] != tool.name:
        raise ValueError("Model did not request the expected evidence tool")
    call = reply.tool_calls[0]
    evidence = tool.invoke(call["args"])
    # The numeric checking policy already ran. The model explains its limits.
    data = {key: evidence[key] for key in ("status", "explanation", "metrics")}
    review = model.with_structured_output(MonitoringReview, method="json_schema").invoke(
        f"Explain this synthetic {CHECKS[dimension]} monitoring finding. Explain the measured "
        "evidence, name a plausible alternative explanation, and identify useful additional "
        "evidence. Preserve its status and distinguish an indicator from confirmed attribution. "
        f"Keep each field brief. Evidence: {json.dumps(data)}"
    )
    return {**review.model_dump(), "tool": tool.name, "tool_call_id": call["id"],
            "observation_ids": evidence["observation_ids"]}


def finding(rows, fields, evaluate, *, any_positive=False):
    covered = [row for row in rows if all(row[key] is not None for key in fields)]
    metrics = {key: sum(row[key] for row in covered) for key in fields}
    references = [row["observation_id"] for row in covered]
    status, explanation = evaluate(metrics, covered)
    # A positive observed conflict remains reportable even with gaps in coverage.
    if len(covered) < WINDOW and not (any_positive and status == "indicator"):
        status, explanation = "insufficient", "This check needs ten samples with the required telemetry."
    return {"status": status, "explanation": explanation, "metrics": metrics,
            "observation_ids": references}


def optional_metric(metrics, rows, field, aggregate=sum):
    values = [row[field] for row in rows if row.get(field) is not None]
    metrics[field] = aggregate(values) if values else None
    metrics[f"{field}_samples"] = len(values)
    return metrics[field]


def assess_majority(metrics, rows):
    total = metrics["total_blocks"]
    depth = optional_metric(metrics, rows, "reorg_depth", max)
    if total < 20:
        return "insufficient", "Fewer than 20 accepted blocks were observed."
    share = metrics["member_blocks"] / total
    z = 1.96
    lower = ((share + z*z/(2*total)
              - z*sqrt(share*(1-share)/total + z*z/(4*total*total)))
             / (1 + z*z/total))
    metrics.update(block_share=share, wilson_lower_95=max(0.0, lower))
    status = "indicator" if share > .5 else "clear"
    reorg_text = f" Maximum reported reorganization depth: {depth}." if depth is not None else ""
    return status, (f"Member accounted for {share:.1%} of accepted blocks "
                    f"(Wilson lower bound {lower:.1%}). This is a production-share proxy.{reorg_text}")


def assess_double_spend(metrics, rows):
    conflicts = metrics["conflicting_spends"]
    reversals = optional_metric(metrics, rows, "reversed_confirmations")
    if conflicts or reversals:
        return "indicator", (f"Observer reported {conflicts} conflicting spends and "
                             f"{reversals if reversals is not None else 'no count for'} confirmation "
                             "reversals. Causation and double settlement are unverified.")
    return "clear", "No conflicting spends were reported by the synthetic observer."


def assess_selfish_mining(metrics, rows):
    overlap = sum(row["competing_tips"] > 0 and row["clustered_arrivals"] > 0 for row in rows)
    metrics["coincident_samples"] = overlap
    stale = optional_metric(metrics, rows, "stale_blocks")
    status = "indicator" if overlap >= 2 else "clear"
    stale_text = f" {stale} stale blocks were reported." if stale is not None else ""
    return status, (f"Competing tips and clustered arrivals coincided in {overlap} samples."
                    f"{stale_text} Intent and causality are unverified.")


def assess_withholding(metrics, rows):
    expected = metrics["expected_pool_blocks"]
    delayed = optional_metric(metrics, rows, "delayed_publications")
    if expected < 5:
        return "insufficient", "Expected pool exposure below five blocks is too small for this check."
    ratio = metrics["reported_pool_blocks"] / expected
    metrics["yield_ratio"] = ratio
    status = "indicator" if ratio < .2 else "clear"
    delayed_text = f" {delayed} delayed publications were reported." if delayed is not None else ""
    return status, (f"Reported pool yield was {ratio:.0%} of the supplied expectation."
                    f"{delayed_text} Variance and telemetry loss remain possible.")


def evaluate_window(rows):
    if len(rows) < WINDOW:
        return {"status": "observing", "window_size": WINDOW, "findings": {}}
    findings = {
        "majority": finding(rows, ("member_blocks", "total_blocks"), assess_majority),
        "double_spend": finding(rows, ("conflicting_spends",), assess_double_spend, any_positive=True),
        "selfish_mining": finding(rows, ("competing_tips", "clustered_arrivals"), assess_selfish_mining),
        "withholding": finding(rows, ("expected_pool_blocks", "reported_pool_blocks"), assess_withholding),
    }
    statuses = {item["status"] for item in findings.values()}
    status = "review" if "indicator" in statuses else "incomplete" if "insufficient" in statuses else "clear"
    return {"status": status, "window_size": WINDOW, "findings": findings}


def require_member(db, node_id, *, allow_revoked=False):
    row = db.execute("SELECT status FROM candidates WHERE id=?", (node_id,)).fetchone()
    if row is None:
        raise LookupError("Unknown candidate")
    if row["status"] != "APPROVED" and not (allow_revoked and row["status"] == "REVOKED"):
        raise ValueError("Monitoring requires an approved candidate")


def monitor(store, node_id):
    with store.connection() as db:
        db.execute("BEGIN")
        require_member(db, node_id, allow_revoked=True)
        records = store.monitoring_records(db, node_id, WINDOW)
        count = db.execute("SELECT count(*) FROM monitoring WHERE candidate=?", (node_id,)).fetchone()[0]
    return {"count": count, "latest": records[0]["report"] if records else evaluate_window([]),
            "observations": list(reversed(records))}


def record_observations(store, node_id, observations):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        require_member(db, node_id)
        records = store.monitoring_records(db, node_id, WINDOW - 1)
        window = [record["payload"] for record in reversed(records)]
        for observation in observations:
            payload = observation.model_dump()
            old = db.execute("SELECT payload FROM monitoring WHERE candidate=? AND observation_id=?",
                             (node_id, observation.observation_id)).fetchone()
            if old is not None:
                if Observation.model_validate_json(old["payload"]).model_dump() != payload:
                    raise ValueError("Observation ID already refers to different telemetry")
                continue
            window.append(payload)
            report = evaluate_window(window[-WINDOW:])
            db.execute("""INSERT INTO monitoring(candidate,observation_id,time,payload,report)
                          VALUES(?,?,?,?,?)""",
                       (node_id, observation.observation_id, datetime.now(timezone.utc).isoformat(),
                        json.dumps(payload), json.dumps(report)))
            window = window[-(WINDOW - 1):]
    return monitor(store, node_id)


def synthetic_batch(scenario, batch_id):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown monitoring scenario")
    return [Observation(
        observation_id=f"{batch_id}-{index}",
        member_blocks=3 if scenario == "concentration" else 1, total_blocks=5,
        conflicting_spends=int(scenario == "conflicts" and index == 9),
        competing_tips=int(scenario == "release" and index >= 8),
        clustered_arrivals=int(scenario == "release" and index >= 8),
        expected_pool_blocks=1,
        reported_pool_blocks=int(scenario != "pool_gap"),
        reorg_depth=3 if scenario == "concentration" and index == 9 else 0,
        reversed_confirmations=int(scenario == "conflicts" and index == 9),
        stale_blocks=int(scenario == "release" and index >= 8),
        delayed_publications=int(scenario == "pool_gap"),
    ) for index in range(WINDOW)]
