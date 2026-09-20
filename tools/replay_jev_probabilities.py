"""Recompute a historical Jev report from saved provider distributions only.

All HTTP is blocked. Original hard-choice logs/reports remain untouched.
Exact round, persona, model, prompt and typed-request matches are required.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_jev_backtest as runner
from ssa import harness, jev, personas, replies, series


def convert_record(source, entrant, rid, pid, prompt, input_hash):
    request, edges = jev.request_for(prompt, harness.model_id(entrant))
    if any(source.get(k) != v for k, v in {
            "entrant": entrant, "round_id": rid, "persona": pid,
            "model": request["model"], "prompt_sha256": replies.prompt_sha256(prompt)}.items()):
        raise ValueError("source reply identity/prompt mismatch")
    evidence = source["usage"]["jev"]
    if evidence["request"] != request or evidence["response"].get("model") != request["model"]:
        raise ValueError("source typed request/model mismatch")
    record = copy.deepcopy(source)
    record["reply"] = json.dumps(jev.convert(evidence["response"], request, edges), allow_nan=False)
    record["input_hash"] = input_hash
    record["usage"]["jev"]["adapter"] = harness.route(entrant)["params"]["adapter"]
    record["derived_from"] = {
        "input_hash": source["input_hash"],
        "record_sha256": runner.digest(source),
        "operation": "reconvert saved provider probabilities; no new inference"}
    return record


def replay(source_report, source_replies, destination):
    if destination.exists():
        raise ValueError("choose a new output directory; never overwrite prior evidence")
    old = runner.read(source_report)
    if old["status"] != "completed":
        raise ValueError("source backtest must be complete")
    tasks, _ = runner.plan(ROOT, runner.DEFAULT_SERIES, len(old["rounds"]))
    if {t["round"]["round_id"]: t["input_sha256"] for t in tasks} != {
            r["round_id"]: r["input_sha256"] for r in old["rounds"]}:
        raise ValueError("source report and current frozen inputs differ")
    by_round = {t["round"]["round_id"]: t for t in tasks}
    target_replies = destination / "replies"
    count = 0
    with patch.dict(os.environ, {"SSA_REPLIES_DIR": str(target_replies)}), \
            patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden in offline replay")), \
            patch.object(harness, "call_provider", side_effect=AssertionError("missing validated replay evidence")):
        for previous in old["records"]:
            entrant, rid = previous["entrant"], previous["round_id"]
            t = by_round[rid]
            source_hash = re.search(r"\bin=([0-9a-f]+)", previous["forecast"]["notes"])[1]
            if entrant == "jev":
                prompts = {None: harness.build_prompt(t["round"], t["history"])}
            else:
                spec = series.survey(t["round"]["series"])
                prompts = {p["id"]: harness.build_persona_prompt(p, spec) for p in personas.panel()}
            ih = harness.prompt_hash(entrant, "\n\n".join(prompts.values()))
            for pid, prompt in prompts.items():
                name = f"{entrant}.{source_hash}" + (f".{pid}" if pid is not None else "") + ".json"
                path = source_replies / rid / name
                source = runner.read(path)
                if source["input_hash"] != source_hash:
                    raise ValueError("source cache filename/content mismatch")
                converted = convert_record(source, entrant, rid, pid, prompt, ih)
                dest = Path(replies.path(rid, entrant, ih, persona=pid))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(converted, indent=2, allow_nan=False) + "\n")
                count += 1
        report = copy.deepcopy(old)
        report.update(persona_mode="probability-expectation-v2; original survey formula and sd",
                      persona_call_identity=harness.call_identity("jev-zeroshot-persona"),
                      source_report_sha256=hashlib.sha256(source_report.read_bytes()).hexdigest(),
                      verification={"http_calls": 0, "reconverted_replies": count,
                                    "direct_unchanged": True, "all_sd_unchanged": True})
        for record in report["records"]:
            rid, entrant = record["round_id"], record["entrant"]
            t = by_round[rid]
            previous = record["forecast"]["topline"]
            forecast = harness.forecast(entrant, t["round"], history=t["history"])
            if forecast["topline"]["sd"] != previous["sd"] or (
                    entrant == "jev" and forecast["topline"] != previous):
                raise ValueError("Direct output or original sd changed")
            if entrant.endswith("persona"):
                if "192/192 respondents, 192 replayed" not in forecast["notes"]:
                    raise ValueError("expected the complete cached panel")
            record["forecast"] = forecast
        report["scores"] = runner.summarize(report["records"])
        report["per_series_scores"] = []
        for sid in runner.DEFAULT_SERIES:
            records = [r for r in report["records"] if by_round[r["round_id"]]["round"]["series"] == sid]
            score = runner.summarize(records)
            report["per_series_scores"].append({
                "series": sid, "rounds": score["jev"]["matched"],
                "direct_crps": score["jev"]["mean_crps"],
                "persona_crps": score["jev-zeroshot-persona"]["mean_crps"],
                "persistence_crps": score["jev"]["persistence_crps"]})
        (destination / "results.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-report", type=Path, required=True)
    p.add_argument("--source-replies", type=Path, default=ROOT / "cache/jev/replies")
    p.add_argument("--out", type=Path, required=True, help="new private output directory")
    args = p.parse_args()
    report = replay(args.source_report, args.source_replies, args.out)
    print(json.dumps({"verification": report["verification"],
                      "per_series_scores": report["per_series_scores"]}, indent=2))


if __name__ == "__main__":
    main()
