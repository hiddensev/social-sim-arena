"""TypeSafe adapter for the unchanged scalar Direct and Persona prompts.

Direct: 200 equal-width bins on the survey's theoretical support, interpreted
as a mixture of uniforms, moment-matched to the arena's normal contract.
Persona: integrate the categorical probabilities through the survey aggregate.
The entire typed request/response is retained in the harness's usage log.
"""
import json
import math
import re
from pathlib import Path
import os
import uuid

import requests

BINS = 200


def probability_vector(ps, options, api_quantization=False):
    """Validate all options; only raw provider output permits API quantization."""
    if not isinstance(ps, dict) or set(ps) != set(options):
        raise ValueError("Jev probabilities must cover exactly the requested options")
    if any(isinstance(v, bool) or not isinstance(v, (float, int))
           or not math.isfinite(v) or not 0 <= v <= 1 for v in ps.values()):
        raise ValueError("Jev probabilities must be finite numbers in [0, 1]")
    total = math.fsum(ps.values())
    # Observed official API responses round each probability to hundredths
    # and can total 0.99. Allow at most one percentage point of missing/extra
    # mass, only for that quantized format; never repair arbitrary bad totals.
    quantized = api_quantization and all(abs(v * 100 - round(v * 100)) < 1e-8 for v in ps.values())
    tolerance = (0.0100000001 if quantized else 1e-5) if api_quantization else 1e-9
    if not math.isclose(total, 1.0, abs_tol=tolerance, rel_tol=0):
        raise ValueError(f"Jev probabilities sum to {total:.12g}, expected 1")
    return {k: ps[k] / total for k in options}


def probabilities(answer, options):
    """Validate the complete distribution and bounded API quantization error."""
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("Jev must return a Choice answer")
    normalized = probability_vector(answer.get("probabilities"), options, api_quantization=True)
    ps = answer["probabilities"]
    choice = answer.get("choice")
    if choice not in ps or ps[choice] < max(ps.values()) - 1e-6:
        raise ValueError("Jev choice must be a highest-probability option")
    return normalized


def parse_persona_reply(text, spec):
    """Strictly parse normalized option probabilities; old hard replies fail."""
    obj = json.loads(text)
    if not isinstance(obj, dict) or set(obj) != {i["key"] for i in spec["items"]}:
        raise ValueError("Jev Persona reply must cover exactly the survey items")
    return {i["key"]: probability_vector(obj[i["key"]], i["options"])
            for i in spec["items"]}


def aggregate_persona_probabilities(kind, answers, weights):
    """Expected survey aggregate for the three supported affine instruments.

    Expand each item marginal into fractional weighted responses, then call
    the unmodified official arithmetic. Each row answers only one item, so
    Michigan's five denominators each retain the original respondent weight.
    This computes the mean without assuming independence between items or
    people. These fractional rows MUST NOT be used to compute panel size/sd.
    """
    from . import personas
    if kind not in {"approve_share", "net_approve_share", "umich_ics"}:
        raise ValueError("Jev expectation requires an audited affine aggregate")
    fractional, fractional_weights = {}, {}
    for pid in sorted(answers):
        if pid not in weights:
            continue
        for item, ps in answers[pid].items():
            for option, probability in ps.items():
                if probability == 0:
                    continue
                key = (pid, item, option)
                fractional[key] = {item: option}
                fractional_weights[key] = weights[pid] * probability
    return personas.aggregate(kind, fractional, fractional_weights)


def request_for(prompt, model):
    """Translate only the output instruction; preserve the harness context."""
    from . import harness, personas

    marker = harness.PERSONA_FOOTER.split("{shape}")[0]
    if prompt.startswith(harness.PERSONA_HEADER.split("{persona}")[0]):
        if marker not in prompt:
            raise ValueError("unrecognized Persona output instruction")
        state, shape = prompt.rsplit(marker, 1)
        keys = json.loads(shape)
        questions = {}
        for key, placeholder in keys.items():
            match = re.search(r"^- " + re.escape(key) + r": (.*?)\n  Options:",
                              state, flags=re.M | re.S)
            if not match or not placeholder.startswith("<") or not placeholder.endswith(">"):
                raise ValueError("unrecognized survey instrument")
            options = placeholder[1:-1].split(" / ")
            if len(set(options)) != len(options) or not 1 <= len(options) <= 255:
                raise ValueError("invalid survey options")
            questions[key] = {"type": "choice", "instructions": match.group(1),
                              "criteria": dict.fromkeys(options)}
        return {"model": model, "state": state.rstrip(), "questions": questions}, None

    if not prompt.startswith("You are forecasting the next scheduled release of a public opinion tracker.\n") or not prompt.endswith(harness.FOOTER):
        raise ValueError("Jev v1 supports scalar survey Direct and Persona prompts only")
    state = prompt[:-len(harness.FOOTER)]
    if harness.SUPERFC in state:
        raise ValueError("Jev does not support Superforecasting")
    match = re.search(r"^Unit: (.+)$", state, re.M)
    unit = match.group(1) if match else ""
    if unit in ("% approve", "percent approving", "percent"):
        lo, hi = 0.0, 100.0
    elif unit.startswith("net points"):
        lo, hi = -100.0, 100.0
    elif unit == "index points" and "University of Michigan" in state:
        lo = personas.ICS_OFFSET
        hi = lo + 1000.0 / personas.ICS_BASE
    else:
        raise ValueError(f"Jev v1 has no declared numerical support for {unit!r}")
    edges = [lo + (hi - lo) * i / BINS for i in range(BINS + 1)]
    criteria = {
        f"bin_{i:03d}": f"{edges[i]:.12g} <= released value "
                       f"{'<=' if i == BINS - 1 else '<'} {edges[i + 1]:.12g} {unit}"
        for i in range(BINS)
    }
    question = {
        "type": "choice",
        "instructions": "Which interval contains the value of the scheduled release? "
                        "Express predictive uncertainty over these exhaustive, "
                        "mutually exclusive intervals in the stated unit.",
        "criteria": criteria,
    }
    return {"model": model, "state": state.rstrip(),
            "questions": {"forecast": question}}, edges


def convert(data, request, edges):
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(request["questions"]):
        raise ValueError("Jev response does not match requested questions")
    validated = {k: probabilities(answers[k], q["criteria"])
                 for k, q in request["questions"].items()}
    if edges is None:
        return validated
    ps = validated["forecast"]
    centres = [(a + b) / 2 for a, b in zip(edges, edges[1:])]
    mean = math.fsum(ps[f"bin_{i:03d}"] * x for i, x in enumerate(centres))
    variance = math.fsum(
        ps[f"bin_{i:03d}"] * ((x - mean) ** 2 + (edges[i + 1] - edges[i]) ** 2 / 12)
        for i, x in enumerate(centres))
    return {"mean": mean, "sd": math.sqrt(variance)}


def call(cfg, base, key, model, prompt):
    from . import harness

    request, edges = request_for(prompt, model)
    response = requests.post(
        base + "/systemone", headers={"Authorization": f"Bearer {key}"},
        json=request, timeout=(15, 60), allow_redirects=False)
    if 300 <= response.status_code < 400:
        raise RuntimeError("Jev API redirected; refusing to forward credentials")
    data = harness._check(response, "Jev")
    # Preserve the provider body even if validation below rejects it. A bad
    # shape is evidence worth inspecting, not a reason to lose a paid reply.
    audit_dir = Path(os.environ.get("SSA_JEV_AUDIT_DIR") or
                     Path(__file__).resolve().parents[1] / "cache/jev/provider-responses")
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit = {"endpoint": base + "/systemone", "request": request,
             "response": data, "adapter": cfg["params"]["adapter"]}
    (audit_dir / (uuid.uuid4().hex + ".json")).write_text(json.dumps(audit, indent=2))
    if not isinstance(data, dict) or not isinstance(data.get("model"), str):
        raise ValueError("Jev response is missing its resolved model ID")
    if model not in ("jev-latest", "jev-preview") and data["model"] != model:
        raise ValueError("Jev returned a different model than the pinned model")
    converted = convert(data, request, edges)
    usage = harness._usage(data) or {}
    usage["jev"] = {"adapter": cfg["params"]["adapter"],
                    "request": request, "response": data,
                    "bin_edges": edges,
                    "probability_totals": {k: math.fsum(a["probabilities"].values())
                                           for k, a in data["answers"].items()}}
    return json.dumps(converted, allow_nan=False), usage
