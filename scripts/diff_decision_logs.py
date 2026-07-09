"""Compare two JSONL decision logs from shadow/backtest runs."""

import argparse
import json
from pathlib import Path


def load(path: Path) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _quote_tuple(intent: dict | None):
    if not intent or not intent.get("quote"):
        return None
    quote = intent["quote"]
    return (
        quote.get("bid_price"),
        quote.get("ask_price"),
        quote.get("bid_size"),
        quote.get("ask_size"),
    )


def compare(reference: list[dict], candidate: list[dict]) -> dict:
    common = min(len(reference), len(candidate))
    mismatches = {
        "intent_presence": 0,
        "target_inventory": 0,
        "quote_shape": 0,
        "decision": 0,
        "urgency": 0,
    }
    for ref, cand in zip(reference[:common], candidate[:common]):
        ref_intent = ref.get("intent")
        cand_intent = cand.get("intent")
        if bool(ref_intent) != bool(cand_intent):
            mismatches["intent_presence"] += 1
        if (ref_intent or {}).get("target_inventory") != (cand_intent or {}).get("target_inventory"):
            mismatches["target_inventory"] += 1
        if _quote_tuple(ref_intent) != _quote_tuple(cand_intent):
            mismatches["quote_shape"] += 1
        if "decision" in ref and "decision" in cand and ref.get("decision") != cand.get("decision"):
            mismatches["decision"] += 1
        if "urgency" in ref and "urgency" in cand and ref.get("urgency") != cand.get("urgency"):
            mismatches["urgency"] += 1
    return {
        "reference_rows": len(reference),
        "candidate_rows": len(candidate),
        "compared_rows": common,
        "mismatches": mismatches,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reference", type=Path)
    ap.add_argument("candidate", type=Path)
    args = ap.parse_args()

    result = compare(load(args.reference), load(args.candidate))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
