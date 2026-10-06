"""The honest baseline: flat retrieval over telemetry, no graph.

This is what most "AI for observability" demos actually are -- fetch the recent
errors, concatenate them, ask an LLM what went wrong. It is a real technique and
it genuinely solves single-hop failures, so the comparison has to be fair:

  * same Jaeger instance
  * same time window
  * same model, same temperature
  * same question

The ONLY thing withheld is the knowledge graph -- topology, pool capacity,
co-tenancy, and the deployment history. If the baseline still finds the root
cause, the graph was not earning its place and you should say so on stage.
"""

from __future__ import annotations

from typing import List

import graph as graph_mod
import llm

SYSTEM_PROMPT = (
    "You are a site reliability engineer performing root cause analysis. "
    "Analyse the telemetry below and identify the root cause of the incident."
)


def collect_flat_context(lookback: str = "15m", max_lines: int = 60) -> str:
    """Recent error spans, flattened into text. Deliberately no structure."""
    tg = graph_mod.TelemetryGraph()
    graph_mod.load_facts(tg, graph_mod.HERE / "facts.yaml")
    services = tg.nodes_of("Service")

    lines: List[str] = []
    for service in services:
        for trace in graph_mod.fetch_traces(service, lookback):
            processes = trace.get("processes", {}) or {}
            for span in trace.get("spans", []):
                if not graph_mod._span_error(span):
                    continue
                proc = processes.get(span.get("processID", "")) or {}
                owner = proc.get("serviceName", "unknown")
                duration_ms = round(span.get("duration", 0) / 1000.0, 1)
                messages = graph_mod._span_messages(span)
                detail = (" | " + "; ".join(messages[:2])) if messages else ""
                lines.append(
                    "[" + owner + "] " + str(span.get("operationName"))
                    + " duration=" + str(duration_ms) + "ms ERROR" + detail)
                if len(lines) >= max_lines:
                    break

    if not lines:
        return "No error telemetry found in the window."

    # De-duplicate the way a log search UI would, keeping counts.
    counted: dict = {}
    for line in lines:
        counted[line] = counted.get(line, 0) + 1
    return "\n".join(
        (str(count) + "x " if count > 1 else "") + line
        for line, count in sorted(counted.items(), key=lambda kv: -kv[1])
    )


def build_prompt(lookback: str = "15m") -> str:
    return (
        "## Recent error telemetry (last " + lookback + ")\n"
        + collect_flat_context(lookback)
        + "\n\n## Task\n"
        "Identify the root cause of this incident. State the specific component "
        "responsible, the causal chain, and the remediation."
    )


def analyze(lookback: str = "15m"):
    prompt = build_prompt(lookback)
    try:
        return prompt, llm.complete(prompt, system=SYSTEM_PROMPT), None
    except (llm.NoLLMConfigured, llm.LLMError) as e:
        return prompt, None, str(e)


if __name__ == "__main__":
    print(build_prompt())
