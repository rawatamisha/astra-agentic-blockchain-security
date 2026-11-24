"""Run synthetic admission scenarios and emit their actual records as JSON."""

import argparse
import json
from tempfile import TemporaryDirectory
from pathlib import Path

from astra.admission import configured_model, investigate, register
from astra.simulation import SCENARIOS
from astra.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.seed <= 2**32 - 1:
        parser.error("seed must be an unsigned 32-bit integer")
    model = configured_model() if args.live_model else None
    if args.live_model and model is None:
        parser.error("configure ASTRA_MODEL and ASTRA_API_KEY before using --live-model")
    records = []
    with TemporaryDirectory() as directory:
        store = Store(Path(directory) / "experiment.sqlite3")
        for name in [args.scenario] if args.scenario else SCENARIOS:
            entry = register(store, name, args.seed)
            result = investigate(store, entry["id"], model)
            records.append({"scenario": name, "seed": args.seed, "synthetic": True,
                            "model_enabled": model is not None, "administrator_decision": None,
                            "report": result["report"], "events": store.events(entry["id"])})
    data = json.dumps(records, indent=2)
    if args.output:
        args.output.write_text(data + "\n", encoding="utf-8")
    else:
        print(data)


if __name__ == "__main__":
    main()
