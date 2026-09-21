# Jev replay of the 2026-08-09 historical experiment

Scope clarification: this is the complete target set of **one historical run**,
not a fixed or exhaustive evaluation dataset for the whole arena. Our earlier
10-round replay is a subset of our three selected Season 0 series, not a subset
of these 339 targets. Civiqs was registered on 2026-08-12, after this run.
The official competition evaluates forecasts submitted before each future
question locks, against the season manifest and official resolutions.
Historical replays are development diagnostics, not prospective entries.

This experiment covers all **339 distinct (series, date) targets** recorded in
`backtest/runs/2026-08-09.jsonl`. It also reports the **22 targets jointly
answered by the original entrants**. It is separate from our first 10-round
replay of resolved Season 0 questions. All use Jev 1.13.0, Direct recent10,
and the 192-person Persona probability-expectation-v2 implementation.

| Historical series | Targets |
| --- | ---: |
| Morning Consult approval | 63 |
| Michigan sentiment | 14 |
| YouGov approval | 82 |
| YouGov economy approval | 58 |
| YouGov generic ballot margin | 65 |
| YouGov immigration approval | 57 |

Civiqs is not part of this original 339-target run. It remains covered by the
separate resolved-Season-0 check and by our prospective registration.

## Frozen inputs and limits of reproduction

Targets and their values come from the original committed run records. A
repeated generic-ballot target, 2025-10-25, has two recorded values (3 and 5);
the last stored value, 5, is retained, and the conflict is recorded explicitly.
The actual target-date span is 2025-05-31 through 2026-08-01. The old summary's
`window.last` says 2026-07-23; it is not the maximum date in the raw records.
These are the historical series' date labels, not a verified calendar of
publication times.

The complete original input snapshot was not committed. Warm-up history is
reconstructed from the earliest available raw source archives, dated
2026-08-18, using the historical pollster filters (without the newer YouGov
sponsor restriction). Every later history point and scored target uses the
original run value; the target itself and all later values are excluded from
its history. The 13 disagreements with the later vintage are recorded in the
manifest. No live source is fetched by these tools.

This reproduces the original **target set**, not every old prompt byte or an
independently verified point-in-time information set. It uses our frozen current
harness. Jev's training cutoff is unverified, so this is historical functional
and exploratory evidence, not a contamination-free performance claim.

## Run and resume

```sh
.venv/bin/python tools/prepare_jev_retrocast.py --out cache/jev/full-retrocast
.venv/bin/python tools/run_jev_retrocast.py --manifest cache/jev/full-retrocast/manifest.json
.venv/bin/python tools/run_jev_retrocast.py --manifest cache/jev/full-retrocast/manifest.json --execute
```

Preparation refuses to overwrite an existing evidence directory. The second
command only displays the plan. Execution requires the existing ignored `.env`
credential. There are **65,427 requests before retries**: 339 Direct plus
339 × 192 Persona. Provider requests are limited to 1,000/minute and 32 in
flight. The process caps requests at 80,000 and reported input tokens at
119 million. Each incomplete forecast gets up to three passes, reusing
successful per-person replies between passes. No public submission is made.

The approximate initial cost is USD 1.61, using measured earlier request sizes
and the checked Jev input-token rate of USD 0.042/million (output is free).
Actual reported token usage is saved; this estimate is not a billing receipt.

Each round has its own 192-person inference run. Responses are not shared
across different dates even when Persona's original date-free prompt repeats.
The original survey arithmetic handles generic-ballot probabilities using its
existing `party_margin` formula; this affine extension does not change the
three prospective registered series. All panel weights and sd rules remain
unchanged. Complete panels are required for scored results.

Outputs beside the manifest: `progress.json`, `provider-stats.json`, per-round
`forecasts/`, original `replies/`, pre-validation `provider-responses/`, and
`results.json`. A manifest hash binds resumable runs to their original inputs.
A partial result is explicitly marked partial; failed panels are not silently
scored as complete. Keep the evidence directory private and out of Git.
