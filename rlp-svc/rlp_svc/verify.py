"""rlp verify — an independent, cross-vendor pass/fail on a node's acceptance.

A worker grading its own homework is the weakest link in a fan-out: it says
`ACCEPTANCE: pass` and nothing checks it. This runs a *different* model — by
preference a different vendor family than the arm that did the work — N times
against the node's acceptance sentence and its evidence, and returns the
majority verdict. Best-of-N with a cross-vendor judge is the cheapest way to
turn "the worker said it passed" into "an independent model agreed, or did not".

The verifier model resolves like the planner/critic do: `RLP_VERIFY_MODEL`,
then the ladder's `verify` role (binding or arm), then a `review` arm on a
different family, then the fast route arm.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import orchestration as orch
from .llm import _split_spec, chat, route_spec

VERIFY_PROMPT = """Return raw JSON only — the first character of your reply must be {{. No prose, no code fences.
You are an independent verifier. Decide whether the work below meets its acceptance contract.
Be strict: if the acceptance is not demonstrably met by the evidence, it fails. Do not assume.
Node: {title}
Acceptance (the pass/fail contract): {acceptance}
Worker's own report (may be self-serving; do not trust it over the evidence):
{report}
Evidence (result text, files, commands):
{evidence}
Reply with exactly {{"pass": true or false, "reason": "<one short line>", "evidence": "<what you actually checked>"}}."""


def _family(ref: str) -> str:
    return ref.partition("/")[0]


def pick_verifier(config: dict, avoid_family: str = "") -> str | None:
    """A verifier model, preferring a vendor family other than the implementer's."""
    model = orch.resolve_model_for_role(config, "verify")
    if model:
        return model
    for worker in orch.workers(config):
        for arm in worker["models"]:
            if "review" in arm["roles"] and (not avoid_family or _family(arm["model"]) != avoid_family):
                return arm["model"]
    for worker in orch.workers(config):
        for arm in worker["models"]:
            if not avoid_family or _family(arm["model"]) != avoid_family:
                return arm["model"]
    for worker in orch.workers(config):
        for arm in worker["models"]:
            return arm["model"]
    return None


def _spec(avoid_family: str) -> tuple[str, str, str]:
    """(provider, model, family). `RLP_VERIFY_MODEL` beats the ladder."""
    raw = os.environ.get("RLP_VERIFY_MODEL")
    if raw:
        return (*_split_spec(raw), _family(raw))
    try:
        config = orch.load()
    except Exception:
        config = None
    if config:
        model = pick_verifier(config, avoid_family)
        if model:
            return (*_split_spec(model), _family(model))
    provider, model = route_spec()
    return provider, model, provider


def _one(provider: str, model: str, prompt: str) -> dict:
    text = chat(provider, model, messages=[{"role": "user", "content": prompt}], max_tokens=500, temperature=0.7)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON in verifier reply")
    data = json.loads(text[start : text.rindex("}") + 1])
    return {"pass": bool(data.get("pass")), "reason": str(data.get("reason", ""))[:200],
            "evidence": str(data.get("evidence", ""))[:200]}


def verify(
    title: str,
    acceptance: str,
    report: str = "",
    evidence: str = "",
    avoid_family: str = "",
    samples: int = 3,
) -> dict:
    """Best-of-N majority verdict from a cross-vendor verifier. Envelope shape."""
    if not acceptance.strip():
        return {"ok": False, "error": "verify needs the node's acceptance sentence"}
    samples = max(1, min(7, int(samples or 3)))
    provider, model, family = _spec(avoid_family)
    prompt = VERIFY_PROMPT.format(
        title=title[:300],
        acceptance=acceptance[:600],
        report=(report or "(none)")[:4000],
        evidence=(evidence or "(none)")[:12000],
    )
    votes: list[dict] = []
    errors: list[str] = []
    for _ in range(samples):
        try:
            votes.append(_one(provider, model, prompt))
        except Exception as e:
            errors.append(str(e)[:160])
    if not votes:
        return {"ok": False, "error": f"verifier produced no verdict: {errors[-1] if errors else 'unknown error'}"}
    passes = sum(1 for v in votes if v["pass"])
    passed = passes * 2 > len(votes)  # strict majority of the votes that parsed
    return {
        "ok": True,
        "result": {
            "pass": passed,
            "verifier": f"{provider}/{model}",
            "family": family,
            "cross_vendor": bool(avoid_family) and family != avoid_family,
            "votes": votes,
            "pass_count": passes,
            "samples": len(votes),
            "agreement": round(passes / len(votes), 2),
            "reason": next((v["reason"] for v in votes if v["pass"] == passed), votes[0]["reason"]),
            "errors": errors,
        },
    }