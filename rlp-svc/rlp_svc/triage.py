"""laya triage — decide whether a request is simple (run inline like plain pi)
or multi-part (decompose with the RLM engine and orchestrate).

This is the gate that stops RLP from ceremony: the cheap decision ("is this
big?") runs on the non-autoregressive laya model in a single forward pass
before any tokens go to the expensive recursive decomposer. Uncertain calls
default to `direct` — a single agent doing a big task inline is still
correct, just slower, while forcing a one-line fix through the full pipeline
wastes a whole fan-out.

**The hybrid gate.** On the bundled checkpoint every decision lands below
`escalateBelow` (measured confidence 0.003–0.50), so a laya-only gate is
effectively "always direct" and every orchestration depends on the brain
manually overriding it. laya is still the System-1 first pass, but when it is
*unsure* this module now also consults a deterministic fan-out signal analyzer:
if the request text carries an explicit fan-out cue, or names an
implementation plus an independent verification, or lists three distinct
deliverable verbs joined into clauses, the gate escalates itself to
`orchestrate` and says which signal fired. The signals can only ever *raise*
an unsure call; a confident laya `direct` stands, which keeps the safe default
intact. Set `routing.gate: "laya"` in the ladder for the old behaviour.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import orchestration as orch
from .llm import JSON_LINE_TOKENS, chat, route_spec

MODES = ("direct", "orchestrate")

_CRITERIA = {
    "direct": (
        "One focused outcome a single agent finishes inline in a few steps: "
        "fix a known bug, small or one-file change, add a simple function or "
        "flag, quick question about the repo, a doc or comment edit, running "
        "or reading tests. No coordination between separate deliverables."
    ),
    "orchestrate": (
        "Several independent outcomes or a build-plus-verify shape: a feature "
        "with tests plus an independent review, a multi-module or cross-cutting "
        "refactor, research across separate areas, work the request explicitly "
        "asks to split, delegate, parallelize, or land in separate branches."
    ),
}

_QUESTIONS: dict[str, Any] = {
    "mode": {
        "type": "choice",
        "instructions": (
            "Can one agent complete this request directly, or does it need "
            "decomposition into subtasks run by separate agents?"
        ),
        "criteria": _CRITERIA,
    }
}

# --- deterministic fan-out signals ---------------------------------------------

#: Explicit words that mean "this is meant to be split", not merely "this is big".
_FANOUT_CUES = (
    "in parallel",
    "parallelize",
    "separately",
    "independently",
    "delegate",
    "split into",
    "fan out",
    "orchestrate",
    "separate branches",
    "two deliverables",
    "three deliverables",
    "at the same time",
    "each with",
    "as well as",
    "plus a",
)

#: Deliverable-shaped verbs. A request naming several of these is usually
#: several outcomes, not one.
_DELIVERABLE_VERBS = (
    "add",
    "implement",
    "build",
    "create",
    "refactor",
    "migrate",
    "write",
    "document",
    "test",
    "review",
    "audit",
    "research",
    "analyze",
    "benchmark",
    "deploy",
    "update",
    "port",
)

#: Independent verification paired with real implementation: the build+review
#: shape the pipeline exists for.
_IMPLEMENT_RE = re.compile(r"\b(add|implement|build|create|refactor|fix|write|update|port)\b")
_VERIFY_RE = re.compile(r"\b(review|reviews|audit|verify|validates?|independently)\b")
_CLAUSE_RE = re.compile(r"\b(and|then|also|plus|as well as)\b")


def signals(request: str, context: str = "") -> dict:
    """Deterministic fan-out features of a request. No model, no I/O.

    Deliberately conservative: it must never manufacture orchestration out of
    a one-line fix. `strong` is the only field the gate acts on.
    """
    text = f"{request}\n{context}".lower()
    fanout = [cue for cue in _FANOUT_CUES if cue in text]
    verbs = sorted({v for v in _DELIVERABLE_VERBS if re.search(rf"\b{v}\b", text)})
    verify_pair = bool(_IMPLEMENT_RE.search(text)) and bool(_VERIFY_RE.search(text))
    clauses = len(_CLAUSE_RE.findall(text))
    strong = bool(fanout) or verify_pair or (len(verbs) >= 3 and clauses >= 1)
    reasons: list[str] = []
    if fanout:
        reasons.append(f"explicit fan-out cue: {fanout[0]!r}")
    if verify_pair:
        reasons.append("implementation plus independent verification")
    if len(verbs) >= 3 and clauses >= 1:
        reasons.append(f"{len(verbs)} deliverable verb(s) joined into clauses")
    return {
        "fanout_cues": fanout,
        "deliverable_verbs": verbs,
        "verify_pair": verify_pair,
        "clauses": clauses,
        "strong": strong,
        "reasons": reasons,
    }


def _state(request: str, context: str) -> dict:
    return {"request": request[:1800], "context": (context or "")[:600]}


def _gate_config() -> tuple[float, str, float]:
    """(escalateBelow, gate mode, signalThreshold) from the ladder, else defaults.

    A broken or absent ladder must not stop triage: the defaults are the same
    ones the ladder ships with, and a failure here is reported by `doctor`.
    """
    try:
        config = orch.load()
    except Exception:
        return 0.55, "hybrid", 1.0
    if config is None:
        return 0.55, "hybrid", 1.0
    routing = config["routing"]
    threshold = routing.get("escalateBelow")
    threshold = 0.55 if threshold is None else float(threshold)
    gate = routing.get("gate") or "hybrid"
    signal_threshold = routing.get("signalThreshold")
    signal_threshold = 1.0 if signal_threshold is None else float(signal_threshold)
    return threshold, gate, signal_threshold


def llm_triage(request: str, context: str = "") -> dict:
    """One-turn LLM triage (fallback engine). Same contract as laya triage."""
    prompt = f"""Classify this request for an agent orchestrator.
Request: {request[:1800]}
Context: {(context or '')[:600]}
direct = {_CRITERIA['direct']}
orchestrate = {_CRITERIA['orchestrate']}
Respond ONLY with JSON: {{"mode": "direct" or "orchestrate", "reason": <one short sentence>}}"""
    text = chat(*route_spec(), messages=[{"role": "user", "content": prompt}], max_tokens=JSON_LINE_TOKENS)
    data = json.loads(text.split("```")[1].split("\n")[1]) if "```" in text else json.loads(text)
    mode = data.get("mode")
    if mode not in MODES:
        raise ValueError(f"llm_triage returned invalid mode {mode!r}")
    return {"mode": mode, "confidence": 1.0, "engine": "llm", "escalate": False, "reason": data.get("reason", "")}


def _apply_hybrid(decision: dict, request: str, context: str, gate: str, signal_threshold: float) -> dict:
    """Fold the deterministic signals into an unsure laya answer.

    Only fires when the gate is `hybrid` and laya is unsure. A confident laya
    `direct` is never overridden — the cheap, always-correct-enough default
    wins — and a confident `orchestrate` is taken as-is.
    """
    sig = signals(request, context)
    decision["signals"] = sig
    if gate != "hybrid" or not decision.get("escalate"):
        return decision
    score = float(bool(sig["strong"]))
    if score < signal_threshold:
        decision["signal_score"] = score
        return decision
    decision.update(
        {
            "mode": "orchestrate",
            "escalate": False,
            "engine": "laya+signals" if decision.get("engine") == "laya" else f"{decision.get('engine')}+signals",
            "signal_score": score,
            "reason": "laya was unsure; fan-out signals say orchestrate (" + "; ".join(sig["reasons"]) + ")",
        }
    )
    return decision


def triage(request: str, context: str = "") -> dict:
    """Triage via laya, transparently downgrading to the LLM on any failure.

    `escalate: true` means low confidence: the default is `direct` (cheap and
    always correct-enough); a hybrid gate may raise it to `orchestrate` on
    deterministic fan-out signals, and the brain may override to `orchestrate`
    only if it can name two or more independent deliverables.
    """
    threshold, gate, signal_threshold = _gate_config()
    try:
        from .route import _router

        ans = _router().predict(_state(request, context), _QUESTIONS)["answers"]["mode"]
        confidence = float(ans["confidence"])
        decision = {
            "mode": ans["choice"],
            "confidence": confidence,
            "engine": "laya",
            "escalate": confidence < threshold,
            "escalate_below": threshold,
            "gate": gate,
            "default_on_escalate": "direct",
        }
        return _apply_hybrid(decision, request, context, gate, signal_threshold)
    except Exception as e:
        try:
            result = llm_triage(request, context)
            result["laya_error"] = str(e)[:300]
            result["escalate_below"] = threshold
            result["gate"] = gate
            result["default_on_escalate"] = "direct"
            return _apply_hybrid(result, request, context, gate, signal_threshold)
        except Exception as e2:
            return {"ok": False, "error": f"triage failed: {str(e2)[:200]}"}