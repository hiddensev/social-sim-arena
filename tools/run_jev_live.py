"""Prospective signed submissions only; plan by default, no public plaintext.

Reads official JSON at pinned Git commits. Uses the same 24-hour call window
and answer freeze as the live baseline. Missing registration/frozen history
never triggers inference. Replies and exact signed retries remain private.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ssa import envfile
envfile.load(str(ROOT / ".env"))
from ssa import baselines, harness, personas, signed_forecasts as protocol
from tools.submit_signed_forecast import load_private_key
from cryptography.hazmat.primitives import serialization
import requests

REPO = "Social-Atoms/social-sim-arena"
DEFAULT_STATE = ROOT / ".local/jev/live"
MARGIN = timedelta(minutes=30)


def now_utc():
    return datetime.now(timezone.utc)


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
    os.replace(tmp, path)


class OfficialSnapshot:
    def __init__(self):
        self.api = requests.Session()
        self.api.headers["Accept"] = "application/vnd.github+json"
        if os.environ.get("GH_TOKEN"):
            self.api.headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
        self.main = self.head("main")
        self.sealed = self.head("sealed")

    def head(self, branch):
        r = self.api.get(f"https://api.github.com/repos/{REPO}/git/ref/heads/{branch}", timeout=30)
        r.raise_for_status()
        return r.json()["object"]["sha"]

    def read(self, path, sealed=False):
        # Deliberately a separate request, so the GitHub API token never
        # travels to the raw-content host or to the arena/TypeSafe.
        sha = self.sealed if sealed else self.main
        r = requests.get(f"https://raw.githubusercontent.com/{REPO}/{sha}/{path}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def eligible(round_def, resolved, selected, now):
    if (round_def.get("series") not in selected or
            round_def.get("target_type", "continuous_normal") != "continuous_normal" or
            round_def["round_id"] in resolved):
        return False
    due = protocol.utc(round_def["lock_at"])
    return due - timedelta(hours=24) <= now < due - MARGIN


def frozen_history(round_def, snapshot):
    """Only the actual answer freeze, never freshly fetched source data."""
    if not snapshot or snapshot.get("round_id") != round_def["round_id"]:
        raise ValueError("missing official answer freeze")
    if snapshot.get("series") != round_def["series"] or snapshot.get("lock_at") != round_def["lock_at"]:
        raise ValueError("freeze identity mismatch")
    due = protocol.utc(round_def["lock_at"])
    if protocol.utc(snapshot["answer_frozen_at"]) > due - timedelta(hours=24):
        raise ValueError("answer freeze was recorded after the window opened")
    history = snapshot.get("answer_history")
    if not history or any(p["date"] >= due.date().isoformat() for p in history):
        raise ValueError("missing or invalid pre-window history")
    return history


def registered(registration, entrant, owner, key_id, public):
    return bool(registration and registration.get("entrant_id") == entrant
                and registration.get("github", "").lower() == owner.lower()
                and registration.get("status", "active") == "active"
                and any(k.get("id") == key_id and k.get("public") == public
                        and k.get("alg") == "ed25519" and not k.get("revoked")
                        for k in registration.get("keys", [])))


def signed_request(answer, entrant, key_id, private, now):
    body = protocol.canonical(answer)
    meta = {"entrant": entrant, "key-id": key_id,
            "request-id": str(uuid.uuid4()), "timestamp": protocol.stamp(now)}
    meta["signature"] = base64.b64encode(private.sign(
        protocol.signing_bytes(meta, body, protocol.AUDIENCE))).decode()
    return {"url": protocol.ORIGIN, "meta": meta,
            "body": base64.b64encode(body).decode()}


def submit(record):
    """Transport retries preserve the exact signed bytes and request ID."""
    if record["url"] != protocol.ORIGIN:
        raise ValueError("saved request belongs to another arena")
    headers = {"X-SSA-" + k: v for k, v in record["meta"].items()}
    headers["Content-Type"] = "application/json"
    for attempt in range(3):
        try:
            response = requests.post(protocol.ORIGIN + protocol.PATH,
                                     headers=headers, data=base64.b64decode(record["body"]),
                                     timeout=60, allow_redirects=False)
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError("intake transport failed; exact retry retained") from None
        else:
            if response.ok:
                receipt = response.json()
                if receipt.get("status") == "accepted":
                    return receipt
                raise RuntimeError("intake did not accept this forecast")
            if response.status_code < 500 and response.status_code != 429:
                try:
                    code = response.json().get("error", {}).get("code", "rejected")
                except ValueError:
                    code = "invalid_response"
                raise RuntimeError(f"intake HTTP {response.status_code}: {code}")
            if attempt == 2:
                raise RuntimeError(f"intake HTTP {response.status_code}; exact retry retained")
        time.sleep(2 ** attempt)


def local_forecast(method, round_def, history, entrant, source_commit):
    r = dict(round_def)
    r["baselines"] = baselines.all_baselines(history, r["release_at"][:10])
    forecast = harness.forecast(method, r, history=history)
    if method.endswith("persona"):
        match = re.search(r"(\d+)/(\d+) respondents", forecast["notes"])
        if not match or match[1] != match[2]:
            raise RuntimeError("partial Persona panel: retain replies and resume next run")
    forecast["entrant"] = entrant
    # Also retain the snapshot pointer for a future exact-input audit.
    forecast["notes"] = (f"source={source_commit[:12]}, " + forecast["notes"])[:500]
    return forecast


def run(args):
    os.umask(0o077)
    cfg = json.loads(args.config.read_text())
    if harness.model_id("jev") != cfg["model"] or personas.REPLICATES != 8 or harness.ALLOW_MOCK:
        raise ValueError("live model/panel/mock settings differ from the registered experiment")
    if any(m.endswith("persona") for m in cfg["entrants"].values()) and (
            harness.route("jev-zeroshot-persona")["params"]["adapter"] != cfg["persona_adapter"]):
        raise ValueError("live Persona adapter differs from the registered experiment")
    snap = OfficialSnapshot()
    rounds = snap.read("questions/season0.json")["rounds"]
    resolved = snap.read("resolutions/resolved.json")
    if resolved is None:
        raise ValueError("official resolutions unavailable")
    now = now_utc()
    due = sorted((r for r in rounds if eligible(r, resolved, cfg["series"], now)), key=lambda r: r["lock_at"])
    due = due[:cfg["max_rounds_per_run"]]
    print(f"Official snapshot {snap.main[:12]}; {len(due)} eligible round(s).", flush=True)
    registrations = {e: snap.read(f"entrants/{e}.json") for e in cfg["entrants"]}
    for e, registration in registrations.items():
        print(f"{e}: {'registration present' if registration else 'registration pending'}")
    if not args.execute:
        for r in due:
            print(f"plan {r['round_id']} lock={r['lock_at']}")
        return 0
    if not due:
        return 0
    if args.key:
        private = load_private_key(args.key)
    else:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ["SSA_JEV_SIGNING_KEY_B64"], validate=True))
    public = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    state = args.state
    os.environ["SSA_REPLIES_DIR"] = str(state / "replies")
    os.environ["SSA_JEV_AUDIT_DIR"] = str(state / "provider-responses")
    failures = 0
    for r in due:
        rid = r["round_id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", rid):
            raise ValueError("invalid official round ID")
        try:
            history = frozen_history(r, snap.read(f"locks/{rid}.json"))
        except (KeyError, ValueError):
            print(f"{rid}: waiting for valid official answer freeze")
            failures += 1
            continue
        for entrant, method in cfg["entrants"].items():
            if not registered(registrations[entrant], entrant, cfg["owner"], cfg["key_id"], public):
                print(f"{entrant}: pending active registration for this signing key")
                continue
            stem = state / "submissions" / rid / entrant
            receipt_file = stem.with_suffix(".receipt.json")
            if receipt_file.exists():
                print(f"{rid} {entrant}: already accepted (local receipt)")
                continue
            # Official ledger also covers a response lost after persistence or
            # an unavailable private cache. Never replace an already filed answer.
            existing = snap.read(f"sealed/{rid}/{entrant}.json", sealed=True)
            revealed = snap.read(f"forecasts/{rid}/{entrant}.json")
            if existing or revealed:
                print(f"{rid} {entrant}: already filed (official ledger)")
                continue
            if not eligible(r, resolved, cfg["series"], now_utc()):
                continue
            try:
                request_file = stem.with_suffix(".request.json")
                if request_file.exists():
                    request = json.loads(request_file.read_text())
                    if (request["meta"]["entrant"] != entrant or
                            json.loads(base64.b64decode(request["body"]))["round_id"] != rid):
                        raise ValueError("cached signed request identity mismatch")
                    if now_utc() - protocol.utc(request["meta"]["timestamp"]) > timedelta(seconds=240):
                        # No official submission exists at the pinned ledger
                        # head. Refresh authentication for the SAME forecast,
                        # keeping the older signed request as private evidence.
                        previous = request["meta"]["request-id"]
                        save_private(stem.parent / (entrant + ".retries") / (previous + ".json"), request)
                        answer = json.loads(base64.b64decode(request["body"]))
                        request = signed_request(answer, entrant, cfg["key_id"], private, now_utc())
                        save_private(request_file, request)
                else:
                    forecast = local_forecast(method, r, history, entrant, snap.main)
                    schema = json.loads((ROOT / "schema/forecast.schema.json").read_text())
                    protocol.validate_answer(forecast, {"entrant": entrant}, r, schema, now_utc())
                    save_private(stem.with_suffix(".forecast.json"), forecast)
                    request = signed_request(forecast, entrant, cfg["key_id"], private, now_utc())
                    save_private(request_file, request)
                if not eligible(r, resolved, cfg["series"], now_utc()):
                    print(f"{rid} {entrant}: outside safe submission window")
                    continue
                receipt = submit(request)
                save_private(receipt_file, receipt)
                print(f"{rid} {entrant}: accepted, receipt commit {receipt['commit'][:12]}", flush=True)
            except Exception as exc:
                # Do not disclose unrevealed predictions or provider bodies
                # through public Actions output.
                print(f"{rid} {entrant}: failed ({type(exc).__name__}); private state retained", flush=True)
                save_private(stem.with_suffix(".error.json"), {"type": type(exc).__name__, "error": str(exc)})
                failures += 1
    return 1 if failures else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--config", type=Path, default=ROOT / "config/jev-live.json")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--key", help="local private signing key; otherwise use the Actions secret")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
