"""laya_route + llm_route — routing a subtask to the best worker agent.

laya path: non-autoregressive System-1 decision model (33-460 ms typed choice
in a single forward pass) with confidence gating and transparent downgrade to
the LLM fallback. Routing must never hard-fail a run.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from .llm import JSON_LINE_TOKENS, chat, route_spec

ESCALATE_THRESHOLD = 0.55

_ROUTER: Any = None
_ROUTER_LOCK = threading.Lock()


def _ensure_hub_trust() -> None:
    """Trust the system CA bundle for the HF download.

    This host sits behind a corporate MITM proxy whose CA (LGE.com) is in
    /etc/ssl/certs/ca-certificates.crt but not in certifi's bundle, so
    any HTTP library using certifi fails with CERTIFICATE_VERIFY_FAILED.
    huggingface_hub honours SSL_CERT_FILE, which we bake in before the
    first Router build.
    """
    import ssl
    import sysconfig
    cafile = os.environ.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    if os.path.exists(cafile):
        os.environ.setdefault("SSL_CERT_FILE", cafile)


def _router() -> Any:
    """Lazily construct the laya Router (first call pays the model download)."""
    global _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None:
            _ensure_hub_trust()
            from laya import Router

            _ROUTER = Router(preload=True)
        return _ROUTER


def _roster_text(roster: list[dict]) -> str:
    cards = []
    for r in roster:
        card = f"{r['id']}: {r.get('description', '')}"
        strengths = r.get("strengths") or []
        if strengths:
            card += f" | Strengths: {', '.join(strengths)}"
        cards.append(card[:380])
    return "\n".join(cards)


def _laya_route(title: str, brief: str, domain: str, roster: list[dict], escalate_below: float) -> dict:
    """Laya decision-model routing. Returns {agent, confidence, engine}."""
    questions = {
        "worker": {
            "type": "choice",
            "instructions": "Which worker agent should handle this task?",
            "criteria": {
                r["id"]: (r.get("description", "") + " Strengths: " + ", ".join(r.get("strengths") or []))[:380]
                for r in roster
            },
        }
    }
    state = {"task": (title + ". " + brief)[:1800], "domain": domain}
    ans = _router().predict(state, questions)["answers"]["worker"]
    return {
        "agent": ans["choice"],
        "confidence": ans["confidence"],
        "engine": "laya",
        "escalate": ans["confidence"] < escalate_below,
        "escalate_below": escalate_below,
    }


def llm_route(title: str, brief: str, domain: str, roster: list[dict]) -> dict:
    """One-turn LLM routing (fallback engine). Same confidence contract."""
    roster_text = _roster_text(roster)
    prompt = f"""Pick the roster id best fitting this task.
Task: {title}. {brief}
Domain: {domain}
Roster:
{roster_text}
Respond ONLY with JSON: {{"agent": <id>, "reason": <one short sentence>}}"""
    text = chat(*route_spec(), messages=[{"role": "user", "content": prompt}], max_tokens=JSON_LINE_TOKENS)
    data = json.loads(text.split("```")[1].split("\n")[1]) if "```" in text else json.loads(text)
    return {
        "agent": data["agent"],
        "confidence": 1.0,
        "engine": "llm",
        "escalate": False,
        "reason": data.get("reason", ""),
    }


def route(
    title: str,
    brief: str,
    domain: str,
    roster: list[dict],
    escalate_below: float = ESCALATE_THRESHOLD,
) -> dict:
    """Route via laya, transparently downgrading to LLM on any exception.

    `escalate_below` comes from the orchestration ladder when one is installed;
    the module constant is only the fallback.
    """
    try:
        return _laya_route(title, brief, domain, roster, escalate_below)
    except Exception as e:
        try:
            result = llm_route(title, brief, domain, roster)
            result["laya_error"] = str(e)[:300]
            return result
        except Exception as e2:
            return {"ok": False, "error": f"routing failed: {str(e2)[:200]}"}

