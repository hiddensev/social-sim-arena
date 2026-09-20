# Jev prospective submissions

Entrants: **jev-direct** and **jev-persona**, owned by `hiddensev`.
Both use TypeSafe official `jev-1.13.0` and the closed information condition.
[Adapter details and historical limitations](jev-local.md).

## Fixed experiment

`config/jev-live.json` maps the public IDs to the existing harness conditions:
Direct = `jev` (recent 10 observations); Persona = `jev-zeroshot-persona`
(the original 24 × 8 panel). Scope is YouGov approval, Civiqs net approval,
and University of Michigan sentiment. No Superforecasting, profiles, rankings,
news, or web arm. Only the provider and required output conversion change.
The live runner additionally refuses incomplete Persona panels.

The runner reads official questions, registrations, resolutions and answer
freezes at an immutable upstream main commit. It files during the official
24-hour answer window, stopping 30 minutes before lock. No fallback to freshly
fetched or unfrozen history. A missing registration/key prevents inference.
This pins our harness version; other entrants may use other harness versions,
so public timing alone does not prove identical prompts across all entrants.
Compare common prospective rounds and matching information conditions.

## Automated operation

`.github/workflows/jev-live.yml` runs every two hours at minute 37 and supports
manual dispatch. GitHub may delay scheduled runs. Only this workflow is enabled
in the fork; inherited arena-operator workflows are disabled. The workflow
runs the adapter and submission tests before attempting inference.

The repository Secrets are `TYPESAFE_API_KEY`, `SSA_JEV_SIGNING_KEY_B64`
(raw 32-byte Ed25519 private key, base64) and `SSA_JEV_CACHE_AGE_KEY`
(an independent age X25519 identity). Registration JSON contains only the
Ed25519 public key. Local backup keys are ignored under `.local/jev/` with
owner-only file permissions. Never commit them or upload them as artifacts.

Responses, predictions, signed requests and receipts stay private. An age
ENCRYPTED archive is saved in Actions cache between runs. Logs contain only
round IDs/statuses. Successful replies resume individually; accepted forecasts
are skipped using either the private receipt or the official sealed/revealed
ledger. Transport retries preserve exact signed bytes. If authentication
expires without a visible ledger entry, the same forecast is re-signed;
the model is not re-run to select a new answer. If cache is evicted before a
forecast reaches the ledger, unfinished inference may need to be purchased again.

A new round costs 193 model requests before retries (1 Direct + 192 Persona).
There is no monetary cap in this initial deployment. At most four eligible
rounds are processed in a run; failed/incomplete panels resume next run.
A failed Actions run is visible through normal GitHub workflow notifications.

## Local commands

```sh
.venv/bin/python -m unittest tests.test_jev tests.test_jev_live -q
.venv/bin/python tools/run_jev_live.py  # public metadata only, no inference
.venv/bin/python tools/run_jev_live.py --execute --key .local/jev/signing.key
```

Registration must merge into the official repository before either entrant
can submit. Registration alone does not produce a score: an accepted forecast
must lock and the corresponding outcome must be resolved first. The historical
backtest is not uploaded as prospective competition evidence.

## Validation record

The 24 Jev adapter/live tests and the original Persona, condition, route,
reply, cutoff and model-backtest-run checks pass. The expanded signed-intake
suite has one existing documentation assertion failure:
`ClientDefaults.test_the_documented_command_is_the_one_that_works` expects
`ssa-production-v1` in upstream `docs/signed-submissions.md`, where it is absent.
The implicated source, documentation and test are unchanged from upstream
`50047fe558dd8c8b7fefad570c693656f718d936`.
