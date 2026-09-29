"""rlp_svc — the RLP decision engine.

Five MCP capabilities the orchestrator brain calls (rlp_triage, rlm_decompose,
laya_route, llm_route, rlp_orchestration) plus the same logic as an importable
library and CLI: `plan` (triage -> DAG -> routes -> dispatch waves) and
`doctor` (is this host runnable). See docs/CONCEPTS.md.
"""

__version__ = "0.2.0"
