"""The KAG retrieval + reasoning pipeline.

This is the part that is genuinely different from "paste the logs into an LLM".
Four explicit stages, each inspectable on stage:

  1. SEED      -- start at the observed symptoms, not at the noisiest log
  2. TRAVERSE  -- walk the impact graph backwards to everything that could
                  have caused them, across service / pool / host / config /
                  deployment boundaries
  3. RANK      -- score each candidate, dominated by blast-radius fit:
                  does this candidate explain ALL the symptoms, or just one?
  4. REASON    -- hand the LLM only the retrieved subgraph and require that
                  every claim cite a specific piece of evidence

Stage 3 is the one worth dwelling on. A candidate that explains every symptom
beats a candidate that explains the loudest one, which is precisely the mistake
flat-log RCA makes -- and the mistake a tired human makes at 3am.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

import networkx as nx

from graph import TelemetryGraph, Evidence
import llm

# The real chain is deployment -> config -> pool -> service -> service -> symptom,
# which is five hops. Anything less than that cuts the actual root cause out of
# the candidate set entirely and hands the win to whatever shallow change happens
# to be nearby.
MAX_HOPS = 6

# Scoring weights.
#
# blast_fit and mechanism carry the most weight, and they answer different
# questions. blast_fit asks "could this explain every symptom?" -- pure topology.
# mechanism asks "is the chain actually corroborated at each step?" -- evidence.
# A decoy change can easily score 1.0 on the first; it almost never scores well
# on the second, because nothing along its path looks wrong.
W_BLAST = 0.35
W_MECHANISM = 0.30
W_CHANGE = 0.20
W_ANOMALY = 0.15
W_DEPTH = 0.05

# Recency half-life for changes. A deploy from last week is not a suspect for an
# incident that started ten minutes ago.
CHANGE_HALF_LIFE_MIN = 20.0

SYSTEM_PROMPT = (
    "You are a site reliability engineer performing root cause analysis. "
    "You will be given a retrieved subgraph from an infrastructure knowledge graph, "
    "not raw logs. Reason strictly over what you are given. "
    "Every factual claim must cite an evidence line or a graph edge shown below. "
    "If the evidence does not support naming a root cause, say so plainly instead "
    "of guessing. Never invent service names, config keys or deployment ids."
)


@dataclass
class Candidate:
    node_id: str
    kind: str
    score: float
    blast_fit: float
    mechanism: float
    change_signal: float
    anomaly_signal: float
    depth: int
    explains: Set[str] = field(default_factory=set)
    paths: List[List[str]] = field(default_factory=list)

    def why(self) -> str:
        return (
            "blast=" + format(self.blast_fit, ".2f")
            + " mech=" + format(self.mechanism, ".2f")
            + " change=" + format(self.change_signal, ".2f")
            + " anom=" + format(self.anomaly_signal, ".2f")
            + " depth=" + str(self.depth)
            + " -> score=" + format(self.score, ".3f")
        )


@dataclass
class RCAResult:
    candidates: List[Candidate]
    prompt: str
    answer: Optional[str]
    error: Optional[str] = None


# --- stage 1 + 2: seed and traverse ---------------------------------------
def traverse(tg: TelemetryGraph, max_hops: int = MAX_HOPS) -> Dict[str, int]:
    """Every node that can reach a symptom within `max_hops`, with its depth.

    Walking the impact graph in reverse from the symptoms is what makes this
    retrieval rather than search: we never ask "what looks broken?", only
    "what could have produced exactly this set of symptoms?".
    """
    impact = tg.impact_graph()
    reverse = impact.reverse(copy=False)
    symptoms = tg.symptoms()

    depth: Dict[str, int] = {}
    for sym in symptoms:
        lengths = nx.single_source_shortest_path_length(reverse, sym, cutoff=max_hops)
        for node, dist in lengths.items():
            if node in symptoms:
                continue
            depth[node] = min(depth.get(node, 99), dist)
    return depth


# --- stage 3: rank ---------------------------------------------------------
def _corroboration(attrs: dict) -> float:
    """How much independent evidence says THIS node is genuinely misbehaving.

    Topology tells you what could propagate; this tells you what actually did.
    """
    if attrs.get("kind") == "Symptom":
        return 1.0
    if attrs.get("exonerated"):
        return 0.0  # probed and found healthy: a chain through it is not corroborated
    if attrs.get("anomalous"):
        return 1.0
    error_rate = attrs.get("error_rate")
    if error_rate:
        return min(1.0, float(error_rate) * 2)
    # Slow but not failing still counts: it is how a saturated dependency looks
    # from the outside, and it is the only trace-visible sign that the component
    # in the middle of the chain is the one under pressure.
    if attrs.get("slow"):
        return 0.8
    if attrs.get("evidence"):
        return 0.5
    return 0.0


def _mechanism_score(tg: TelemetryGraph, paths: List[List[str]]) -> float:
    """Mean corroboration of the intermediate hops on the causal paths.

    This is what separates a real root cause from a coincidental recent change.
    Both may be topologically capable of causing the symptoms; only one has a
    chain where every link independently looks broken.
    """
    if not paths:
        return 0.0
    per_path = []
    for path in paths:
        middle = path[1:-1]  # exclude the candidate itself and the symptom
        if not middle:
            # The candidate surfaces the symptom directly. That is only a proven
            # mechanism when the candidate itself is independently known to be
            # broken (e.g. its health check fails) -- otherwise it is merely the
            # place the pain shows up.
            per_path.append(1.0 if tg.g.nodes[path[0]].get("anomalous") else 0.0)
            continue
        per_path.append(
            sum(_corroboration(tg.g.nodes[n]) for n in middle) / len(middle))
    return sum(per_path) / len(per_path)


def _change_recency(attrs: dict) -> float:
    """Exponential decay on how long ago a change landed."""
    if attrs.get("kind") not in ("Deployment", "ConfigItem"):
        return 0.0
    stamp = attrs.get("at")
    if not stamp:
        return 0.5  # a change with no timestamp: suspicious, but unproven
    try:
        when = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0.5
    age_min = max(0.0, (datetime.now() - when).total_seconds() / 60.0)
    return float(2 ** (-age_min / CHANGE_HALF_LIFE_MIN))



def rank(tg: TelemetryGraph, max_hops: int = MAX_HOPS, top_n: int = 5) -> List[Candidate]:
    impact = tg.impact_graph()
    observed: Set[str] = set(tg.symptoms())
    if not observed:
        return []

    reachable_depth = traverse(tg, max_hops)
    candidates: List[Candidate] = []
    max_depth = max(reachable_depth.values()) if reachable_depth else 1

    for node_id, depth in reachable_depth.items():
        attrs = tg.g.nodes[node_id]
        kind = attrs.get("kind", "?")
        if attrs.get("exonerated"):
            continue  # live probes say this component is fine

        # Which symptoms would this node, if broken, actually produce?
        downstream = nx.descendants(impact, node_id)
        explains = downstream & observed
        if not explains:
            continue

        # Jaccard: rewards explaining everything, punishes predicting symptoms
        # that were never observed.
        blast_fit = len(explains) / len(explains | observed)

        # A ConfigItem carries no timestamp of its own -- it is only as recent as
        # the most recent deployment that touched it.
        change_signal = _change_recency(attrs)
        if kind == "ConfigItem":
            deploy_recency = [
                _change_recency(tg.g.nodes[n])
                for n in tg.g.successors(node_id)
                if tg.g.nodes[n].get("kind") == "Deployment"
            ]
            change_signal = max(deploy_recency) if deploy_recency else change_signal

        evidence: List[Evidence] = attrs.get("evidence", []) or []
        anomaly_signal = 0.0
        if attrs.get("anomalous"):
            anomaly_signal = 1.0
        elif attrs.get("error_rate"):
            anomaly_signal = min(1.0, float(attrs["error_rate"]) * 2)
        elif evidence:
            anomaly_signal = 0.4

        paths = []
        for sym in sorted(explains):
            try:
                paths.append(nx.shortest_path(impact, node_id, sym))
            except nx.NetworkXNoPath:
                continue

        mechanism = _mechanism_score(tg, paths)

        score = (W_BLAST * blast_fit
                 + W_MECHANISM * mechanism
                 + W_CHANGE * change_signal
                 + W_ANOMALY * anomaly_signal
                 - W_DEPTH * (depth / max(max_depth, 1)))

        candidates.append(Candidate(node_id, kind, score, blast_fit, mechanism,
                                    change_signal, anomaly_signal, depth,
                                    explains, paths))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]


# --- stage 4: serialize the retrieved subgraph ----------------------------
def _describe_node(tg: TelemetryGraph, node_id: str) -> str:
    attrs = tg.g.nodes.get(node_id, {})
    interesting = ("kind", "role", "health_status", "error_rate", "p95_ms", "span_count",
                   "max_size", "baseline_max_size", "active", "awaiting", "last_lag_ms",
                   "value", "previous", "key", "at", "by", "note", "title", "engine",
                   "first_error_at", "last_error_at", "last_seen_at", "down_since")
    bits = [k + "=" + str(attrs[k]) for k in interesting if attrs.get(k) is not None]
    return node_id + " [" + ", ".join(bits) + "]"


def _render_path(tg: TelemetryGraph, path: List[str]) -> str:
    impact = tg.impact_graph()
    parts = []
    for i, node_id in enumerate(path):
        parts.append(node_id)
        if i < len(path) - 1:
            rel = impact.get_edge_data(path[i], path[i + 1], {}).get("rel", "?")
            parts.append(" --" + rel + "--> ")
    return "".join(parts)


# How to read an impact edge u -> v ("u failing hurts v") out loud.
SPOKEN = {
    "CALLS": "is called by",
    "PUBLISHES_TO": "is published to by",
    "CONSUMES_FROM": "is linked through the queue to",
    "USES_POOL": "is the connection pool of",
    "BACKED_BY": "is the database behind",
    "RUNS_ON": "is the host of",
    "HOSTS": "hosts the queue",
    "CO_TENANT_OF": "shares a host with",
    "CONFIGURES": "configures",
    "CHANGED_IN": "changed",
}


def explain_path(tg: TelemetryGraph, path: List[str]) -> List[str]:
    """A causal path as plain-language steps, for people rather than graphs."""
    impact = tg.impact_graph()
    steps = []
    for u, v in zip(path, path[1:]):
        rel = impact.get_edge_data(u, v, {}).get("rel", "?")
        if rel == "MANIFESTS_AS":
            steps.append("users see it: " + str(tg.g.nodes[v].get("title", v)))
        else:
            steps.append(u + " " + SPOKEN.get(rel, rel.lower()) + " " + v)
    return steps


def build_prompt(tg: TelemetryGraph, candidates: List[Candidate], include_task: bool = True) -> str:
    lines: List[str] = []
    lines.append("## Observed symptoms")
    for sym in tg.symptoms():
        attrs = tg.g.nodes[sym]
        lines.append("- " + sym + ": " + str(attrs.get("title")))
        for ev in (attrs.get("evidence") or [])[:4]:
            lines.append("    evidence[" + ev.kind + " @ " + ev.source + "]: " + ev.text)

    lines.append("")
    lines.append("## Retrieved causal subgraph")
    lines.append("Ranked by how well each candidate explains the FULL symptom set.")
    lines.append("")

    for i, cand in enumerate(candidates, 1):
        lines.append(str(i) + ". " + _describe_node(tg, cand.node_id))
        lines.append("   ranking: " + cand.why())
        lines.append("   explains " + str(len(cand.explains)) + " of "
                     + str(len(tg.symptoms())) + " symptoms: " + ", ".join(sorted(cand.explains)))
        for path in cand.paths:
            lines.append("   causal path: " + _render_path(tg, path))
        for ev in (tg.g.nodes[cand.node_id].get("evidence") or [])[:5]:
            lines.append("   evidence[" + ev.kind + " @ " + ev.source + "]: " + ev.text)
        lines.append("")

    if not include_task:
        return "\n".join(lines)
    lines.append("## Task")
    lines.append(
        "Identify the single most likely root cause. Then:\n"
        "1. State the root cause in one sentence, naming the specific component "
        "and, if a change caused it, the specific deployment and config key.\n"
        "2. Give the causal chain from root cause to each observed symptom, as a "
        "numbered list, citing the graph edges shown above.\n"
        "3. Explain briefly why the highest-scoring candidate beats the "
        "second-highest -- refer to blast-radius fit.\n"
        "4. State the immediate remediation.\n"
        "5. Give a confidence level (high/medium/low) and say what evidence "
        "would raise it."
    )
    return "\n".join(lines)


# --- orchestration ---------------------------------------------------------
def analyze(tg: TelemetryGraph, top_n: int = 5) -> RCAResult:
    candidates = rank(tg, top_n=top_n)
    if not candidates:
        return RCAResult([], "", None, error="No symptoms detected -- the system looks healthy.")

    prompt = build_prompt(tg, candidates)
    try:
        answer = llm.complete(prompt, system=SYSTEM_PROMPT)
        return RCAResult(candidates, prompt, answer)
    except llm.NoLLMConfigured as e:
        return RCAResult(candidates, prompt, None, error=str(e))
    except llm.LLMError as e:
        return RCAResult(candidates, prompt, None, error=str(e))


if __name__ == "__main__":
    import graph as graph_mod

    tg = graph_mod.build()
    print(tg.summary())
    result = analyze(tg)
    for cand in result.candidates:
        print(cand.node_id, "->", cand.why())
