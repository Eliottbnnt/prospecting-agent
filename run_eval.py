"""
Offline evaluation of the qualification rules on the synthetic dataset (no API keys needed).

    python evals/run_eval.py                 # prints a table + accuracy
    python evals/run_eval.py --langsmith     # also uploads the dataset and logs an experiment in LangSmith
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from scoring import Evidence, classify  # noqa: E402

CASES = json.load(open(os.path.join(os.path.dirname(__file__), "dataset.json"), encoding="utf-8"))["cases"]


def predict(evidence: dict) -> dict:
    q = classify(Evidence(**evidence))
    return {"classification": q.classification, "technical_fit": q.technical_fit, "buyer_fit": q.buyer_fit,
            "build_vs_buy_risk": q.build_vs_buy_risk, "evidence_confidence": q.evidence_confidence}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--langsmith", action="store_true")
    args = p.parse_args()
    ok = 0
    print(f"{'case':55} {'expected':25} {'predicted':25} tech buy risk")
    for c in CASES:
        r = predict(c["evidence"])
        hit = r["classification"] == c["expected"]
        ok += hit
        print(f"{c['name'][:54]:55} {c['expected']:25} {r['classification']:25} {r['technical_fit']}    {r['buyer_fit']}   "
              f"{r['build_vs_buy_risk']} {'OK' if hit else 'MISMATCH'}")
    print(f"\nQualification accuracy: {ok}/{len(CASES)}")

    if args.langsmith:
        if not os.getenv("LANGSMITH_API_KEY"):
            sys.exit("Set LANGSMITH_API_KEY in .env first.")
        from langsmith import Client, evaluate
        client = Client()
        name = "prospecting-agent-qualification (synthetic)"
        if not client.has_dataset(dataset_name=name):
            ds = client.create_dataset(name, description="Synthetic labelled scenarios for the scoring rules")
            client.create_examples(dataset_id=ds.id, inputs=[{"evidence": c["evidence"]} for c in CASES],
                                   outputs=[{"classification": c["expected"]} for c in CASES])
        evaluate(lambda inputs: predict(inputs["evidence"]), data=name,
                 evaluators=[lambda run, example: {"key": "classification_match",
                                                   "score": int(run.outputs["classification"] == example.outputs["classification"])}],
                 experiment_prefix="scoring-rules")
        print("Experiment logged: LangSmith > Datasets & Experiments")


if __name__ == "__main__":
    main()
