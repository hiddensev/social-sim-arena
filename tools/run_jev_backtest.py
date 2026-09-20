"""Local replay of resolved survey rounds through the original live harness.

Dry-run by default. Uses the committed questions, frozen inputs and resolutions;
never refreshes sources, files an arena forecast, or registers an entrant.
This is a functional historical backtest, not evidence of post-training skill:
Jev's training cutoff is not documented in the checked official model docs.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load before harness import, which reads some settings at import time.
from ssa import envfile
envfile.load(str(ROOT / ".env"))
from ssa import baselines, harness, jev, personas, scoring, series

ENTRANTS = ("jev", "jev-zeroshot-persona")
DEFAULT_SERIES = ("yougov_approval", "civiqs_net_approval", "umich_sentiment")


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def plan(root, selected, limit):
    resolutions = read(root / "resolutions/resolved.json")
    rounds = read(root / "questions/season0.json")["rounds"]
    tasks = []
    skipped = []
    for r in rounds:
        sid, rid = r.get("series"), r["round_id"]
        if sid not in selected or rid not in resolutions:
            continue
        resolution = resolutions[rid]
        lock_path = root / "locks" / (rid + ".json")
        if not lock_path.exists():
            skipped.append({"round_id": rid, "reason": "no committed input freeze"})
            continue
        frozen = read(lock_path)
        # The live harness gives models answer_history when present, rather
        # than history refreshed later at the round's final lock.
        history = frozen.get("answer_history", frozen.get("history"))
        target_date = resolution.get("observed_date")
        if (not history or not target_date or
                any(p["date"] >= target_date for p in history)):
            skipped.append({"round_id": rid, "reason": "missing or non-prospective history"})
            continue
        if series.survey(sid) is None:
            raise ValueError(f"{sid} has no Persona survey instrument")
        round_input = dict(r)
        round_input["baselines"] = baselines.all_baselines(history, r["release_at"][:10])
        # Validate adapter support before any paid calls.
        jev.request_for(harness.build_prompt(round_input, history), harness.model_id("jev"))
        tasks.append({"round": round_input, "history": history,
                      "outcome": resolution["value"], "observed_date": target_date,
                      "input_sha256": digest({"round": r, "freeze": frozen,
                                              "resolution": resolution})})
    tasks.sort(key=lambda t: (t["round"]["release_at"], t["round"]["round_id"]), reverse=True)
    return tasks[:limit], skipped


def summarize(records):
    """Score only rounds both methods answered, using upstream CRPS and skill."""
    ok = {e: {r["round_id"]: r for r in records
              if r["entrant"] == e and r.get("forecast")} for e in ENTRANTS}
    matched = sorted(set.intersection(*(set(v) for v in ok.values())))
    out = {}
    for entrant, rows in ok.items():
        crps = [scoring.crps_forecast(rows[r]["forecast"]["topline"], rows[r]["outcome"])
                for r in matched]
        base = [scoring.crps_forecast(rows[r]["persistence"], rows[r]["outcome"])
                for r in matched]
        mean = sum(crps) / len(crps) if crps else None
        baseline = sum(base) / len(base) if base else None
        out[entrant] = {"answered": len(rows), "matched": len(matched),
                        "mean_crps": mean, "persistence_crps": baseline,
                        "skill": scoring.skill(mean, baseline) if crps else None}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--series", default=",".join(DEFAULT_SERIES))
    ap.add_argument("--limit", type=int, default=1, help="latest resolved rounds; default 1")
    ap.add_argument("--max-calls", type=int, default=1000,
                    help="upper bound on requested model calls before replay-cache savings")
    ap.add_argument("--out", type=Path, default=ROOT / "cache/jev/backtest.json")
    args = ap.parse_args()
    if args.limit < 1 or args.max_calls < 1:
        ap.error("--limit and --max-calls must be positive")
    selected = [s.strip() for s in args.series.split(",") if s.strip()]
    if not selected or any(s not in series.SERIES for s in selected):
        ap.error("--series must contain registered series IDs")
    tasks, skipped = plan(ROOT, selected, args.limit)
    if not tasks:
        ap.error("no resolved rounds with valid frozen inputs for these series")
    count = len(tasks) * (1 + len(personas.panel()))
    report = {"status": "planned", "model": harness.model_id("jev"),
              "call_identity": harness.call_identity("jev"),
              "persona_call_identity": harness.call_identity("jev-zeroshot-persona"),
              "training_cutoff": None, "evaluation": "historical-functional-only",
              "persona_mode": "probability-expectation-v2; original survey formula and sd",
              "replicates": personas.REPLICATES,
              "upstream_commit": subprocess.check_output(
                  ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "max_provider_calls": count, "skipped": skipped,
              "rounds": [{"round_id": t["round"]["round_id"],
                          "input_sha256": t["input_sha256"]} for t in tasks],
              "records": []}
    print(f"{len(tasks)} round(s); {len(personas.panel())} personas per round; "
          f"at most {count} provider calls before cache replay.")
    print("Historical functional check only: Jev training cutoff unknown.")
    for task in tasks:
        print("  " + task["round"]["round_id"])
    if args.execute:
        if count > args.max_calls:
            ap.error(f"{count} planned calls exceeds --max-calls {args.max_calls}")
        if not harness.has_key("jev"):
            ap.error("set TYPESAFE_API_KEY in the repository's ignored .env")
        if harness.ALLOW_MOCK:
            ap.error("unset SSA_ALLOW_MOCK: real backtests must never use placeholders")
        os.environ["SSA_REPLIES_DIR"] = str(ROOT / "cache/jev/replies")
        for task in tasks:
            for entrant in ENTRANTS:
                r = task["round"]
                record = {"round_id": r["round_id"], "entrant": entrant,
                          "outcome": task["outcome"],
                          "persistence": r["baselines"]["persistence"],
                          "forecast": None, "error": None}
                try:
                    record["forecast"] = harness.forecast(entrant, r, history=task["history"])
                    if entrant == "jev-zeroshot-persona":
                        match = re.search(r"(\d+)/(\d+) respondents", record["forecast"]["notes"])
                        record["panel_complete"] = bool(match and match[1] == match[2])
                except Exception as exc:
                    record["error"] = f"{type(exc).__name__}: {exc}"
                report["records"].append(record)
                print(f"{entrant} {r['round_id']}: "
                      f"{record['error'] or record['forecast']['topline']}", flush=True)
        report["scores"] = summarize(report["records"])
        report["status"] = "failed" if any(r["error"] for r in report["records"]) else "completed"
        if report["status"] == "completed" and any(
                r.get("panel_complete") is False for r in report["records"]):
            report["status"] = "partial"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Report: {args.out}")
    if report["status"] in ("failed", "partial"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
