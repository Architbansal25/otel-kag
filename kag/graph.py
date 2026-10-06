"""Builds the knowledge graph the KAG engine reasons over.

Four sources, deliberately kept distinct because they have different trust levels:

  1. facts.yaml      -- declared infrastructure (CMDB / Helm / Terraform)
  2. Jaeger          -- observed runtime behaviour
  3. live probes     -- current pool + consumer state
  4. deploys.json    -- the change log, written by ops/deploy.ps1

The important output is not the graph itself but `impact_graph()`: a directed view
where an edge u -> v means "u failing can cause v to fail". Every edge type below
declares how causality flows along it, and that declaration is what lets the
engine walk from a symptom back to a cause nobody thought to look for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import networkx as nx
import requests
import yaml

HERE = Path(__file__).resolve().parent
JAEGER_API = os.environ.get("JAEGER_API", "http://localhost:16686/api/traces")
PROBE_TIMEOUT = 3

# How causality flows along each relation.
#   forward -> edge u->v means u failing impacts v
#   reverse -> edge u->v means v failing impacts u
#   both    -> either end can degrade the other
#   None    -> annotation only, carries no causality
IMPACT_DIRECTION: Dict[str, Optional[str]] = {
    "CALLS":         "reverse",   # caller depends on callee
    "PUBLISHES_TO":  "reverse",   # producer depends on the queue
    "CONSUMES_FROM": "both",      # broken queue stalls consumer; slow consumer backs up queue
    "USES_POOL":     "reverse",   # service depends on its pool
    "BACKED_BY":     "reverse",   # pool depends on the datastore
    "RUNS_ON":       "reverse",   # service depends on its host
    "HOSTS":         "reverse",   # queue depends on its broker
    "CO_TENANT_OF":  "both",      # noisy-neighbour coupling
    "CONFIGURES":    "forward",   # a wrong config value breaks what it configures
    "CHANGED_IN":    "reverse",   # the deployment is what changed the config item
    # TARGETS is deliberately NOT causal. A deployment does not break a service by
    # magic -- it breaks it through something it changed. Leaving this causal gave
    # deploys a one-hop shortcut straight to the service, bypassing the config and
    # pool nodes that carry the actual evidence, which made every recent deploy
    # look equally guilty. Forcing causality through CHANGED_IN -> CONFIGURES is
    # what lets the ranking tell a real cause from a coincidental change.
    "TARGETS":       None,
    "MANIFESTS_AS":  "forward",   # a broken component surfaces as a symptom
    "EVIDENCED_BY":  None,
}

# 5% of REQUESTS failing is unambiguously user-visible and would page someone.
# Measured per trace, not per span -- see ingest_traces().
ERROR_RATE_SYMPTOM_THRESHOLD = 0.05
QUEUE_LAG_SYMPTOM_THRESHOLD_MS = 1000

# A service can be the sick one without ever returning an error: under pool
# starvation inventory-svc still answers 200, just slowly, and the timeout fires
# one hop upstream. Latency is the only evidence that it is struggling, so it has
# to count as corroboration or the true causal chain looks unsupported.
SLOW_SERVICE_P95_MS = 1000


@dataclass
class Evidence:
    """A concrete, citable observation. Every RCA claim must point at one of these."""
    kind: str          # "log" | "span" | "metric" | "change"
    source: str        # where it came from, e.g. "jaeger:trace:abc123"
    text: str
    ts: Optional[int] = None


class TelemetryGraph:
    def __init__(self) -> None:
        self.g = nx.MultiDiGraph()

    # --- construction -----------------------------------------------------
    def node(self, node_id: str, kind: str, **attrs: Any) -> str:
        if self.g.has_node(node_id):
            self.g.nodes[node_id].update({k: v for k, v in attrs.items() if v is not None})
        else:
            self.g.add_node(node_id, kind=kind, evidence=[], **attrs)
        return node_id

    def edge(self, u: str, v: str, rel: str, **attrs: Any) -> None:
        if not self.g.has_edge(u, v, key=rel):
            self.g.add_edge(u, v, key=rel, rel=rel, **attrs)

    def attach(self, node_id: str, ev: Evidence) -> None:
        if self.g.has_node(node_id):
            self.g.nodes[node_id].setdefault("evidence", []).append(ev)

    # --- views ------------------------------------------------------------
    def impact_graph(self) -> nx.DiGraph:
        """Directed view where u -> v means 'u failing can cause v to fail'."""
        out = nx.DiGraph()
        out.add_nodes_from(self.g.nodes(data=True))
        for u, v, rel in self.g.edges(keys=True):
            direction = IMPACT_DIRECTION.get(rel)
            if direction in ("forward", "both"):
                out.add_edge(u, v, rel=rel)
            if direction in ("reverse", "both"):
                out.add_edge(v, u, rel=rel)
        return out

    def nodes_of(self, kind: str) -> List[str]:
        return [n for n, d in self.g.nodes(data=True) if d.get("kind") == kind]

    def symptoms(self) -> List[str]:
        return self.nodes_of("Symptom")

    def summary(self) -> str:
        kinds: Dict[str, int] = {}
        for _, d in self.g.nodes(data=True):
            kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
        parts = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        return f"{self.g.number_of_nodes()} nodes ({parts}), {self.g.number_of_edges()} edges"


# --- 1. declared infrastructure -------------------------------------------
def load_facts(tg: TelemetryGraph, path: Path) -> None:
    facts = yaml.safe_load(path.read_text(encoding="utf-8"))

    for host in facts.get("hosts", []):
        tg.node(host["id"], "Host", cores=host.get("cores"), memory_gb=host.get("memory_gb"))

    for svc in facts.get("services", []):
        tg.node(svc["id"], "Service", port=svc.get("port"), role=svc.get("role"))
        if svc.get("runs_on"):
            tg.edge(svc["id"], svc["runs_on"], "RUNS_ON")

    # Co-tenancy is derived, not declared -- exactly the kind of implicit
    # relationship that never appears on a dashboard but explains real incidents.
    by_host: Dict[str, List[str]] = {}
    for svc in facts.get("services", []):
        by_host.setdefault(svc.get("runs_on"), []).append(svc["id"])
    for host, services in by_host.items():
        for i, a in enumerate(services):
            for b in services[i + 1:]:
                tg.edge(a, b, "CO_TENANT_OF", host=host)

    for q in facts.get("queues", []):
        tg.node(q["id"], "Queue")
        if q.get("hosted_by"):
            tg.edge(q["hosted_by"], q["id"], "HOSTS")

    for ds in facts.get("datastores", []):
        tg.node(ds["id"], "Datastore", engine=ds.get("engine"))

    for pool in facts.get("connection_pools", []):
        tg.node(pool["id"], "ConnectionPool",
                baseline_max_size=pool.get("baseline_max_size"),
                config_key=pool.get("config_key"))
        tg.edge(pool["owned_by"], pool["id"], "USES_POOL")
        if pool.get("backs"):
            tg.edge(pool["id"], pool["backs"], "BACKED_BY")

    rel_for_kind = {"http": "CALLS", "publish": "PUBLISHES_TO", "consume": "CONSUMES_FROM"}
    for dep in facts.get("dependencies", []):
        tg.edge(dep["from"], dep["to"], rel_for_kind.get(dep.get("kind"), "CALLS"),
                declared=True)


# --- 2. observed runtime behaviour ----------------------------------------
def _span_error(span: Dict[str, Any]) -> bool:
    for tag in span.get("tags", []):
        key, value = tag.get("key"), str(tag.get("value"))
        if key == "error" and value.lower() == "true":
            return True
        if key in ("http.status_code", "http.response.status_code") and value.isdigit():
            if int(value) >= 500:
                return True
        if key == "otel.status_code" and value.upper() == "ERROR":
            return True
    return False


def _span_messages(span: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for logrec in span.get("logs", []):
        for field in logrec.get("fields", []):
            if field.get("key") in ("event", "message", "error.message",
                                    "exception.message", "exception.type"):
                if field.get("value"):
                    out.append(str(field["value"]))
    for tag in span.get("tags", []):
        if tag.get("key") in ("error.message", "exception.message"):
            out.append(str(tag.get("value")))
    return out


def fetch_traces(service: str, lookback: str = "15m", limit: int = 200) -> List[Dict[str, Any]]:
    try:
        resp = requests.get(JAEGER_API,
                            params={"service": service, "lookback": lookback, "limit": limit},
                            timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", []) or []
    except (requests.RequestException, ValueError):
        return []


def ingest_traces(tg: TelemetryGraph, services: Iterable[str], lookback: str = "15m") -> None:
    """Confirms topology from real spans and raises Symptom nodes where users hurt."""
    stats: Dict[str, Dict[str, Any]] = {}
    seen_traces: set = set()

    # Error rate is measured per REQUEST, not per span.
    #
    # The OTel Java agent emits several spans per request -- an inbound server
    # span, an outbound client span, a JMS producer span -- and only some carry
    # the error. Dividing error spans by total spans therefore under-reports the
    # failure rate roughly threefold: a load generator reporting 10% of requests
    # failing showed up here as 3.3%, silently sat below the symptom threshold,
    # and the symptom never fired. Counting distinct traces touched vs. traces
    # containing an error for that service gives the number an SRE actually
    # means by "29% of requests are failing".
    traces_touched: Dict[str, set] = {}
    traces_failed: Dict[str, set] = {}

    for service in services:
        for trace in fetch_traces(service, lookback):
            trace_id = trace.get("traceID")
            if not trace_id or trace_id in seen_traces:
                continue
            seen_traces.add(trace_id)

            processes = trace.get("processes", {}) or {}
            spans_by_id = {s.get("spanID"): s for s in trace.get("spans", []) if s.get("spanID")}

            def svc_of(span: Dict[str, Any]) -> str:
                proc = processes.get(span.get("processID", "")) or {}
                return proc.get("serviceName", "unknown")

            for span in spans_by_id.values():
                owner = svc_of(span)
                st = stats.setdefault(owner, {"total": 0, "errors": 0,
                                              "durations": [], "messages": []})
                st["total"] += 1
                st["durations"].append(span.get("duration", 0) / 1000.0)  # us -> ms
                traces_touched.setdefault(owner, set()).add(trace_id)

                if _span_error(span):
                    st["errors"] += 1
                    traces_failed.setdefault(owner, set()).add(trace_id)
                    msgs = _span_messages(span)
                    st["messages"].extend(msgs)
                    detail = " -- " + msgs[0] if msgs else ""
                    tg.attach(owner, Evidence(
                        kind="span",
                        source="jaeger:trace:" + trace_id + ":span:" + str(span.get("spanID")),
                        text=owner + " " + str(span.get("operationName")) + " FAILED" + detail,
                        ts=span.get("startTime")))

                # Confirm cross-service calls from the actual parent-child structure.
                for ref in span.get("references", []):
                    if ref.get("refType") != "CHILD_OF":
                        continue
                    parent = spans_by_id.get(ref.get("spanID"))
                    if not parent:
                        continue
                    caller, callee = svc_of(parent), owner
                    if caller != callee and tg.g.has_node(caller) and tg.g.has_node(callee):
                        tg.edge(caller, callee, "CALLS", observed=True)

    # Turn per-service statistics into Symptom nodes.
    for service, st in stats.items():
        if not tg.g.has_node(service) or st["total"] == 0:
            continue
        touched = len(traces_touched.get(service, ()))
        failed = len(traces_failed.get(service, ()))
        error_rate = (failed / touched) if touched else 0.0
        durations = sorted(st["durations"])
        p95 = durations[min(int(len(durations) * 0.95), len(durations) - 1)] if durations else 0.0
        tg.g.nodes[service].update(span_count=st["total"],
                                   request_count=touched,
                                   failed_requests=failed,
                                   error_rate=round(error_rate, 3),
                                   p95_ms=round(p95, 1))

        if p95 >= SLOW_SERVICE_P95_MS:
            tg.g.nodes[service]["slow"] = True
            tg.attach(service, Evidence(
                "metric", "jaeger:" + service,
                "p95 latency " + str(round(p95, 1)) + " ms over "
                + str(st["total"]) + " spans"))

        if error_rate >= ERROR_RATE_SYMPTOM_THRESHOLD:
            sym = tg.node("symptom:" + service + ":errors", "Symptom",
                          title=service + " failing " + format(error_rate, ".0%")
                                + " of requests (" + str(failed) + "/" + str(touched) + ")",
                          severity=round(error_rate, 3))
            tg.edge(service, sym, "MANIFESTS_AS")
            for msg in list(dict.fromkeys(st["messages"]))[:3]:
                tg.attach(sym, Evidence("log", "jaeger:" + service, msg))


# --- 3. live probes --------------------------------------------------------
def probe_runtime(tg: TelemetryGraph,
                  pool_url: str = "http://localhost:8082/admin/pool",
                  stats_url: str = "http://localhost:8083/admin/stats") -> None:
    """Reads current pool and consumer state. This is where saturation shows up."""
    try:
        pool = requests.get(pool_url, timeout=PROBE_TIMEOUT).json()
        if tg.g.has_node("inventory-pool"):
            max_size = pool.get("maxPoolSize")
            awaiting = pool.get("awaitingConnection") or 0
            tg.g.nodes["inventory-pool"].update(max_size=max_size,
                                                active=pool.get("active"),
                                                awaiting=awaiting)
            baseline = tg.g.nodes["inventory-pool"].get("baseline_max_size")
            tg.attach("inventory-pool", Evidence(
                "metric", pool_url,
                "pool max=" + str(max_size) + " active=" + str(pool.get("active"))
                + " threads_awaiting=" + str(awaiting)))

            shrunk = baseline is not None and (max_size or 0) < baseline
            if awaiting > 0 or shrunk:
                tg.g.nodes["inventory-pool"]["anomalous"] = True
                if shrunk:
                    tg.attach("inventory-pool", Evidence(
                        "metric", pool_url,
                        "capacity is " + str(max_size)
                        + ", below the baseline of " + str(baseline)))
    except (requests.RequestException, ValueError, KeyError):
        pass

    try:
        st = requests.get(stats_url, timeout=PROBE_TIMEOUT).json()
        spot_lag = st.get("lastLagMs", 0) or 0
        # Peak-within-window, not the spot value: the consumer drains the backlog
        # within seconds of traffic stopping, so `lastLagMs` reads ~1ms moments
        # after a real incident and the symptom would silently disappear.
        lag = st.get("maxLagMs", spot_lag) or spot_lag
        if tg.g.has_node("order.events"):
            tg.g.nodes["order.events"].update(last_lag_ms=spot_lag, peak_lag_ms=lag,
                                              processed=st.get("processed"))
            if lag >= QUEUE_LAG_SYMPTOM_THRESHOLD_MS:
                window_min = int((st.get("lagWindowMs") or 300000) / 60000)
                sym = tg.node("symptom:order.events:lag", "Symptom",
                              title="order.events consumer lag peaked at " + str(lag)
                                    + " ms in the last " + str(window_min) + " min",
                              severity=min(1.0, lag / 10000.0))
                tg.edge("order.events", sym, "MANIFESTS_AS")
                tg.attach(sym, Evidence("metric", stats_url,
                                        "peak consumer lag " + str(lag) + " ms (currently "
                                        + str(spot_lag) + " ms)"))
    except (requests.RequestException, ValueError, KeyError):
        pass


# --- 4. the change log -----------------------------------------------------
def load_deploys(tg: TelemetryGraph, path: Path) -> None:
    """Deployments and the config items they touched.

    Without this the graph can reach the pool but can never say WHY the pool is
    small -- the difference between "the pool is saturated" and an actionable
    root cause that names a specific change.
    """
    if not path.exists():
        return
    for dep in json.loads(path.read_text(encoding="utf-8") or "[]"):
        dep_id = "deploy:" + str(dep["id"])
        tg.node(dep_id, "Deployment", at=dep.get("at"), by=dep.get("by"),
                note=dep.get("note"), target=dep.get("target"))
        if dep.get("target") and tg.g.has_node(dep["target"]):
            tg.edge(dep_id, dep["target"], "TARGETS")
        tg.attach(dep_id, Evidence("change", "deploys.json",
                                   str(dep.get("at")) + " " + str(dep.get("by"))
                                   + ": " + str(dep.get("note"))))

        for change in dep.get("changes", []):
            cfg_id = "config:" + change["key"]
            tg.node(cfg_id, "ConfigItem", key=change["key"],
                    value=change.get("to"), previous=change.get("from"))
            tg.edge(cfg_id, dep_id, "CHANGED_IN")
            tg.attach(cfg_id, Evidence("change", "deploys.json",
                                       change["key"] + ": " + str(change.get("from"))
                                       + " -> " + str(change.get("to"))))
            if change.get("configures") and tg.g.has_node(change["configures"]):
                tg.edge(cfg_id, change["configures"], "CONFIGURES")


# --- orchestration ---------------------------------------------------------
def build(lookback: str = "15m", probe: bool = True) -> TelemetryGraph:
    tg = TelemetryGraph()
    load_facts(tg, HERE / "facts.yaml")
    ingest_traces(tg, tg.nodes_of("Service"), lookback)
    if probe:
        probe_runtime(tg)
    load_deploys(tg, HERE / "deploys.json")
    return tg


if __name__ == "__main__":
    graph = build()
    print(graph.summary())
    print("Symptoms:", graph.symptoms() or "none detected")
