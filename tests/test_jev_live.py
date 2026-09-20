"""Live submission gates, signature/idempotency, and private cache safety."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import pyrage

from ssa import signed_forecasts as protocol
from tools import jev_live_cache as cache, run_jev_live as live


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 9, 22, 18, tzinfo=timezone.utc)
        self.r = {"round_id": "yougov-future-test", "series": "yougov_approval",
                  "lock_at": "2026-09-23T14:00:00Z", "release_at": "2026-09-25T14:00:00Z",
                  "target_type": "continuous_normal"}
        self.history = [{"date": "2026-09-01", "value": 36}, {"date": "2026-09-08", "value": 35}]
        self.freeze = {"round_id": self.r["round_id"], "series": self.r["series"],
                       "lock_at": self.r["lock_at"], "answer_frozen_at": "2026-09-22T12:00:00Z",
                       "answer_history": self.history}
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption())
        self.key = self.root / "sign.key"
        self.key.write_bytes(raw)
        self.public = base64.b64encode(self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.reg = {"entrant_id": "jev-direct", "github": "hiddensev",
                    "keys": [{"id": "k1", "alg": "ed25519", "public": self.public}]}
        self.cfg = {"owner": "hiddensev", "key_id": "k1", "model": "jev-1.13.0",
                    "series": ["yougov_approval"], "entrants": {"jev-direct": "jev"},
                    "max_rounds_per_run": 4}
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(self.cfg))
        self.args = argparse.Namespace(config=self.config, state=self.root / "state", key=str(self.key), execute=True)
        self.files = {"questions/season0.json": {"rounds": [self.r]},
                      "resolutions/resolved.json": {}, "entrants/jev-direct.json": self.reg,
                      "locks/yougov-future-test.json": self.freeze}
        self.snapshot = Mock(main="a" * 40, sealed="b" * 40)
        self.snapshot.read.side_effect = lambda name, sealed=False: self.files.get(name)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_only_unresolved_scalars_in_matching_window(self):
        self.assertTrue(live.eligible(self.r, {}, self.cfg["series"], self.now))
        for now in (self.now - timedelta(days=2), protocol.utc(self.r["lock_at"]) - timedelta(minutes=30),
                    protocol.utc(self.r["lock_at"])):
            self.assertFalse(live.eligible(self.r, {}, self.cfg["series"], now))
        self.assertFalse(live.eligible(self.r, {self.r["round_id"]: {}}, self.cfg["series"], self.now))
        self.assertFalse(live.eligible(dict(self.r, target_type="profile_energy"), {}, self.cfg["series"], self.now))

    def test_freeze_must_be_recorded_before_window_and_match_round(self):
        self.assertEqual(live.frozen_history(self.r, self.freeze), self.history)
        for changed in ({"series": "wrong"}, {"round_id": "wrong"}, {"lock_at": "2026-10-01T00:00:00Z"},
                        {"answer_frozen_at": "2026-09-22T18:00:00Z"}, {"answer_history": []}):
            with self.assertRaises(ValueError):
                live.frozen_history(self.r, dict(self.freeze, **changed))

    def test_registration_must_match_owner_and_active_key(self):
        args = ("jev-direct", "hiddensev", "k1", self.public)
        self.assertTrue(live.registered(self.reg, *args))
        for reg in (None, dict(self.reg, status="revoked"), dict(self.reg, github="somebody"),
                    dict(self.reg, keys=[dict(self.reg["keys"][0], revoked=True)])):
            self.assertFalse(live.registered(reg, *args))

    def test_signed_request_verifies_and_tampering_fails(self):
        answer = {"entrant": "jev-direct", "round_id": self.r["round_id"], "topline": {"mean": 36, "sd": 2}}
        request = live.signed_request(answer, "jev-direct", "k1", self.private, self.now)
        raw = base64.b64decode(request["body"])
        protocol.verify(request["meta"], raw, self.reg, protocol.AUDIENCE, self.now)
        with self.assertRaises(protocol.IntakeError):
            protocol.verify(request["meta"], raw + b" ", self.reg, protocol.AUDIENCE, self.now)

    def test_transport_retries_identical_request(self):
        request = live.signed_request({"round_id": self.r["round_id"]}, "jev-direct", "k1", self.private, self.now)
        ok = {"status": "accepted", "commit": "a" * 40}
        with patch("tools.run_jev_live.requests.post", side_effect=[
                Mock(ok=False, status_code=503), Mock(ok=True, json=lambda: ok)]) as post, patch("tools.run_jev_live.time.sleep"):
            self.assertEqual(live.submit(request), ok)
            self.assertEqual(post.call_args_list[0], post.call_args_list[1])

    def test_no_registration_or_official_receipt_never_calls_model(self):
        with patch.object(live, "OfficialSnapshot", return_value=self.snapshot), patch.object(live, "now_utc", return_value=self.now), patch.object(live, "local_forecast") as model:
            self.files["entrants/jev-direct.json"] = None
            self.assertEqual(live.run(self.args), 0)
            self.files["entrants/jev-direct.json"] = self.reg
            self.files["sealed/yougov-future-test/jev-direct.json"] = {"receipt": "exists"}
            self.assertEqual(live.run(self.args), 0)
            model.assert_not_called()

    def test_submission_is_replayed_without_regeneration_or_reposting(self):
        answer = {"entrant": "jev-direct", "round_id": self.r["round_id"], "topline": {"mean": 36, "sd": 2}}
        with patch.object(live, "OfficialSnapshot", return_value=self.snapshot), patch.object(live, "now_utc", return_value=self.now), patch.object(live, "local_forecast", return_value=answer) as model, patch.object(live, "submit", return_value={"status": "accepted", "commit": "f" * 40}) as submit:
            self.assertEqual(live.run(self.args), 0)
            self.assertEqual(live.run(self.args), 0)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(submit.call_count, 1)

    def test_expired_authentication_reuses_same_forecast(self):
        answer = {"entrant": "jev-direct", "round_id": self.r["round_id"], "topline": {"mean": 36, "sd": 2}}
        old = live.signed_request(answer, "jev-direct", "k1", self.private, self.now - timedelta(hours=2))
        request_file = self.args.state / "submissions/yougov-future-test/jev-direct.request.json"
        live.save_private(request_file, old)
        with patch.object(live, "OfficialSnapshot", return_value=self.snapshot), patch.object(live, "now_utc", return_value=self.now), patch.object(live, "local_forecast") as model, patch.object(live, "submit", return_value={"status": "accepted", "commit": "f" * 40}) as submit:
            self.assertEqual(live.run(self.args), 0)
            model.assert_not_called()
            new = submit.call_args.args[0]
            self.assertEqual(new["body"], old["body"])
            self.assertNotEqual(new["meta"]["request-id"], old["meta"]["request-id"])
            protocol.verify(new["meta"], base64.b64decode(new["body"]), self.reg, protocol.AUDIENCE, self.now)

    def test_cache_roundtrip_is_encrypted_with_separate_key(self):
        identity = pyrage.x25519.Identity.generate()
        source = self.root / "source"
        source.mkdir()
        (source / "forecast.json").write_text('private-forecast-evidence')
        archive = self.root / "cache.age"
        cache.pack(source, archive, identity)
        self.assertNotIn(b'private-forecast-evidence', archive.read_bytes())
        dest = self.root / "restored"
        cache.unpack(dest, archive, identity)
        self.assertEqual((dest / "forecast.json").read_text(), 'private-forecast-evidence')

    def test_cache_rejects_path_traversal(self):
        identity = pyrage.x25519.Identity.generate()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("state/../../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        archive = self.root / "unsafe.age"
        archive.write_bytes(pyrage.encrypt(buf.getvalue(), [identity.to_public()]))
        with self.assertRaises(ValueError):
            cache.unpack(self.root / "destination", archive, identity)


if __name__ == "__main__":
    unittest.main()
