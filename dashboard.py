"""Streamlit interface for the local ASTRA demonstration."""

from collections import Counter
from uuid import uuid4

import networkx as nx
import plotly.graph_objects as go
import streamlit as st

from astra.admission import MORE, PENDING, REVIEW, REVOKED, configured_model, database, decide, investigate, register, revoke
from astra.monitoring import (CHECKS, SCENARIOS as MONITORING_SCENARIOS, explain_finding, monitor,
                              record_observations, synthetic_batch)
from astra.simulation import RELAYS, SCENARIOS, established_topology


def topology_figure(graph, candidate):
    positions = nx.spring_layout(graph, seed=17)
    figure = go.Figure()
    for proposed, name, color in ((False, "Established", "#526782"),
                                  (True, "Proposed", "#a78bfa")):
        x, y = [], []
        for source, target, attrs in graph.edges(data=True):
            if attrs.get("proposed", False) == proposed:
                a, b = positions[source], positions[target]
                x.extend([a[0], b[0], None])
                y.extend([a[1], b[1], None])
        figure.add_scatter(x=x, y=y, mode="lines", name=name, hoverinfo="skip",
                           line={"color": color, "dash": "dot" if proposed else "solid"})
    labels = ["Candidate" if node == candidate["id"] and candidate["status"] != "APPROVED"
              else node[:10] for node in graph]
    colors = ["#38bdf8" if node in RELAYS else
              "#a78bfa" if node == candidate["id"] and candidate["status"] != "APPROVED"
              else "#4ade80" for node in graph]
    figure.add_scatter(x=[positions[node][0] for node in graph],
                       y=[positions[node][1] for node in graph],
                       mode="markers+text", text=labels, textposition="top center",
                       hovertext=list(graph), hoverinfo="text",
                       marker={"size": 15, "color": colors}, showlegend=False)
    figure.update_layout(template="plotly_dark", height=370, margin=dict(l=20, r=20, t=25, b=20),
                         xaxis={"visible": False}, yaxis={"visible": False})
    return figure


st.set_page_config(page_title="ASTRA | Node Admission", layout="wide")
store = database()
st.title("ASTRA")
st.markdown("**Agentic Blockchain Security Platform**")
st.caption("Layer 1 admission and Layer 2 monitoring · Synthetic evidence · Local administrator console")

with st.sidebar:
    st.header("Create a candidate")
    with st.form("register"):
        scenario = st.selectbox("Evidence scenario", list(SCENARIOS), format_func=SCENARIOS.get)
        seed = st.number_input("Scenario seed", min_value=0, max_value=2**32 - 1, value=41)
        if st.form_submit_button("Submit admission request", type="primary"):
            candidate = register(store, scenario, seed)
            st.session_state["candidate"] = candidate["id"]

candidates = store.list()
counts = Counter(candidate["status"] for candidate in candidates)
columns = st.columns(6)
for col, label, value in zip(columns,
                             ("Requests", "Awaiting investigation", "Approved", "Rejected",
                              "With reports", "Reports to review"),
                             (len(candidates), counts[PENDING] + counts[MORE], counts["APPROVED"],
                              counts["REJECTED"], sum(bool(item["report"]) for item in candidates),
                              counts[REVIEW])):
    col.metric(label, value)

if not candidates:
    st.info("Submit a synthetic admission request to inspect its wallet, peers and ledger.")
    st.stop()

by_id = {row["id"]: row for row in candidates}
candidate_id = st.selectbox(
    "Admission request", list(by_id), key="candidate",
    format_func=lambda item: f"{SCENARIOS[by_id[item]['scenario']]} · {item[:10]}",
)
candidate = by_id[candidate_id]
st.write(candidate["status"].replace("_", " ").title())
if candidate["status"] in (PENDING, MORE) and st.button("Run investigation", type="primary"):
    try:
        with st.status("Collecting evidence", expanded=False) as status:
            investigate(store, candidate_id, configured_model(),
                        progress=lambda label: status.update(label=label))
        st.rerun()
    except ValueError as error:
        st.error(str(error))

report = candidate["report"]
evidence_tab, agents_tab, trust_tab, admin_tab, monitor_tab = st.tabs(
    ["Network & ledger", "Investigation", "Trust analysis", "Administrator", "Post-admission monitoring"]
)
with evidence_tab:
    evidence = report.get("evidence") if report else None
    graph = established_topology(candidates)
    st.subheader("Current network")
    st.caption(f"{graph.number_of_nodes()} active replicas · {counts['APPROVED']} approved candidates")
    if candidate["status"] not in ("APPROVED", "REJECTED", REVOKED):
        graph.add_node(candidate_id)
        for item in (evidence or {}).get("eclipse", {}).get("observations", []):
            if item["peer"] in graph:
                graph.add_edge(candidate_id, item["peer"], proposed=True)
    st.plotly_chart(topology_figure(graph, candidate), use_container_width=True)
    if evidence:
        st.subheader("Investigation ledger")
        replica_count = evidence["network"].get("replica_count")
        if replica_count is not None:
            st.caption(f"Validated by {replica_count} replicas when this investigation ran.")
        st.write("Replica validation", "Passed" if evidence["network"]["replicas_agree"] else "Failed")
        st.dataframe([{k: v for k, v in block.items() if k != "transactions"}
                      for block in evidence["blocks"]], use_container_width=True)
        with st.expander("Transactions"):
            st.dataframe([tx for block in evidence["blocks"] for tx in block["transactions"]],
                         use_container_width=True)
    else:
        st.info("Network and ledger evidence appears after an investigation.")
with agents_tab:
    if report:
        st.caption(f"Investigation {report['investigation_id']} · {report['elapsed_seconds']} s")
        if "error" in report:
            st.error(f"Investigation failed during evidence collection ({report['error']}).")
        else:
            st.write(report["planning"]["rationale"])
            st.write("Dispatch order", " → ".join(report["order"]))
            st.write(report["supervisor_review"]["summary"])
            for name in report["order"]:
                result = report["results"][name]
                with st.expander(name.title()):
                    st.json(result["assessment"])
                    for call in result["calls"]:
                        st.write(f"{call['tool']} · {call['id']}")
                        st.json(call["result"], expanded=False)
            if report["evidence"]["wallet"]["model"]:
                st.write("Wallet anomaly model")
                st.json(report["evidence"]["wallet"]["model"])
    else:
        st.info("Run an investigation to see the dispatch and evidence.")
with trust_tab:
    if report:
        trust = report["trust"]
        st.metric("Trust factor", f"{trust['factor']:.3f}" if trust["factor"] is not None else "Unavailable")
        if trust.get("adjusted_attack_risk") is not None:
            st.caption(f"Original weighted formula {trust['original_factor']:.3f} · "
                       f"Adjusted attack risk {trust['adjusted_attack_risk']:.3f}")
        st.markdown(f"**Recommendation: {trust['recommendation'].replace('_', ' ').title()}**")
        if trust.get("formula_recommendation") != trust["recommendation"]:
            st.caption("The Trust Factor alone meets the acceptance threshold. A high-risk finding requires review.")
        for item in trust.get("escalations", []):
            st.warning(f"{item['dimension'].title()} review required. {item['reason']}")
        st.json(trust)
with admin_tab:
    if candidate["status"] == REVIEW:
        with st.form("decision"):
            action = st.radio("Decision", ["approve", "reject", "more"], horizontal=True,
                              format_func=lambda choice: {"approve": "Approve", "reject": "Reject",
                                                          "more": "Request more evidence"}[choice])
            note = st.text_area("Administrator note", max_chars=1000)
            if st.form_submit_button("Record decision"):
                try:
                    decide(store, candidate_id, action, note)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
    if candidate["status"] == "APPROVED":
        with st.form("revocation"):
            reason = st.text_area("Membership revocation reason", max_chars=1000)
            if st.form_submit_button("Revoke membership"):
                try:
                    revoke(store, candidate_id, reason)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
    st.write("Admission audit trail")
    st.dataframe(store.events(candidate_id), use_container_width=True)

with monitor_tab:
    if candidate["status"] not in ("APPROVED", REVOKED):
        st.info("Post-admission monitoring begins after the selected candidate is approved.")
    else:
        st.caption("Each observation is a synthetic transaction-linked sample.")
        if candidate["status"] == REVOKED:
            st.info("Membership revoked. This node’s monitoring history is read-only.")
        else:
            scenario = st.selectbox("Monitoring scenario", list(MONITORING_SCENARIOS),
                                    format_func=MONITORING_SCENARIOS.get)
            if st.button("Record 10 synthetic observations"):
                try:
                    record_observations(store, candidate_id, synthetic_batch(scenario, uuid4().hex))
                except ValueError as error:
                    st.error(str(error))
        monitoring = monitor(store, candidate_id)
        latest = monitoring["latest"]
        st.metric("Observations recorded", monitoring["count"])
        if latest["status"] == "observing":
            st.info(f"Collecting the first {latest['window_size']} observations.")
        elif latest["status"] == "review":
            st.warning("The current window contains indicators for administrator review.")
        elif latest["status"] == "incomplete":
            st.info("The current window has incomplete telemetry.")
        else:
            st.success("The current window contains no flagged indicators.")
        for name, item in latest["findings"].items():
            with st.expander(f"{CHECKS[name]} · {item['status'].title()}"):
                st.write(item["explanation"])
                st.json({"metrics": item["metrics"], "observation_ids": item["observation_ids"]})
        if latest["findings"]:
            selected = st.selectbox("Finding to explain", list(latest["findings"]),
                                    format_func=CHECKS.get)
            key = (candidate_id, monitoring["count"], selected)
            if st.button("Explain with LLM"):
                try:
                    with st.spinner("Reading and interpreting monitoring evidence"):
                        st.session_state["monitoring_explanation"] = (
                            key, explain_finding(configured_model(), selected, latest)
                        )
                except ValueError as error:
                    st.error(str(error))
                except Exception as error:
                    st.error(f"Model explanation unavailable ({type(error).__name__}).")
            saved = st.session_state.get("monitoring_explanation")
            if saved and saved[0] == key:
                st.json(saved[1])
        if monitoring["observations"]:
            with st.expander("Recent observations"):
                st.dataframe([{**entry["payload"], "recorded_at": entry["time"],
                               "window_status": entry["report"]["status"]}
                              for entry in monitoring["observations"]], use_container_width=True)
