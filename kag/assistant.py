""""Is anything breaking?" -- the question-answering front end of the KAG engine.

    from assistant import ask
    result = ask("Is the application running fine?")
    print(result.report.model_dump_json(indent=2))

Pipeline, each step reported to the UI as it happens:

  1. OBSERVE   health-check every service, read traces from Jaeger, probe the
               pool and the queue, load deploys + monitor history
  2. SEED      turn what hurts into Symptom nodes (down / failing / slow / lagging)
  3. RANK      walk the impact graph back from the symptoms and score candidates
  4. REASON    LLM fills the `Diagnosis` schema from the retrieved subgraph,
               the measured service table and the timeline

The per-service table is measured, not generated. If no LLM is configured (or
the call fails) a rule-based diagnosis fills the same schema, so the answer
always arrives in the same shape.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import graph as graph_mod
import kag_engine
import llm
from report import Diagnosis, HealthReport, Incident, ServiceHealth

DEFAULT_WINDOW = "5m"
RECENT_MIN_REQUESTS = 3

SYSTEM_PROMPT = (
    "You are the on-call SRE assistant for a small order-processing platform. "
    "You answer questions about whether the system is healthy and, if it is not, "
    "what broke, why, and since when. "
    "Reason only over the observations you are given: live health checks, measured "
    "per-service traffic, a timeline, and a causal subgraph retrieved from an "
    "infrastructure knowledge graph and pre-ranked by how well each candidate explains "
    "ALL the symptoms. Prefer the top-ranked candidate unless the evidence contradicts it. "
    "Use exact service names, node ids and timestamps from the input; never invent them. "
    "A service whose health check fails is DOWN. Failures or slow responses in the last "
    "minute mean DEGRADED. If problems show up only earlier in the window and the last "
    "minute is clean, the system is HEALTHY now: report the incident as over "
    "(ongoing=false, ended_at set). The timeline can also hold an EARLIER incident "
    "that already recovered (a health monitor DOWN -> UP line): describe the current "
    "problem, with timestamps after that recovery, and mention the earlier one only "
    "briefly if at all. If nothing is wrong, say so plainly and set "
    "incident to null. Write for an audience that is not technical: short, concrete sentences."
)

StageFn = Callable[[str, str, str], None]


@dataclass
class AskResult:
    report: HealthReport
    prompt: str
    candidates: List[dict] = field(default_factory=list)
    llm_error: Optional[str] = None
    elapsed_s: float = 0.0


# --- measured service table -------------------------------------------------
def service_health(tg: graph_mod.TelemetryGraph, window: str) -> List[ServiceHealth]:
    out: List[ServiceHealth] = []
    lagging = tg.g.has_node("symptom:order.events:lag")
    for name in tg.nodes_of("Service"):
        a = tg.g.nodes[name]
        if a.get("up") is False:
            since = a.get("down_since") or a.get("last_seen_at")
            detail = "health check failed (" + str(a.get("health_status", "")).lower() + ")"
            if a.get("down_since"):
                detail += "; down since " + a["down_since"]
            elif since:
                detail += "; last trace seen " + since
            out.append(ServiceHealth(name=name, status="DOWN", reachable=False,
                                     error_rate_pct=round(100 * (a.get("error_rate") or 0), 1),
                                     p95_latency_ms=a.get("p95_ms") or 0.0,
                                     requests_observed=a.get("request_count") or 0,
                                     last_error_at=a.get("last_error_at"), detail=detail))
            continue

        if (a.get("recent_requests") or 0) >= RECENT_MIN_REQUESTS:
            err, p95, basis = a.get("recent_error_rate") or 0.0, a.get("recent_p95_ms") or 0.0, "last minute"
            sample = a.get("recent_requests") or 0
        else:
            err, p95, basis = a.get("error_rate") or 0.0, a.get("p95_ms") or 0.0, "last " + window
            sample = a.get("request_count") or 0
        if sample < graph_mod.MIN_REQUESTS_FOR_LATENCY:
            p95 = 0.0 if p95 >= graph_mod.SLOW_SERVICE_P95_MS else p95  # too few to judge

        reasons = []
        if err >= graph_mod.ERROR_RATE_SYMPTOM_THRESHOLD:
            reasons.append(f"{err:.0%} of requests failing ({basis})")
        if p95 >= graph_mod.SLOW_SERVICE_P95_MS:
            reasons.append(f"slow: p95 {p95:.0f} ms ({basis})")
        if a.get("health_status") not in (None, "UP"):
            reasons.append("health " + str(a["health_status"]))
        history = ""
        if lagging and name == "notification-svc":
            q = tg.g.nodes.get("order.events", {})
            if (q.get("last_lag_ms") or 0) >= graph_mod.QUEUE_LAG_SYMPTOM_THRESHOLD_MS:
                reasons.append(f"falling behind on order.events (lag {q.get('last_lag_ms')} ms now)")
            else:
                history = (f"caught up; order.events lag peaked at {q.get('peak_lag_ms')} ms "
                           "earlier in the last 5 min")

        if reasons:
            detail = "; ".join(reasons)
        elif history:
            detail = history
        elif a.get("last_error_at") and (a.get("recent_requests") or 0) >= RECENT_MIN_REQUESTS:
            detail = "recovered; last failure at " + a["last_error_at"]
        elif not a.get("request_count"):
            detail = "up; no traffic in the last " + window
        else:
            detail = "up; requests succeeding"
        out.append(ServiceHealth(
            name=name, status="DEGRADED" if reasons else "HEALTHY", reachable=True,
            error_rate_pct=round(100 * err, 1), p95_latency_ms=round(p95, 1),
            requests_observed=a.get("request_count") or 0,
            last_error_at=a.get("last_error_at"), detail=detail))
    return out


def overall(services: List[ServiceHealth]) -> str:
    states = {s.status for s in services}
    return "DOWN" if "DOWN" in states else "DEGRADED" if "DEGRADED" in states else "HEALTHY"


# --- timeline -----------------------------------------------------------------
def timeline(tg: graph_mod.TelemetryGraph) -> List[str]:
    rows = []
    for name in tg.nodes_of("Service"):
        a = tg.g.nodes[name]
        if a.get("first_error_at"):
            rows.append((a["first_error_at"], name + ": first failed request"))
        if a.get("last_error_at") and a.get("last_error_at") != a.get("first_error_at"):
            rows.append((a["last_error_at"], name + ": most recent failed request"))
        if a.get("first_slow_at"):
            rows.append((a["first_slow_at"], name + ": first request slower than "
                         + str(graph_mod.SLOW_SERVICE_P95_MS) + " ms"))
        if a.get("up") is False and a.get("last_seen_at"):
            rows.append((a["last_seen_at"], name + ": last trace span received (it has been silent since)"))
    for ev in graph_mod.load_health_events():
        if ev.get("epoch", 0) >= time.time() - 3600:
            rows.append((ev.get("at", ""), ev["service"] + ": health monitor saw " + ev.get("from", "?")
                         + " -> " + ev.get("to", "?")
                         + (" (" + ev["detail"] + ")" if ev.get("detail") else "")))
    for dep in tg.nodes_of("Deployment"):
        a = tg.g.nodes[dep]
        if a.get("at"):
            rows.append((str(a["at"]).replace("T", " "),
                         dep + " to " + str(a.get("target")) + ": " + str(a.get("note"))))
    return [t + "  " + text for t, text in sorted(rows)]


# --- prompt -------------------------------------------------------------------
def build_prompt(question: str, tg: graph_mod.TelemetryGraph, services: List[ServiceHealth],
                 candidates: List[kag_engine.Candidate], window: str, jaeger_ok: bool) -> str:
    lines = ["## Question", question, "",
             "## Context",
             "Now: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
             + "   Trace window: last " + window
             + ("" if jaeger_ok else "   WARNING: Jaeger unreachable, no trace data"),
             "Topology: order-api -> inventory-svc (HTTP); order-api -> order.events queue "
             "(hosted by broker) -> notification-svc, which also calls inventory-svc.", "",
             "## Live service health (measured just now)"]
    for s in services:
        a = tg.g.nodes[s.name]
        extra = []
        if a.get("request_count"):
            extra.append(f"window: {a['request_count']} requests, "
                         f"{100 * (a.get('error_rate') or 0):.0f}% failed, p95 {a.get('p95_ms')} ms")
        if a.get("recent_requests"):
            extra.append(f"last minute: {a['recent_requests']} requests, "
                         f"{100 * (a.get('recent_error_rate') or 0):.0f}% failed, "
                         f"p95 {a.get('recent_p95_ms')} ms")
        if a.get("error_codes"):
            extra.append("error codes " + ", ".join(f"{k} x{v}" for k, v in a["error_codes"].items()))
        lines.append(f"- {s.name}: {s.status}. {s.detail}" + (" | " + " | ".join(extra) if extra else ""))

    events = timeline(tg)
    lines += ["", "## Timeline (traces, health monitor, deployments)"]
    lines += ["- " + e for e in events] if events else ["- nothing notable"]

    lines.append("")
    if candidates:
        lines.append(kag_engine.build_prompt(tg, candidates, include_task=False))
    else:
        lines += ["## Observed symptoms", "None: no failures, slow requests, down services "
                  "or queue lag in the window."]

    lines += ["", "## Your task",
              "Answer the question by filling the Diagnosis schema.",
              "- answer: speak to the question directly, in 2-3 sentences.",
              "- incident.root_cause_component: the exact node id of the root cause.",
              "- incident.started_at / ended_at: copy timestamps from the timeline above.",
              "- incident.causal_chain: one step per hop, root cause first, user impact last.",
              "- incident.evidence: quote the specific observations you relied on."]
    return "\n".join(lines)


# --- rule-based fallback --------------------------------------------------------
TS = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def last_recovery_at() -> str:
    """When a service last came back UP. Earlier evidence belongs to a previous
    incident -- the demo often breaks things twice inside one trace window."""
    ups = [ev.get("at", "") for ev in graph_mod.load_health_events()
           if ev.get("to") == "UP" and ev.get("epoch", 0) >= time.time() - 3600]
    return max(ups) if ups else ""

REMEDIATION = {
    "service_down": ["Restart the service and confirm its health check returns UP",
                     "Check its logs for why the process exited",
                     "Callers are failing fast; no other action needed for them"],
    "high_latency": ["Find what is slowing the service's data access (slow query, lock, I/O)",
                     "Roll back any recent change to that service",
                     "Consider raising caller timeouts only as a stop-gap"],
    "error_spike": ["Inspect the failing spans in Jaeger for the exception",
                    "Roll back any recent change to the failing component"],
    "bad_deployment": ["Roll back the deployment / restore the previous config value",
                       "Add a guard so this config cannot drop below a safe floor"],
    "resource_exhaustion": ["Restore the resource's capacity (pool size / workers)",
                            "Shed load until it recovers"],
}


def rule_diagnosis(tg: graph_mod.TelemetryGraph, services: List[ServiceHealth],
                   candidates: List[kag_engine.Candidate]) -> Diagnosis:
    status = overall(services)
    broken = [s for s in services if s.status != "HEALTHY"]

    if not candidates:
        if status == "HEALTHY":
            return Diagnosis(
                overall_status="HEALTHY", is_anything_breaking=False, incident=None,
                confidence="high",
                answer="Everything is running fine. All " + str(len(services))
                       + " services pass their health checks and requests are succeeding "
                       "at normal speed.")
        return Diagnosis(overall_status=status, is_anything_breaking=True, incident=None,
                         confidence="low",
                         answer="Something is off: " + "; ".join(
                             s.name + " " + s.detail for s in broken))

    # An old change is not a suspect for a fresh incident, and a weak best guess
    # is reported as a weak guess -- not dressed up as a root cause.
    credible = [c for c in candidates
                if not (c.kind in ("Deployment", "ConfigItem") and c.change_signal < 0.1)]
    ongoing = status != "HEALTHY"
    if not credible or credible[0].score < 0.45:
        sym = tg.symptoms()[0]
        origin = next(iter(tg.g.predecessors(sym)), sym)
        title = str(tg.g.nodes[sym].get("title"))
        return Diagnosis(
            overall_status=status, is_anything_breaking=ongoing, confidence="low",
            answer=(("Something is off: " if ongoing else "It is running fine now. Earlier: ")
                    + title + ". No single component clearly explains it."),
            incident=Incident(
                title=title, affected_services=sorted({s.name for s in broken}),
                root_cause_component=origin,
                root_cause="Unclear: " + title + ", with no failing dependency behind it.",
                category="unknown", started_at=None, ended_at=None, ongoing=ongoing,
                causal_chain=[], evidence=[ev.text for ev in
                                           (tg.g.nodes[sym].get("evidence") or [])[:4]],
                remediation=["Keep watching; ask again if it recurs"]))

    top = credible[0]
    a = tg.g.nodes[top.node_id]
    kind = top.kind
    if kind == "Service" and a.get("up") is False:
        category, title = "service_down", top.node_id + " is down"
        cause = top.node_id + " is not running: its health check fails and callers cannot connect."
    elif kind in ("Deployment", "ConfigItem"):
        category, title = "bad_deployment", "Bad change: " + top.node_id
        cause = top.node_id + " changed configuration that the failing path depends on."
    elif kind == "ConnectionPool":
        category, title = "resource_exhaustion", top.node_id + " is starved"
        cause = top.node_id + " is below its baseline capacity, so requests queue for connections."
    elif kind == "Service" and a.get("slow"):
        category, title = "high_latency", top.node_id + " is slow"
        cause = (top.node_id + " is responding slowly (p95 " + str(a.get("p95_ms"))
                 + " ms), so its callers time out or slow down.")
    elif kind == "Service":
        category, title = "error_spike", top.node_id + " is failing requests"
        cause = top.node_id + " is returning errors that propagate to its callers."
    else:
        category, title = "unknown", "Problem at " + top.node_id
        cause = "The most likely origin is " + top.node_id + "."

    recovered = last_recovery_at()
    if category == "high_latency":
        starts = [a.get("first_slow_at")]
    else:
        starts = [a.get("down_since"), a.get("first_slow_at"), a.get("first_error_at")]
        for sym in top.explains:
            for pred in tg.g.predecessors(sym):
                starts.append(tg.g.nodes[pred].get("first_error_at"))
    starts = [x for x in starts if x and x >= recovered]
    ends = [s.last_error_at for s in services if s.last_error_at]
    evidence: List[str] = []
    for node in [top.node_id] + sorted(top.explains):
        for ev in (tg.g.nodes[node].get("evidence") or [])[:4]:
            # one-word exception types ("exception") say nothing; repeats say less
            stamp = TS.search(ev.text)
            stale = ev.source == "health-monitor" and stamp and stamp.group() < recovered
            if len(ev.text) > 15 and ev.text not in evidence and not stale:
                evidence.append(ev.text)
    chain: List[str] = []
    for p in top.paths:
        for step in kag_engine.explain_path(tg, p):
            if step not in chain:
                chain.append(step)

    return Diagnosis(
        overall_status=status, is_anything_breaking=ongoing, confidence="medium",
        answer=(("Something is breaking. " if ongoing else "It is running fine now, but there was an incident. ")
                + cause),
        incident=Incident(
            title=title,
            affected_services=sorted({s.name for s in broken} | (
                {top.node_id} if kind == "Service" else set())),
            root_cause_component=top.node_id, root_cause=cause, category=category,
            started_at=min(starts) if starts else None,
            ended_at=None if ongoing else (max(ends) if ends else None),
            ongoing=ongoing,
            causal_chain=chain,
            evidence=evidence[:8],
            remediation=REMEDIATION.get(category, ["Investigate " + top.node_id])))


# --- orchestration --------------------------------------------------------------
def ask(question: str, window: str = DEFAULT_WINDOW, use_llm: bool = True,
        on_stage: Optional[StageFn] = None) -> AskResult:
    started = time.time()
    stage = on_stage or (lambda *_: None)
    question = question.strip() or "Is anything breaking, or is the application running fine?"
    reasoning: List[str] = []

    stage("observe", "running", "health checks, traces, pool, queue")
    jaeger_ok = graph_mod.jaeger_reachable()
    tg = graph_mod.build(lookback=window)
    services = service_health(tg, window)
    up = sum(1 for s in services if s.reachable)
    traced = sum(tg.g.nodes[s.name].get("request_count") or 0 for s in services)
    msg = (f"{up}/{len(services)} services answering health checks; "
           f"{traced} traced requests in the last {window}"
           + ("" if jaeger_ok else "; Jaeger unreachable"))
    reasoning.append("Observe: " + msg)
    stage("observe", "done", msg)

    symptoms = tg.symptoms()
    stage("seed", "running", "")
    msg = (f"{len(symptoms)} symptom(s): " + ", ".join(s.replace("symptom:", "") for s in symptoms)
           if symptoms else "no symptoms")
    reasoning.append("Seed: " + msg)
    stage("seed", "done", msg)

    candidates: List[kag_engine.Candidate] = []
    if symptoms:
        stage("rank", "running", "")
        candidates = kag_engine.rank(tg)
        msg = (f"{len(candidates)} candidate causes; top: {candidates[0].node_id} "
               f"(score {candidates[0].score:.2f}, explains {len(candidates[0].explains)}"
               f"/{len(symptoms)} symptoms)" if candidates else "no candidate explains the symptoms")
        reasoning.append("Rank: " + msg)
        stage("rank", "done", msg)
    else:
        stage("rank", "skipped", "nothing to explain")

    prompt = build_prompt(question, tg, services, candidates, window, jaeger_ok)

    diagnosis: Optional[Diagnosis] = None
    llm_error = None
    generated_by = "rules (no LLM configured)"
    if use_llm and llm.provider() != "none":
        stage("reason", "running", f"asking {llm.model_name()}")
        try:
            diagnosis = llm.structured(prompt, Diagnosis, system=SYSTEM_PROMPT)
            generated_by = f"{llm.provider()}:{llm.model_name()}"
            stage("reason", "done", "structured answer received")
        except (llm.NoLLMConfigured, llm.LLMError, KeyError, ValueError) as exc:
            llm_error = str(exc)
            generated_by = "rules (LLM call failed)"
            stage("reason", "error", llm_error[:160])
    if diagnosis is None:
        diagnosis = rule_diagnosis(tg, services, candidates)
        if not llm_error:
            stage("reason", "done", "rule-based answer (no LLM configured)")
    reasoning.append("Reason: " + generated_by)

    report = HealthReport(
        question=question,
        checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        window=window,
        services=services,
        reasoning=reasoning,
        generated_by=generated_by,
        **diagnosis.model_dump(),
    )
    cand_view = [{
        "id": c.node_id, "kind": c.kind, "score": round(c.score, 3),
        "explains": len(c.explains), "total": len(symptoms),
        "paths": [kag_engine._render_path(tg, p) for p in c.paths],
    } for c in candidates]
    return AskResult(report, prompt, cand_view, llm_error, round(time.time() - started, 1))
