"""Read-only measurements for pre-admission attack specialists."""

from collections import Counter, defaultdict
from statistics import mean

import networkx as nx


def ratio(count, total):
    return count / total if total else None


def sybil_features(transactions, wallet, cohort, funders, first_seen):
    outgoing = defaultdict(set)
    prior_receipts = defaultdict(list)
    candidate_payments = []
    funding_hops = 0
    for tx in transactions:
        if tx.recipient == wallet and tx.sender in funders:
            recent = [item for item in prior_receipts[tx.sender]
                      if item.sender != wallet and 0 <= tx.timestamp - item.timestamp <= 300]
            funding_hops += any(tx.amount >= .4 * item.amount for item in recent)
        prior_receipts[tx.recipient].append(tx)
        outgoing[tx.sender].add(tx.recipient)
        if tx.sender == wallet:
            candidate_payments.append(tx)
    overlap = []
    for sibling in cohort:
        if outgoing[wallet] and outgoing[sibling]:
            total = outgoing[wallet] | outgoing[sibling]
            overlap.append(len(outgoing[wallet] & outgoing[sibling]) / len(total))
    linked = {wallet, *cohort}
    return {
        "shared_funders": len(funders),
        "cohort_size": len(cohort),
        "recent_pass_through_funding_observations": funding_hops,
        "closed_cluster_transfer_share": ratio(
            sum(tx.recipient in linked for tx in candidate_payments), len(candidate_payments)
        ),
        "mean_counterparty_similarity": mean(overlap) if overlap else None,
        "observed_account_age": max((tx.timestamp for tx in transactions), default=0) - first_seen
        if first_seen is not None else None,
        "funding_source_reputation": None,
    }


def wallet_signal_features(outgoing, history, wallet):
    amounts = Counter(row[1] for row in outgoing)
    timestamps = [row[0] for row in outgoing]
    gaps = [after - before for before, after in zip(timestamps, timestamps[1:])]
    first_seen = {}
    fresh = 0
    for tx in history:
        if tx.sender == wallet and tx.recipient not in first_seen:
            fresh += 1
        first_seen.setdefault(tx.sender, tx.timestamp)
        first_seen.setdefault(tx.recipient, tx.timestamp)
    return {
        "burst_share_under_10_ticks": ratio(sum(gap < 10 for gap in gaps), len(gaps)),
        "repeated_amount_share": ratio(sum(count for count in amounts.values() if count > 1), len(outgoing)),
        "first_seen_recipient_share": ratio(fresh, len(outgoing)),
        "failed_transaction_share": None,
        "ethereum_dust_share": None,
    }


def peer_features(graph, candidate_id, observations, relays):
    if not observations:
        return {"group_hhi": None, "backbone_share": None, "independent_routes": None,
                "peer_freshness": None, "tip_lag": None}
    groups = Counter(item["group"] for item in observations)
    core = "__trusted_core__"
    routes = graph.copy()
    routes.add_edges_from((relay, core) for relay in relays)
    return {
        "group_hhi": sum((size / len(observations)) ** 2 for size in groups.values()),
        "backbone_share": ratio(sum(item["peer"] in relays for item in observations), len(observations)),
        "independent_routes": sum(1 for _ in nx.node_disjoint_paths(routes, candidate_id, core)),
        "peer_freshness": None,
        "tip_lag": None,
    }


def conflict_features(spends):
    destinations = defaultdict(set)
    for event in spends:
        destinations[(event["wallet"], event["nonce"])].add(event["recipient"])
    return {"conflicting_spend_pairs": sum(len(recipients) > 1 for recipients in destinations.values()),
            "observed_spends": len(spends), "private_block_timing": None,
            "publication_delay": None}
