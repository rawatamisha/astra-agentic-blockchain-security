"""Deterministic stand-in for the local Qwen3 analyst.

ASTRA's investigation code (LangGraph graph, specialists, tool contract, trust
policy) runs unchanged. When no LLM endpoint is configured, this object answers
the model's calls with the same category rules that the small-model prompts
describe (``LOCAL_SIGNALS`` in ``astra/investigation.py``). The UI always says
which analyst produced a report.
"""

import json
from uuid import uuid4

from langchain_core.messages import AIMessage

DIMENSIONS = ("wallet", "sybil", "eclipse", "finney")


def _embedded_json(text, marker):
    start = text.index(marker) + len(marker)
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


class _ToolCaller:
    def __init__(self, tools):
        self.names = [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]

    def invoke(self, messages):
        text = messages if isinstance(messages, str) else " ".join(
            str(getattr(message, "content", "")) for message in messages)
        primary = next((f"get_{name}_evidence" for name in DIMENSIONS
                        if f"for {name} risk" in text), None)
        if primary is None:  # a single-tool call such as a monitoring finding
            primary = next(name for name in self.names if name.startswith("get_"))
        return AIMessage(content="", tool_calls=[
            {"name": primary, "args": {}, "id": f"call_{uuid4().hex[:12]}", "type": "tool_call"}])


class _Structured:
    def __init__(self, schema):
        self.schema = schema

    def invoke(self, prompt):
        name = self.schema.__name__
        if name == "Plan":
            return self.schema(order=list(DIMENSIONS),
                               rationale="Stand-in analyst: every specialist runs in a fixed order.")
        if name == "LocalAssessment":
            return self.schema(**_classify(_embedded_json(prompt, "Primary evidence: ")))
        if name == "Review":
            coverage = _embedded_json(prompt, "Specialist status by dimension: ")
            missing = [key for key, status in coverage.items() if status != "completed"]
            return self.schema(concerns=missing, request_followup=bool(missing),
                               summary="All four specialists completed." if not missing
                               else "Incomplete: " + ", ".join(missing))
        if name == "MonitoringReview":
            return self.schema(
                explanation="The rolling-window rule raised this indicator from the reported counts.",
                alternative_explanation="Telemetry loss, network latency or ordinary variance.",
                evidence_to_collect="Independent observer reports and the raw block timestamps.")
        raise ValueError(f"Unsupported schema {name}")


def _classify(data):
    if "baseline_result" in data:
        if data["baseline_result"] == "within_baseline":
            return dict(finding="no_indicator", confidence=.9,
                        explanation=f"Wallet sits inside the ordinary baseline "
                                    f"(percentile {data['percentile']:.2f}).")
        strong = data["percentile"] <= .01
        return dict(finding="strong_indicator" if strong else "moderate_indicator", confidence=.9,
                    explanation=f"Wallet falls below the 5th percentile of the baseline "
                                f"(percentile {data['percentile']:.2f}).")
    if "same_timestamp_recipients" in data:
        count = data["same_timestamp_recipients"]
        if not count:
            return dict(finding="no_indicator", confidence=.9,
                        explanation="No other identity was funded at the candidate's funding time.")
        return dict(finding="strong_indicator" if count >= 2 else "moderate_indicator", confidence=.9,
                    explanation=f"{count} other identities were funded at the same timestamp.")
    if "peer_group_pattern" in data:
        pattern = data["peer_group_pattern"]
        finding = {"diverse": "no_indicator", "single_group": "strong_indicator"}.get(
            pattern, "moderate_indicator")
        return dict(finding=finding, confidence=.9,
                    explanation=f"{data['proposed_degree']} proposed peers span "
                                f"{data['distinct_groups']} group(s): {pattern.replace('_', ' ')}.")
    conflict = data.get("conflict")
    return dict(finding="strong_indicator" if conflict else "no_indicator", confidence=.9,
                explanation="A spend conflict was observed." if conflict
                else "No conflicting spend was observed.")


class StandInAnalyst:
    """Duck-typed chat model accepted by ``astra.investigation``."""

    model_name = "astra-qwen3-small"  # selects the small-model code path

    def bind_tools(self, tools, **_):
        return _ToolCaller(tools)

    def with_structured_output(self, schema, method=None, **_):
        return _Structured(schema)
