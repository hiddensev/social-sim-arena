"""Offline contract and end-to-end replay checks. No real credentials or HTTP."""
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssa import baselines, harness, jev, personas, series
from tools import run_jev_backtest as runner
from tools import replay_jev_probabilities as offline


def response_for(request):
    answers = {}
    for key, question in request["questions"].items():
        options = list(question["criteria"])
        # Simulate a heterogeneous panel using the unchanged persona text.
        choice = options[0] if "a Democrat." in request["state"] else options[-1]
        ps = dict.fromkeys(options, 0.0)
        if key == "forecast":
            ps = dict.fromkeys(options, 1 / len(options))
        else:
            ps[choice] = 1.0
        answers[key] = {"type": "choice", "choice": choice,
                        "probabilities": ps, "confidence": 1.0}
    return {"model": request["model"], "answers": answers,
            "usage": {"input_tokens": 300, "output_tokens": 20}}


class JevTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TYPESAFE_API_KEY": "offline-test-key",
                         "SSA_REPLIES_DIR": self.tmp.name,
                         "SSA_JEV_AUDIT_DIR": str(Path(self.tmp.name) / "audit")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.history = [{"date": f"2026-07-{d:02d}", "value": 40 + d / 10}
                        for d in range(1, 13)]
        self.round = {"round_id": "jev-test-round", "series": "yougov_approval",
                      "release_at": "2026-08-01T14:00:00Z",
                      "lock_at": "2026-07-30T14:00:00Z",
                      "baselines": baselines.all_baselines(self.history, "2026-08-01")}

    def http(self, url, **kwargs):
        self.assertEqual(url, "https://api.typesafe.ai/v1/systemone")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer offline-test-key")
        return Mock(status_code=200, json=lambda: response_for(kwargs["json"]))

    def test_direct_preserves_information_and_uniform_moments(self):
        prompt = harness.build_prompt(self.round, self.history)
        req, edges = jev.request_for(prompt, "jev-1.13.0")
        self.assertEqual(req["state"], prompt[:-len(harness.FOOTER)].rstrip())
        self.assertEqual(len(req["questions"]["forecast"]["criteria"]), 200)
        result = jev.convert(response_for(req), req, edges)
        self.assertAlmostEqual(result["mean"], 50)
        self.assertAlmostEqual(result["sd"], 100 / math.sqrt(12))

    def test_single_bin_has_declared_within_bin_uncertainty(self):
        req, edges = jev.request_for(harness.build_prompt(self.round, []), "jev-1.13.0")
        data = response_for(req)
        answer = data["answers"]["forecast"]
        answer["choice"] = "bin_080"
        answer["probabilities"] = {k: float(k == "bin_080") for k in answer["probabilities"]}
        result = jev.convert(data, req, edges)
        self.assertAlmostEqual(result["mean"], 40.25)
        self.assertAlmostEqual(result["sd"], 0.5 / math.sqrt(12))

    def test_declared_net_and_ics_support(self):
        for sid, bounds in [("civiqs_net_approval", (-100, 100)),
                            ("umich_sentiment", (2, 2 + 1000 / personas.ICS_BASE))]:
            r = dict(self.round, series=sid)
            _, edges = jev.request_for(harness.build_prompt(r, []), "jev-1.13.0")
            self.assertEqual((edges[0], edges[-1]), bounds)

    def test_probability_validation(self):
        cases = [{"a": -0.1, "b": 1.1}, {"a": float("nan"), "b": 1},
                 {"a": True, "b": 0}, {"a": 0.2, "b": 0.3}, {"a": 1},
                 {"a": float("inf"), "b": 0}, {"a": 0.1, "b": 0.9}]
        for ps in cases:
            with self.subTest(ps=ps), self.assertRaises(ValueError):
                jev.probabilities({"type": "choice", "choice": "a", "probabilities": ps}, ["a", "b"])

    def test_observed_hundredth_quantization_has_bounded_normalization(self):
        for ps in ({"a": 0.66, "b": 0.33}, {"a": 0.67, "b": 0.34}):
            normalized = jev.probabilities({"type": "choice", "choice": "a", "probabilities": ps}, ["a", "b"])
            self.assertAlmostEqual(sum(normalized.values()), 1)
            self.assertAlmostEqual(normalized["a"] / normalized["b"], ps["a"] / ps["b"])
        for ps in ({"a": 0.65, "b": 0.33}, {"a": 0.659, "b": 0.331}):
            with self.assertRaises(ValueError):
                jev.probabilities({"type": "choice", "choice": "a", "probabilities": ps}, ["a", "b"])

    def test_persona_exact_instrument_and_probability_answers(self):
        spec = series.survey("umich_sentiment")
        prompt = harness.build_persona_prompt(personas.panel()[0], spec)
        req, edges = jev.request_for(prompt, "jev-1.13.0")
        self.assertIsNone(edges)
        for item in spec["items"]:
            self.assertEqual(req["questions"][item["key"]]["instructions"], item["text"])
            self.assertEqual(list(req["questions"][item["key"]]["criteria"]), item["options"])
        converted = jev.convert(response_for(req), req, edges)
        self.assertEqual(jev.parse_persona_reply(json.dumps(converted), spec), converted)
        with self.assertRaises(ValueError):
            harness.parse_survey_reply(json.dumps(converted), spec)
        self.assertNotIn("Recent published", req["state"])

    def test_probability_reply_rejects_hard_answers_and_invalid_vectors(self):
        spec = series.survey("yougov_approval")
        bad = ["approve", {"approve": 1},
               {"approve": .6, "disapprove": .2, "not sure": .1},
               {"approve": True, "disapprove": 0, "not sure": 0},
               {"approve": float("nan"), "disapprove": 0, "not sure": 0}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                jev.parse_persona_reply(json.dumps({"approval": value}), spec)

    def test_affine_aggregate_matches_expectation_of_correlated_hard_panels(self):
        # Two mutually exclusive whole-panel scenarios: strong correlation
        # across people and Michigan items. Marginals suffice for the mean.
        weights = {"p1": .27, "p2": .73}
        for sid in runner.DEFAULT_SERIES:
            spec = series.survey(sid)
            scenarios = [
                {pid: {i["key"]: i["options"][(which + n) % 3]
                       for i in spec["items"]}
                 for n, pid in enumerate(weights)} for which in (0, 2)]
            ps = {pid: {i["key"]: {o: sum(p for p, s in zip((.35, .65), scenarios)
                                            if s[pid][i["key"]] == o)
                                  for o in i["options"]} for i in spec["items"]}
                  for pid in weights}
            expected = sum(p * personas.aggregate(spec["aggregate"], s, weights)
                           for p, s in zip((.35, .65), scenarios))
            self.assertAlmostEqual(jev.aggregate_persona_probabilities(spec["aggregate"], ps, weights), expected)
        with self.assertRaises(ValueError):
            jev.aggregate_persona_probabilities("nonlinear_unreviewed", {}, {})

    def test_soft_persona_preserves_probability_instead_of_argmax(self):
        def soft_http(url, **kwargs):
            data = response_for(kwargs["json"])
            data["answers"]["approval"].update(choice="approve", probabilities={
                "approve": .6, "disapprove": .35, "not sure": .05})
            return Mock(status_code=200, json=lambda: data)
        with patch.object(personas, "REPLICATES", 1), patch("ssa.jev.requests.post", side_effect=soft_http) as post:
            forecast = harness.forecast("jev-zeroshot-persona", self.round, self.history)
            self.assertEqual(post.call_count, 24)
            self.assertAlmostEqual(forecast["topline"]["mean"], 60)  # argmax would give 100
            self.assertEqual(forecast["topline"]["sd"], personas.sd_for(personas.weights_for("A"), self.history))
            self.assertIn("response=probability-expectation-v2", forecast["notes"])
            again = harness.forecast("jev-zeroshot-persona", self.round, self.history)
            self.assertEqual(forecast["topline"], again["topline"])
            self.assertEqual(post.call_count, 24)

    def test_only_persona_adapter_identity_changes(self):
        direct = harness.call_identity("jev")
        soft = harness.call_identity("jev-zeroshot-persona")
        with patch.dict(harness.MODELS["jev"], {"persona_params": {}}):
            self.assertEqual(direct, harness.call_identity("jev"))
            self.assertEqual(direct, harness.call_identity("jev-zeroshot-persona"))
        self.assertNotEqual(direct, soft)

    def test_offline_reconversion_checks_original_request_and_prompt(self):
        spec = series.survey("yougov_approval")
        prompt = harness.build_persona_prompt(personas.panel()[0], spec)
        req, _ = jev.request_for(prompt, "jev-1.13.0")
        response = response_for(req)
        source = {"entrant": "jev-zeroshot-persona", "round_id": "test-round", "persona": "p000",
                  "model": "jev-1.13.0", "input_hash": "old-hash",
                  "prompt_sha256": harness.replies.prompt_sha256(prompt),
                  "reply": '{"approval":"approve"}',
                  "usage": {"jev": {"request": req, "response": response}}}
        args = ("jev-zeroshot-persona", "test-round", "p000", prompt, "new-hash")
        converted = offline.convert_record(source, *args)
        self.assertIsInstance(json.loads(converted["reply"])["approval"], dict)
        self.assertEqual(converted["derived_from"]["input_hash"], "old-hash")
        self.assertEqual(source["input_hash"], "old-hash")
        self.assertEqual(source["reply"], '{"approval":"approve"}')
        with self.assertRaises(ValueError):
            offline.convert_record(dict(source, prompt_sha256="wrong"), *args)
        source["usage"]["jev"]["request"] = dict(req, state="a different persona")
        with self.assertRaises(ValueError):
            offline.convert_record(source, *args)

    def test_unsupported_methods_and_shapes_fail_before_http(self):
        with self.assertRaises(KeyError):
            harness.resolve("jev-superfc")
        self.assertEqual(harness.cell_entrants([("recent10", "superfc")], ["jev"]), [])
        with self.assertRaises(ValueError):
            harness.entrant_id("jev", elicitation="superfc")
        with self.assertRaises(ValueError):
            jev.request_for("Rank these pages", "jev-1.13.0")
        with self.assertRaises(ValueError):
            jev.request_for(harness.build_prompt(dict(self.round, unit="pageviews"), []), "jev-1.13.0")

    def test_live_direct_harness_and_auditable_replay(self):
        with patch("ssa.jev.requests.post", side_effect=self.http) as post:
            forecast = harness.forecast("jev", self.round, self.history)
            self.assertEqual(post.call_count, 1)
            replay = harness.forecast("jev", self.round, self.history)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(forecast["topline"], replay["topline"])
        logs = list((Path(self.tmp.name) / self.round["round_id"]).glob("*.json"))
        record = json.loads(logs[0].read_text())
        self.assertEqual(len(record["usage"]["jev"]["response"]["answers"]["forecast"]["probabilities"]), 200)

    def test_live_persona_keeps_weights_aggregation_sd_and_replay(self):
        with patch.object(personas, "REPLICATES", 1), patch("ssa.jev.requests.post", side_effect=self.http) as post:
            fc = harness.forecast("jev-zeroshot-persona", self.round, self.history)
            self.assertEqual(post.call_count, 24)
            again = harness.forecast("jev-zeroshot-persona", self.round, self.history)
            self.assertEqual(post.call_count, 24)
            expected = {p["id"]: {"approval": "approve" if p["party"] == "Democrat" else "not sure"}
                        for p in personas.panel()}
            weights = personas.weights_for("A")
            self.assertEqual(fc["topline"]["mean"], personas.aggregate("approve_share", expected, weights))
            self.assertEqual(fc["topline"]["sd"], personas.sd_for(weights, self.history))
            self.assertEqual(fc["topline"], again["topline"])

    def test_transport_failure_never_fabricates_forecast(self):
        with patch("ssa.jev.requests.post", return_value=Mock(status_code=401, text="unauthorized")):
            with self.assertRaises(RuntimeError):
                harness.forecast("jev", self.round, self.history)

    def test_jev_is_opt_in_and_cache_changes_with_adapter(self):
        self.assertNotIn("jev", harness.active_models())
        with patch.dict(os.environ, {"SSA_MODELS": "jev"}):
            self.assertEqual(harness.active_models(), ["jev"])
        before = harness.call_identity("jev")
        with patch.dict(harness.MODELS["jev"]["params"], {"adapter": "changed"}):
            self.assertNotEqual(before, harness.call_identity("jev"))

    def test_invalid_body_is_saved_before_validation(self):
        with patch("ssa.jev.requests.post", return_value=Mock(status_code=200, json=lambda: {})):
            with self.assertRaises(ValueError):
                harness.call_provider("jev", harness.build_prompt(self.round, self.history))
        audit = list((Path(self.tmp.name) / "audit").glob("*.json"))
        self.assertEqual(len(audit), 1)
        self.assertNotIn("offline-test-key", audit[0].read_text())

    def test_runner_requires_matching_successful_rounds(self):
        records = [{"entrant": "jev", "round_id": "a", "outcome": 40,
                    "forecast": {"topline": {"mean": 40, "sd": 1}},
                    "persistence": {"mean": 42, "sd": 2}},
                   {"entrant": "jev-zeroshot-persona", "round_id": "a", "forecast": None}]
        self.assertIsNone(runner.summarize(records)["jev"]["mean_crps"])

    def test_plan_uses_answer_freeze_and_excludes_target(self):
        root = Path(self.tmp.name)
        rid = self.round["round_id"]
        fixtures = {
            "questions/season0.json": {"rounds": [self.round]},
            "resolutions/resolved.json": {rid: {"value": 999999,
                                                "observed_date": "2026-08-01"}},
            f"locks/{rid}.json": {"answer_history": self.history,
                                   "history": self.history + [{"date": "2026-08-01", "value": 999999}]},
        }
        for name, value in fixtures.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        tasks, _ = runner.plan(root, ["yougov_approval"], 1)
        self.assertEqual(tasks[0]["history"], self.history)
        prompt = harness.build_prompt(tasks[0]["round"], tasks[0]["history"])
        request, _ = jev.request_for(prompt, "jev-1.13.0")
        self.assertNotIn("999999", json.dumps(request))
        frozen = fixtures[f"locks/{rid}.json"]
        frozen["answer_history"] = frozen["history"]
        (root / f"locks/{rid}.json").write_text(json.dumps(frozen))
        tasks, skipped = runner.plan(root, ["yougov_approval"], 1)
        self.assertEqual(tasks, [])
        self.assertEqual(len(skipped), 1)


if __name__ == "__main__":
    unittest.main()
