"""Streamlit interface for the local ASTRA demonstration."""

from collections import Counter
from html import escape
from uuid import uuid4

import networkx as nx
import plotly.graph_objects as go
import streamlit as st

from astra.admission import MORE, PENDING, REVIEW, REVOKED, configured_model, database, decide, investigate, register, revoke
from astra.monitoring import (CHECKS, SCENARIOS as MONITORING_SCENARIOS, explain_finding, monitor,
                              record_observations, synthetic_batch)
from astra.policy import TRUST_THRESHOLD
from astra.simulation import RELAYS, SCENARIOS, established_topology


CONSOLE_STYLE = """
<style>
[data-testid="stMainBlockContainer"] {max-width: 1180px; padding-top: 2.6rem;}
[data-testid="stSidebar"] {border-right: 1px solid #dce3db;}
[data-testid="stMainBlockContainer"] h1 {font-size: clamp(2rem, 5vw, 2.65rem); letter-spacing: -.045em;}
[data-testid="stMainBlockContainer"] h2 {font-size: 1.35rem; letter-spacing: -.02em;}
[data-testid="stMetric"] {border-top: 1px solid #cdd8ce; padding-top: .55rem;}
[data-testid="stMetricValue"] {font-size: 1.7rem; font-variant-numeric: tabular-nums;}
.astra-masthead {font-size: .78rem; font-weight: 700; letter-spacing: .09em; color: #17665c;}
.astra-kicker {color: #4e635a; font-size: .78rem; margin-bottom: .35rem;}
.astra-state {display: inline-block; padding: .25rem .55rem; border: 1px solid #d7dfd5;
              border-radius: .3rem; background: #f0f2ed; color: #283c34; font-size: .83rem;}
.astra-state--approved {color: #135244; background: #e5f1eb; border-color: #bcd9ca;}
.astra-state--review {color: #6d4b16; background: #faf0db; border-color: #e8d7ae;}
.astra-state--closed {color: #812e29; background: #f8eae6; border-color: #eacdc6;}
</style>
"""


def status_label(value):
    return value.replace("_", " ").title()


def score_label(value):
    return "Unavailable" if value is None else f"{value:.3f}"


def topology_figure(graph, candidate):
    positions = nx.spring_layout(graph, seed=17)
    figure = go.Figure()
    for proposed, name, color in ((False, "Established link", "#93a29c"),
                                  (True, "Proposed link", "#986319")):
        x, y = [], []
        for source, target, attrs in graph.edges(data=True):
            if attrs.get("proposed", False) == proposed:
                a, b = positions[source], positions[target]
                x.extend([a[0], b[0], None])
                y.extend([a[1], b[1], None])
        figure.add_scatter(x=x, y=y, mode="lines", name=name, hoverinfo="skip",
                           line={"color": color, "dash": "dot" if proposed else "solid"})
    proposed_id = candidate["id"] if candidate["status"] != "APPROVED" else None
    for name, color, symbol, nodes in (
        ("Initial relay", "#41556c", "circle", [node for node in graph if node in RELAYS]),
        ("Approved member", "#187165", "circle",
         [node for node in graph if node not in RELAYS and node != proposed_id]),
        ("Candidate", "#986319", "diamond", [node for node in graph if node == proposed_id]),
    ):
        if nodes:
            figure.add_scatter(x=[positions[node][0] for node in nodes],
                               y=[positions[node][1] for node in nodes],
                               name=name, mode="markers+text",
                               text=["Candidate" if node == proposed_id else node[:10] for node in nodes],
                               textposition="top center", hovertext=nodes, hoverinfo="text",
                               marker={"size": 13, "color": color, "symbol": symbol,
                                       "line": {"width": 1.5, "color": "#fcfbf8"}})
    figure.update_layout(
        template="plotly_white", height=365, margin=dict(l=24, r=24, t=18, b=12),
        paper_bgcolor="#f1f3ee", plot_bgcolor="#f1f3ee", font={"color": "#293e36", "size": 11},
        xaxis={"visible": False}, yaxis={"visible": False},
        legend={"orientation": "h", "y": -.06, "x": 0},
    )
    return figure


st.set_page_config(page_title="ASTRA | Network Operations", layout="wide")
st.markdown(CONSOLE_STYLE, unsafe_allow_html=True)
store = database()
st.title("Network operations")
st.caption("Node admission, investigations and post-admission monitoring")

with st.sidebar:
    st.markdown('<p class="astra-masthead">ASTRA</p>', unsafe_allow_html=True)
    st.caption("Private blockchain simulator")
    st.divider()
    st.header("New admission")
    with st.form("register"):
        scenario = st.selectbox("Evidence scenario", list(SCENARIOS), format_func=SCENARIOS.get)
        seed = st.number_input("Scenario seed", min_value=0, max_value=2**32 - 1, value=41)
        if st.form_submit_button("Submit admission request", type="primary"):
            candidate = register(store, scenario, seed)
            st.session_state["candidate"] = candidate["id"]

candidates = store.list()
counts = Counter(candidate["status"] for candidate in candidates)
columns = st.columns(4)
for col, label, value in zip(columns,
                             ("Admission requests", "To investigate", "To review", "Approved members"),
                             (len(candidates), counts[PENDING] + counts[MORE],
                              counts[REVIEW], counts["APPROVED"])):
    col.metric(label, value)

if not candidates:
    st.info("Submit a synthetic admission request to inspect its wallet, peers and ledger.")
    st.stop()

by_id = {row["id"]: row for row in candidates}
with st.container(border=True):
    selection, state_column = st.columns([3, 1], vertical_alignment="center")
    candidate_id = selection.selectbox(
        "Admission request", list(by_id), key="candidate",
        format_func=lambda item: f"{SCENARIOS[by_id[item]['scenario']]} · {item[:10]}",
    )
    candidate = by_id[candidate_id]
    tone = {"APPROVED": "approved", REVIEW: "review", "REJECTED": "closed", REVOKED: "closed"}
    state_column.markdown(
        f'<div class="astra-kicker">Membership status</div>'
        f'<span class="astra-state astra-state--{tone.get(candidate["status"], "pending")}">'
        f'{escape(status_label(candidate["status"]))}</span>', unsafe_allow_html=True,
    )
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
    st.plotly_chart(topology_figure(graph, candidate), use_container_width=True, theme=None,
                    config={"displayModeBar": False, "scrollZoom": False})
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
            st.subheader("Supervisor review")
            st.write(report["supervisor_review"]["summary"])
            st.caption("Dispatch order · " + " → ".join(name.title() for name in report["order"]))
            with st.expander("Planning rationale"):
                st.write(report["planning"]["rationale"])
            for name in report["order"]:
                result = report["results"][name]
                assessment = result["assessment"]
                with st.expander(f"{name.title()} · {status_label(assessment['status'])}"):
                    st.write(assessment["explanation"])
                    st.caption(f"Risk {score_label(assessment['risk'])} · "
                               f"Confidence {score_label(assessment['confidence'])}")
                    st.json(assessment, expanded=False)
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
        st.subheader("Admission assessment")
        score, cutoff, recommendation = st.columns(3)
        score.metric("Trust factor", score_label(trust["factor"]))
        cutoff.metric("Acceptance threshold", f"{TRUST_THRESHOLD:.2f}")
        recommendation.metric("Recommendation", status_label(trust["recommendation"]))
        if trust.get("adjusted_attack_risk") is not None:
            st.caption(f"Original weighted formula {score_label(trust.get('original_factor'))} · "
                       f"Adjusted attack risk {score_label(trust['adjusted_attack_risk'])}")
        if trust.get("formula_recommendation") != trust["recommendation"]:
            st.caption("The Trust Factor alone meets the acceptance threshold. A high-risk finding requires review.")
        for item in trust.get("escalations", []):
            st.warning(f"{item['dimension'].title()} review required. {item['reason']}")
        if trust.get("risks"):
            st.table([{"Specialist": name.title(), "Risk": score_label(risk)}
                      for name, risk in trust["risks"].items()])
        with st.expander("Scoring inputs and saved fields"):
            st.json(trust)
    else:
        st.info("Run an investigation to see the admission assessment.")
with admin_tab:
    st.subheader("Membership decision")
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
    st.subheader("Admission audit trail")
    st.dataframe(store.events(candidate_id), use_container_width=True)

with monitor_tab:
    st.subheader("Monitoring window")
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
