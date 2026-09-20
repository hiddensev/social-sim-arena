# Jev local experiment

Fork: https://github.com/hiddensev/social-sim-arena

Upstream starting commit: `50047fe558dd8c8b7fefad570c693656f718d936`.

This branch adds TypeSafe's official `POST /v1/systemone` adapter, pinned to
`jev-1.13.0`. It is opt-in. The public entrants are `jev-direct` and `jev-persona`,
owned by `hiddensev`; see [prospective submissions](jev-live.md).

## Setup and replay

Use Python >=3.10 and install `requirements.txt` in a virtual environment. Put
the credential in the ignored `.env`, never in a command argument or commit:

```dotenv
TYPESAFE_API_KEY=your-key
SSA_MODELS=jev
SSA_MODEL_JEV=jev-1.13.0
```

```sh
.venv/bin/python -m unittest tests.test_jev -v
.venv/bin/python tools/run_jev_backtest.py                    # plan only
.venv/bin/python tools/run_jev_backtest.py --execute          # latest eligible round
.venv/bin/python tools/run_jev_backtest.py --execute --series yougov_approval --limit 1 --out cache/jev/yougov-backtest.json
.venv/bin/python tools/run_jev_backtest.py --execute --series civiqs_net_approval --limit 1 --out cache/jev/civiqs-backtest.json
.venv/bin/python tools/run_jev_backtest.py --execute --series umich_sentiment --limit 1 --out cache/jev/umich-backtest.json
.venv/bin/python tools/run_jev_backtest.py --execute --limit 10 --max-calls 2000 --out cache/jev/full-backtest.json
```

`--limit` caps total rounds, newest first, not rounds per series. Select one
series explicitly for balanced coverage. Defaults are the three series above.
Each fresh round requires 193 requests: one Direct and 192 Persona. `--max-calls`
defaults to 1000 and caps the plan before cache savings. It is not a monetary
budget; the provider bills tokens. No real API calls occur without `--execute`.

The runner uses committed questions, resolutions and `answer_history` from
lock files, falling back to `history` for older snapshots. It rejects history
containing a point dated on or after the resolved target. Neither future
observations nor outcomes are passed to the adapter. It calls the existing
`harness.forecast` for **both** conditions and the existing CRPS/skill functions.
Do not use the original `run_model_backtest.py` for this Persona experiment:
that runner calls the scalar provider/parser directly rather than simulating
the panel.

## What changes, and what remains a baseline choice

**Direct** (`jev`, context `recent10`; `jev-zeroshot` also works): the original
question, publisher, methodology, release date and history remain unchanged.
Only the free-text JSON output instruction is replaced with a typed Choice
question over 200 equal-width, non-overlapping intervals. Supports are 0–100
for approval/percent, −100–100 for net opinion, and 2–150.02095977 for the
Michigan ICS (the exact endpoint is `2 + 1000 / 6.7558`). The adapter interprets
the returned probabilities as a mixture of uniforms within those intervals:

```
mean = sum(p[i] * midpoint[i])
variance = sum(p[i] * ((midpoint[i] - mean)^2 + width[i]^2 / 12))
```

The final mean and sd are consumed by the original normal-distribution parser
and scorer. The choice of support, 200 bins and uniform within-bin density is
an explicit modeling approximation, not a native continuous Jev distribution.
Moment matching loses multimodality; bin width contributes to uncertainty even
if all probability is in one bin. The original categorical distribution and
bin edges are preserved for analysis. No outcome-dependent grid or tuning is
used. Units without a declared support are refused, including pageview counts.
Profiles and rankings are not implemented by this first scalar adapter.

**Persona** (`jev-zeroshot-persona`, context `none`): the same panel (24 cells ×
8 replicates), persona descriptions, survey questions and options go to Jev.
Each survey item becomes one Choice; Michigan's five items share one request
per persona. Jev's highest-probability `choice` supplies the single answer the
original harness expects. The original weights, aggregates, minimum response
threshold and sd estimator are unchanged. Full option probabilities are saved
but not used as fractional respondents. A soft-probability aggregate would be
a separate method change, not a drop-in replacement. Persona's final sd is
still a harness estimate, not Jev's uncertainty about the population mean.

No Superforecasting entrant can be resolved for Jev. No generative model is
used as a fallback. Jev is excluded from the default season roster unless
explicitly selected with `SSA_MODELS=jev`.

## Evidence, failures and limitations

Successful typed requests/responses, usage and bin edges are in the existing
reply logs under `cache/jev/replies/`. Raw successful HTTP response bodies are
also archived **before validation** under `cache/jev/provider-responses/`, so
a rejected typed response can be examined. Neither log contains the API key.
Cache identity includes the pinned model, endpoint and adapter version.

Malformed, missing, non-finite, negative or materially unnormalized
probabilities are rejected. Floating-point rounding within `1e-5` of a unit
total is renormalized. For the observed API format where **all** probabilities
are multiples of 0.01, a total within 0.01 of 1 is also normalized; larger
deviations are rejected. Original totals and probabilities remain in the audit
record. Selected options must attain the maximum
probability. Redirects and a response from a different pinned model are refused.

Initial real requests included rejected probability totals. The first such
responses preceded the raw-body audit addition, so their exact deviation is
unknown. Subsequent audit records confirmed 200-option responses with two-decimal
probabilities totaling 0.99. This prompted the bounded normalization rule above,
with a regression test; previously accepted distributions produce the same
converted outputs. Re-running resumed successful replies and retried missing
ones until all ten checked panels had 192/192 responses.
The same model/prompt is not assumed to generate identical fresh inference;
reproducibility means replaying the stored responses. Remaining transient
errors need repeated evaluation before treating the service as production-ready.

The original harness can accept a panel with >=75% of its weight responding.
Inspect the forecast notes for complete coverage; a score from a partial panel
is not equivalent to a complete panel. The local report marks such runs partial.

Jev's training cutoff was not stated in the official model reference checked
for this implementation. Historical scores are labeled `historical-functional-only`
and cannot establish absence of training contamination. Ten historical rounds
are a functional backtest, not a statistically reliable comparison or evidence that this
approach outperforms the baseline. Public arena registration remains pending.

Official references:

- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/primitives/choice
- https://docs.typesafe.ai/models
