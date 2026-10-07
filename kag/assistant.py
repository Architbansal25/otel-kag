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
                                     error_rate_pct=round(100 * (a.get("window_error_rate") or 0), 1),
                                     p95_latency_ms=a.get("p95_ms") or 0.0,
                                     requests_observed=a.get("request_count") or 0,
                                     last_error_at=a.get("last_error_at"), detail=detail))
            continue

        if (a.get("recent_requests") or 0) >= RECENT_MIN_REQUESTS:
            err, p95, basis = a.get("recent_error_rate") or 0.0, a.get("recent_p95_ms") or 0.0, "last minute"
            sample = a.get("recent_requests") or 0
        else:
            err, p95, basis = a.get("window_error_rate") or 0.0, a.get("p95_ms") or 0.0, "last " + window
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
        for ep in a.get("failing_endpoints") or []:
            if ep["still_failing"]:
                reasons.append(f"{ep['endpoint']} failing ({ep['failed']}/{ep['total']} calls"
                               + (f", {ep['codes']}" if ep["codes"] else "") + ")")
            else:
                history = (f"recovered; {ep['endpoint']} failed {ep['failed']} times, "
                           f"last at {ep['last_error_at']}")
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
        if a.get("error_episode_at") and a.get("error_episode_at") != a.get("first_error_at"):
            rows.append((a["error_episode_at"], name + ": CURRENT run of failures began "
                         "(earlier failures belong to a previous episode)"))
        if a.get("slow_episode_at") and a.get("slow_episode_at") != a.get("first_slow_at"):
            rows.append((a["slow_episode_at"], name + ": CURRENT slow period began"))
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
                         f"{100 * (a.get('window_error_rate') or 0):.0f}% failed, p95 {a.get('p95_ms')} ms")
        if a.get("recent_requests"):
            extra.append(f"last minute: {a['recent_requests']} requests, "
                         f"{100 * (a.get('recent_error_rate') or 0):.0f}% failed, "
                         f"p95 {a.get('recent_p95_ms')} ms")
        if a.get("error_codes"):
            extra.append("error codes " + ", ".join(f"{k} x{v}" for k, v in a["error_codes"].items()))
        for ep in a.get("failing_endpoints") or []:
            extra.append(f"endpoint {ep['endpoint']}: {ep['failed']}/{ep['total']} calls failed"
                         + (f" ({ep['codes']})" if ep["codes"] else "")
                         + f", first {ep['first_error_at']}, last {ep['last_error_at']}, "
                         + ("most recent call FAILED" if ep["still_failing"] else "most recent call succeeded"))
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


def focus(question: str, services: List[ServiceHealth]) -> List[ServiceHealth]:
    """Services the question names, loosely: 'inventory service' -> inventory-svc."""
    q = question.lower()
    return [s for s in services if s.name.split("-")[0] in q]


def _focus_sentence(question: str, services: List[ServiceHealth]) -> str:
    parts = []
    for s in focus(question, services):
        parts.append(f"{s.name} is {s.status}: {s.detail}.")
    return (" " + " ".join(parts)) if parts else ""


def rule_diagnosis(tg: graph_mod.TelemetryGraph, services: List[ServiceHealth],
                   candidates: List[kag_engine.Candidate], question: str = "") -> Diagnosis:
    diagnosis = _rule_diagnosis(tg, services, candidates)
    # Without an LLM the wording is fixed, so at least speak to what was asked about.
    extra = _focus_sentence(question, services)
    if extra and extra.strip() not in diagnosis.answer:
        diagnosis.answer += extra
    return diagnosis


def _slow_now(a: dict) -> bool:
    """Slow right now. Needs enough recent requests to judge; with only a few,
    they must agree with the window; with none, the window decides."""
    n = a.get("recent_requests") or 0
    recent_slow = (a.get("recent_p95_ms") or 0) >= graph_mod.SLOW_SERVICE_P95_MS
    if n >= graph_mod.MIN_REQUESTS_FOR_LATENCY:
        return recent_slow
    if n >= RECENT_MIN_REQUESTS:
        return recent_slow and bool(a.get("slow"))
    return bool(a.get("slow"))


def _failing_now(a: dict) -> bool:
    if any(e["still_failing"] for e in a.get("failing_endpoints") or []):
        return True
    return ((a.get("recent_requests") or 0) >= RECENT_MIN_REQUESTS
            and (a.get("recent_error_rate") or 0) >= graph_mod.ERROR_RATE_SYMPTOM_THRESHOLD)


def _top_message(tg: graph_mod.TelemetryGraph, node: str) -> str:
    """The most informative error message recorded for a node's symptoms."""
    for sym in tg.g.successors(node):
        if tg.g.nodes[sym].get("kind") != "Symptom":
            continue
        for ev in tg.g.nodes[sym].get("evidence") or []:
            if ev.kind == "log" and len(ev.text) > 15 and "." not in ev.text.split(" ")[0]:
                return ev.text
    return ""


def _history_diagnosis(tg: graph_mod.TelemetryGraph, services: List[ServiceHealth],
                       status: str) -> Optional[Diagnosis]:
    """Healthy now: what was the most recent trouble, and when did it stop?

    Ranking is built for live incidents; once everything has recovered, the
    trustworthy record is the health monitor (went DOWN, came back UP) and the
    trace timestamps of the last failures. Whichever is more recent wins.
    """
    stories = []
    events = [e for e in graph_mod.load_health_events() if e.get("epoch", 0) >= time.time() - 900]
    for i, ev in enumerate(events):
        if ev.get("to") != "UP":
            continue
        down = next((d for d in reversed(events[:i])
                     if d["service"] == ev["service"] and d.get("to") in ("DOWN", "DEGRADED")), None)
        if down:
            stories.append((ev["at"], Incident(
                title=f"{ev['service']} was {down['to'].lower()}", affected_services=[ev["service"]],
                root_cause_component=ev["service"],
                root_cause=f"{ev['service']} stopped answering health checks"
                           + (f" ({down['detail']})" if down.get("detail") else "")
                           + f" at {down['at']} and was back at {ev['at']}.",
                category="service_down", started_at=down["at"], ended_at=ev["at"], ongoing=False,
                causal_chain=[], evidence=[f"health monitor: {down['at']} {down['from']} -> {down['to']}",
                                           f"health monitor: {ev['at']} {ev['from']} -> UP"],
                remediation=["Nothing urgent: it is back", "Check its logs for why it went down"])))
    hit = [s for s in services if s.last_error_at]
    if hit:
        last = max(hit, key=lambda s: s.last_error_at)
        a = tg.g.nodes[last.name]
        began = a.get("error_episode_at") or a.get("first_error_at")
        slow = [n for n in tg.nodes_of("Service") if tg.g.nodes[n].get("slow")]
        cause = f"{', '.join(slow)} was slow at the time" if slow else "cause not determined"
        stories.append((last.last_error_at, Incident(
            title=f"{last.name} was failing requests", affected_services=[last.name],
            root_cause_component=slow[0] if slow else last.name,
            root_cause=f"{last.name} failed requests from {began} until {last.last_error_at}; {cause}.",
            category="high_latency" if slow else "error_spike",
            started_at=began, ended_at=last.last_error_at, ongoing=False, causal_chain=[],
            evidence=[ev.text for ev in (a.get("evidence") or [])[:4]],
            remediation=["Nothing urgent: requests are succeeding again",
                         "Review what changed around " + str(began)])))
    if not stories:
        return None
    _, incident = max(stories, key=lambda x: x[0])
    return Diagnosis(
        overall_status=status, is_anything_breaking=False, confidence="medium", incident=incident,
        answer=("It is running fine now: every service passes its health check and requests are "
                f"succeeding. The most recent problem: {incident.root_cause}"))


def _rule_diagnosis(tg: graph_mod.TelemetryGraph, services: List[ServiceHealth],
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
    if not ongoing:
        history = _history_diagnosis(tg, services, status)
        if history:
            return history
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
    elif kind == "Service" and _failing_now(a):
        category = "error_spike"
        eps = [e for e in a.get("failing_endpoints") or [] if e["still_failing"]]
        msg = _top_message(tg, top.node_id)
        if eps and a.get("healthy_endpoints"):
            ep = eps[0]
            title = f"{top.node_id} {ep['endpoint']} is failing"
            cause = (f"{top.node_id} is up, but {ep['endpoint']} failed {ep['failed']} of its "
                     f"{ep['total']} calls" + (f" ({ep['codes']})" if ep["codes"] else "")
                     + "; its other endpoints (" + ", ".join(a["healthy_endpoints"][:3])
                     + ") are working.")
        else:
            title = top.node_id + " is failing requests"
            where = (" on " + ", ".join(e["endpoint"] for e in eps)) if eps else ""
            cause = (f"{top.node_id} is up but failing {100 * (a.get('recent_error_rate') or 0):.0f}% "
                     f"of its requests in the last minute{where}; the failures start inside it, "
                     "not in anything it calls.")
        if msg:
            cause += f' Error: "{msg}".'
    elif kind == "Service" and _slow_now(a):
        category, title = "high_latency", top.node_id + " is slow"
        cause = (top.node_id + " is responding slowly (p95 "
                 + str(a.get("recent_p95_ms") or a.get("p95_ms")) + " ms), so its callers time out or slow down.")
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
    if category == "bad_deployment":
        deploys = [top.node_id] if kind == "Deployment" else [
            n for n in tg.g.successors(top.node_id) if tg.g.nodes[n].get("kind") == "Deployment"]
        stamps = [str(tg.g.nodes[d].get("at")).replace("T", " ") for d in deploys
                  if tg.g.nodes[d].get("at")]
        recovered = ""  # the change itself is the start, however much flapping followed
        starts = [max(stamps)] if stamps else []
    elif category == "service_down":
        starts = [a.get("down_since") or a.get("last_seen_at")]
    elif category == "high_latency":
        starts = [a.get("slow_episode_at")]
    else:
        starts = [a.get("error_episode_at")]
    if not any(starts):
        # Fall back to when the people calling it started to hurt.
        starts = [tg.g.nodes[p].get("error_episode_at")
                  for sym in top.explains for p in tg.g.predecessors(sym)]
    starts = [x for x in starts if x]
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
        diagnosis = rule_diagnosis(tg, services, candidates, question)
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
