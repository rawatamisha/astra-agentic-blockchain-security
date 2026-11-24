"""Reproducible synthetic evidence and independent in-process replicas."""

import random
from dataclasses import asdict
from functools import lru_cache

import networkx as nx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sklearn.ensemble import IsolationForest

from astra.attack_evidence import conflict_features, peer_features, sybil_features, wallet_signal_features

from astra.chain import Chain, Transaction, digest, identity, public

SCENARIOS = {
    "normal": "Ordinary activity",
    "sybil": "Shared funding and synchronized identities",
    "eclipse": "Concentrated peer observations",
    "wallet": "Unusual wallet activity",
    "conflict": "Conflicting spend observations",
    "sparse": "Insufficient historical evidence",
}


RELAYS = tuple(f"relay-{index}" for index in range(6))


def established_topology(candidates):
    graph = nx.cycle_graph(RELAYS)
    for candidate in candidates:
        if candidate["status"] != "APPROVED":
            continue
        node_id = candidate["id"]
        evidence = (candidate.get("report") or {}).get("evidence", {})
        observations = evidence.get("eclipse", {}).get("observations", [])
        peers = {item["peer"] for item in observations if item["peer"] in RELAYS}
        graph.add_node(node_id)
        graph.add_edges_from((node_id, peer) for peer in sorted(peers or {"relay-0"}))
    return graph


def network_snapshot(graph):
    return {
        "nodes": list(graph),
        "edges": [{"source": a, "target": b, "proposed": attrs.get("proposed", False)}
                  for a, b, attrs in graph.edges(data=True)],
    }


class Network:
    def __init__(self, allocations, candidates=()):
        self.topology = established_topology(candidates)
        self.chains = {node: Chain(allocations) for node in self.topology}

    def publish(self, transactions, timestamp):
        source = "relay-0"
        block = self.chains[source].append(transactions, timestamp)
        for _, receiver in nx.bfs_edges(self.topology, source):
            self.chains[receiver].receive(block)


def signing_key(seed, label):
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(digest([seed, label])))


def histories(rng, unusual=False):
    count = 28 if unusual else rng.randint(4, 9)
    interval = 1 if unusual else rng.randint(11, 19)
    return [(i * interval + 2,
             500 if unusual else rng.randint(2, 30),
             0 if unusual else i % 4) for i in range(count)]


def wallet_features(rows):
    return [len(rows), sum(row[1] for row in rows) / len(rows),
            len({row[2] for row in rows}),
            len(rows) / max(1, max(row[0] for row in rows) - min(row[0] for row in rows))]


@lru_cache(maxsize=1)
def train_wallet_baseline():
    rng = random.Random(803)
    training = [wallet_features(histories(rng)) for _ in range(160)]
    model = IsolationForest(random_state=803, n_jobs=1).fit(training)
    return model, model.score_samples(training)


def wallet_anomaly(rows):
    model, baseline = train_wallet_baseline()
    values = wallet_features(rows)
    score = model.score_samples([values])[0]
    return {"features": dict(zip(("count", "mean_amount", "counterparties", "frequency"), values)),
            "isolation_score": float(score),
            "lower_tail_percentile": float((baseline <= score).mean()),
            "training_size": len(baseline)}


def collect(candidate, scenario, seed, members=()):
    rng = random.Random(seed)
    wallet = candidate["wallet"]
    treasury = signing_key(seed, "treasury")
    network = Network({public(treasury): 100_000}, members)
    rows = [] if scenario == "sparse" else histories(rng, scenario == "wallet")
    targets = [public(signing_key(seed, str(i))) for i in range(4)]
    ledgers = [(wallet, 1, 20_000)] if rows else []
    cohort = {}
    if scenario != "sparse":
        for index in range(3):
            peer_wallet = public(signing_key(seed, f"identity-{index}"))
            timestamp = 1 if scenario == "sybil" else 25 + index
            ledgers.append((peer_wallet, timestamp, 15))
            identity_key = public(signing_key(seed, f"observed-node-{index}"))
            cohort[peer_wallet] = {"node_id": digest(identity_key), "wallet": peer_wallet,
                                   "first_seen": timestamp}
    funding = [Transaction.sign(treasury, recipient, amount, nonce, timestamp)
               for nonce, (recipient, timestamp, amount) in
               enumerate(sorted(ledgers, key=lambda row: row[1]))]
    wallet_key = signing_key(seed, "candidate")
    transfers = [Transaction.sign(wallet_key, targets[target], amount, nonce, timestamp)
                 for nonce, (timestamp, amount, target) in enumerate(rows)]
    for tx in sorted([*funding, *transfers], key=lambda item: item.timestamp):
        network.publish([tx], tx.timestamp)

    authority = network.chains["relay-0"]
    transactions = [tx for block in authority.blocks for tx in block.transactions]
    history = [asdict(tx) for tx in transactions if wallet in (tx.recipient, tx.sender)]
    outgoing = [(tx.timestamp, tx.amount, tx.recipient) for tx in transactions if tx.sender == wallet]
    funding_graph = nx.DiGraph((tx.sender, tx.recipient) for tx in transactions)
    candidate_funders = set(funding_graph.predecessors(wallet)) if wallet in funding_graph else set()
    aligned = [tx.recipient for tx in transactions
               if tx.sender in candidate_funders and tx.recipient in cohort
               and history and tx.timestamp == history[0]["timestamp"]]

    observations = [] if scenario == "sparse" else [
        {"peer": f"relay-{index}", "group": 0 if scenario == "eclipse" else index}
        for index in range(4)
    ]
    groups = {item["group"] for item in observations}
    graph = network.topology.copy()
    graph.add_node(candidate["node_id"])
    for item in observations:
        graph.add_edge(candidate["node_id"], item["peer"], proposed=True)

    # Historical records from a synthetic observer. These never enter the ledger.
    spends = []
    if rows:
        spends.append({"wallet": wallet, "nonce": 0, "time": 200, "recipient": targets[0]})
    if scenario == "conflict":
        spends.append({"wallet": wallet, "nonce": 0, "time": 204, "recipient": targets[1]})
    spend_metrics = conflict_features(spends)

    return {
        "network": {
            **network_snapshot(graph),
            "replica_count": len(network.chains),
            "replicas_agree": all(chain.replay() == authority.replay()
                                  and chain.blocks[-1] == authority.blocks[-1]
                                  for chain in network.chains.values()),
        },
        "blocks": [asdict(block) for block in authority.blocks],
        "wallet": {"available": bool(rows), "history": history,
                   "balance": authority.replay().get(wallet, 0),
                   "signals": wallet_signal_features(outgoing, transactions, wallet),
                   "model": wallet_anomaly(outgoing) if outgoing else None},
        "sybil": {"available": bool(rows), "funders": sorted(candidate_funders),
                  "observed_identities": list(cohort.values()),
                  "signals": sybil_features(transactions, wallet, cohort, candidate_funders,
                                              history[0]["timestamp"] if history else None),
                  "same_timestamp_recipients": aligned},
        "eclipse": {"available": bool(observations), "observations": observations,
                    "signals": peer_features(graph, candidate["node_id"], observations, RELAYS),
                    "distinct_groups": len(groups), "proposed_degree": graph.degree(candidate["node_id"])},
        "finney": {"available": bool(spends), "observations": spends,
                   "conflict": bool(spend_metrics["conflicting_spend_pairs"]),
                   "signals": spend_metrics, "private_block_evidence": None},
    }
