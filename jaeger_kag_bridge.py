#!/usr/bin/env python3
"""Automated bridge: Jaeger error traces -> OpenSPG KAG reasoning pipeline.

Fetches recent error traces from Jaeger, extracts their structural context
(trace id, failing service, error logs, span parent-child relationships),
formats them into a text context block, seeds a KAG solver with the known
application topology, injects the trace context, and runs a natural-language
root-cause query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("jaeger_kag_bridge")

# --- Configuration ---------------------------------------------------------
JAEGER_API = "http://localhost:16686/api/traces"
REQUEST_TIMEOUT = 15  # seconds
TARGET_SERVICES = ("frontend", "checkoutservice")
LOOKBACK = "1h"
TRACE_LIMIT = 20

# Predefined application topology injected into KAG as ground-truth graph.
APP_TOPOLOGY = [
    ("Frontend", "CheckoutService"),
    ("CheckoutService", "PaymentService"),
]


# --- Data models -----------------------------------------------------------
@dataclass
class SpanInfo:
    span_id: str
    operation: str
    service: str
    parent_id: Optional[str] = None
    error: bool = False
    error_messages: List[str] = field(default_factory=list)


@dataclass
class TraceInfo:
    trace_id: str
    failing_services: List[str] = field(default_factory=list)
    spans: List[SpanInfo] = field(default_factory=list)
    error_logs: List[str] = field(default_factory=list)


class JaegerError(Exception):
    """Raised when Jaeger telemetry cannot be fetched or parsed."""


# --- Step 1: Query Jaeger --------------------------------------------------
def fetch_error_traces(
    service: str,
    endpoint: str = JAEGER_API,
    timeout: int = REQUEST_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Query Jaeger for recent traces of `service` tagged as errors / HTTP 500.

    Returns the raw list of trace objects from the Jaeger JSON payload.
    """
    params = {
        "service": service,
        "lookback": LOOKBACK,
        "limit": TRACE_LIMIT,
        # Jaeger tag filter: match spans flagged as errors OR HTTP 500.
        "tags": '{"error":"true","http.status_code":"500"}',
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise JaegerError(f"Timed out querying Jaeger for '{service}'") from exc
    except requests.exceptions.ConnectionError as exc:
        raise JaegerError(
            f"Cannot connect to Jaeger at {endpoint} for '{service}'"
        ) from exc
    except requests.exceptions.HTTPError as exc:
        raise JaegerError(
            f"Jaeger returned HTTP error for '{service}': {exc}"
        ) from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise JaegerError("Jaeger response was not valid JSON") from exc

    return payload.get("data", []) or []


# --- Step 2: Parse traces --------------------------------------------------
def _process_to_service(process: Dict[str, Any]) -> str:
    """Resolve a Jaeger process object to its service name."""
    return process.get("serviceName", "unknown") if process else "unknown"


def _span_is_error(span: Dict[str, Any]) -> bool:
    """Detect an error span via the `error=true` or `http.status_code=500` tag."""
    for tag in span.get("tags", []):
        key, value = tag.get("key"), tag.get("value")
        if key == "error" and str(value).lower() == "true":
            return True
        if key in ("http.status_code", "otel.status_code") and str(value) in (
            "500",
            "ERROR",
        ):
            return True
    return False


def _extract_error_logs(span: Dict[str, Any]) -> List[str]:
    """Pull human-readable error messages out of a span's structured logs."""
    messages: List[str] = []
    for log in span.get("logs", []):
        for fld in log.get("fields", []):
            if fld.get("key") in ("event", "message", "error.message", "exception.message"):
                value = fld.get("value")
                if value:
                    messages.append(str(value))
    return messages


def parse_trace(trace: Dict[str, Any]) -> Optional[TraceInfo]:
    """Convert one raw Jaeger trace into a structured `TraceInfo`.

    Returns None if the trace lacks the minimum keys needed for analysis.
    """
    trace_id = trace.get("traceID")
    if not trace_id:
        logger.warning("Skipping trace with no traceID")
        return None

    processes = trace.get("processes", {}) or {}
    info = TraceInfo(trace_id=trace_id)

    for raw_span in trace.get("spans", []):
        # Guard against missing telemetry keys.
        span_id = raw_span.get("spanID")
        if not span_id:
            continue

        service = _process_to_service(processes.get(raw_span.get("processID", "")))
        parent_id = None
        for ref in raw_span.get("references", []):
            if ref.get("refType") == "CHILD_OF":
                parent_id = ref.get("spanID")
                break

        is_error = _span_is_error(raw_span)
        error_messages = _extract_error_logs(raw_span) if is_error else []

        info.spans.append(
            SpanInfo(
                span_id=span_id,
                operation=raw_span.get("operationName", "unknown"),
                service=service,
                parent_id=parent_id,
                error=is_error,
                error_messages=error_messages,
            )
        )

        if is_error:
            if service not in info.failing_services:
                info.failing_services.append(service)
            info.error_logs.extend(error_messages)

    return info


def collect_error_traces() -> List[TraceInfo]:
    """Fetch and parse error traces across all target services."""
    collected: Dict[str, TraceInfo] = {}
    for service in TARGET_SERVICES:
        try:
            raw_traces = fetch_error_traces(service)
        except JaegerError as exc:
            logger.error("Skipping service '%s': %s", service, exc)
            continue

        logger.info("Fetched %d trace(s) for '%s'", len(raw_traces), service)
        for raw in raw_traces:
            parsed = parse_trace(raw)
            if parsed and parsed.error_logs:
                collected[parsed.trace_id] = parsed  # de-dupe by trace id

    return list(collected.values())


# --- Step 3: Format context block -----------------------------------------
def format_context_block(traces: List[TraceInfo]) -> str:
    """Render parsed traces into a clean text context block for KAG."""
    if not traces:
        return "No error traces found in the queried window."

    lines: List[str] = ["=== Jaeger Error Trace Context ==="]
    for trace in traces:
        lines.append(f"\nTrace ID: {trace.trace_id}")
        lines.append(f"Failing service(s): {', '.join(trace.failing_services) or 'n/a'}")

        # Span relationships (parent -> child by service/operation).
        span_by_id = {s.span_id: s for s in trace.spans}
        lines.append("Span relationships:")
        for span in trace.spans:
            parent = span_by_id.get(span.parent_id) if span.parent_id else None
            parent_desc = (
                f"{parent.service}:{parent.operation}" if parent else "ROOT"
            )
            marker = " [ERROR]" if span.error else ""
            lines.append(
                f"  {parent_desc} -> {span.service}:{span.operation}{marker}"
            )

        if trace.error_logs:
            lines.append("Error logs:")
            for msg in trace.error_logs:
                lines.append(f"  - {msg}")

    return "\n".join(lines)


# =============================================================================
# REFERENCE ONLY — Groq LLM configuration for KAG (not executed)
# -----------------------------------------------------------------------------
# Groq exposes an OpenAI-compatible API, so KAG's OpenAI client works by simply
# pointing the base_url at Groq and using a Groq model name. KAG reads these
# values from its `kag_config.yaml` (chat_llm / vectorize_model sections).
#
# 1) Set your key as an environment variable (never hard-code it):
#        setx GROQ_API_KEY "gsk_your_key_here"     # Windows (new shell after)
#        export GROQ_API_KEY="gsk_your_key_here"   # Linux / macOS
#
# 2) In kag_config.yaml, configure the chat model to use Groq:
#        chat_llm:
#          type: maas
#          base_url: https://api.groq.com/openai/v1
#          api_key: ${GROQ_API_KEY}
#          model: llama-3.3-70b-versatile   # or another Groq-hosted model
#
#    NOTE: Groq only serves chat/completion models — it does NOT provide an
#    embeddings endpoint. Configure `vectorize_model` with a separate embedding
#    provider (e.g. local sentence-transformers or OpenAI embeddings):
#        vectorize_model:
#          type: openai
#          base_url: https://api.openai.com/v1
#          api_key: ${OPENAI_API_KEY}
#          model: text-embedding-3-small
#          vector_dimensions: 1536
#
# 3) Equivalent direct call (if you bypass KAG and hit Groq yourself):
#        from openai import OpenAI
#        client = OpenAI(
#            base_url="https://api.groq.com/openai/v1",
#            api_key=os.environ["GROQ_API_KEY"],
#        )
#        resp = client.chat.completions.create(
#            model="llama-3.3-70b-versatile",
#            messages=[{"role": "user", "content": grounded_prompt}],
#        )
#        answer = resp.choices[0].message.content
# =============================================================================


# --- Step 4: Initialize KAG and inject context -----------------------------
def build_kag_solver(topology: List[tuple], trace_context: str):
    """Initialize an OpenSPG KAG solver, load the topology graph, inject context.

    The KAG SDK surface differs across releases, so imports are done lazily and
    guarded. Returns the initialized solver, or None if the SDK is unavailable.
    """
    try:
        # Imported lazily so the fetch/parse pipeline works without the SDK.
        from kag.solver.logic.solver_pipeline import SolverPipeline  # type: ignore
        from kag.common.conf import KAG_CONFIG  # type: ignore
    except ImportError as exc:
        logger.error(
            "OpenSPG KAG SDK not available (%s). "
            "Install it with `pip install openspg-kag`.",
            exc,
        )
        return None

    try:
        solver = SolverPipeline.from_config(KAG_CONFIG.all_config["solver_pipeline"])
    except Exception as exc:  # SDK init surfaces many error types
        logger.error("Failed to initialize KAG solver: %s", exc)
        return None

    # Load the predefined application topology as ground-truth relationships.
    topology_text = "\n".join(f"{src} -> {dst}" for src, dst in topology)
    logger.info("Loaded application topology:\n%s", topology_text)

    # Attach the live Jaeger trace context so the solver reasons over real data.
    setattr(solver, "runtime_context", trace_context)
    setattr(solver, "topology_graph", topology)

    return solver


# --- Step 5: Sample query --------------------------------------------------
def query_kag(solver, question: str, trace_context: str, topology: List[tuple]) -> str:
    """Execute a natural-language query against KAG with the injected context."""
    grounded_prompt = (
        f"{question}\n\n"
        f"Application topology:\n"
        + "\n".join(f"{s} -> {d}" for s, d in topology)
        + f"\n\n{trace_context}"
    )

    if solver is None:
        # Graceful degradation: return the prompt that WOULD be sent to KAG.
        logger.warning("KAG solver unavailable; returning grounded prompt only.")
        return grounded_prompt

    try:
        # `solve` is the common KAG entry point; adjust to your SDK version.
        answer = solver.solve(grounded_prompt)
    except AttributeError:
        try:
            answer = solver.run(grounded_prompt)
        except Exception as exc:
            logger.error("KAG query failed: %s", exc)
            return grounded_prompt
    except Exception as exc:
        logger.error("KAG query failed: %s", exc)
        return grounded_prompt

    return str(answer)


# --- Orchestration ---------------------------------------------------------
def main() -> int:
    logger.info("Starting Jaeger -> KAG bridge")

    traces = collect_error_traces()
    if not traces:
        logger.warning("No error traces collected; nothing to analyze.")
        return 0

    context_block = format_context_block(traces)
    logger.info("Built trace context block (%d chars)", len(context_block))
    print(context_block)

    solver = build_kag_solver(APP_TOPOLOGY, context_block)

    question = (
        "Analyze the root cause of the current checkout failures "
        "using the attached trace data."
    )
    answer = query_kag(solver, question, context_block, APP_TOPOLOGY)

    print("\n=== KAG Root-Cause Analysis ===")
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
