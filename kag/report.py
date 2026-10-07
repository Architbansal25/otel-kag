"""The structured answer format.

Every answer the assistant gives -- healthy or on fire, LLM or rule-based --
comes back as a `HealthReport`. That is what makes it demo-able: the same
fields light up the same way every time, and the raw JSON can be shown to the
room to prove the model is filling a contract, not free-writing.

`Diagnosis` is the subset the LLM fills in. The per-service table is measured,
not generated, so the model never gets the chance to invent a number.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["HEALTHY", "DEGRADED", "DOWN"]

Category = Literal[
    "service_down",         # a process is not answering at all
    "high_latency",         # requests succeed, but slowly
    "error_spike",          # requests fail
    "bad_deployment",       # a recent change broke something
    "resource_exhaustion",  # pool / queue / capacity starvation
    "bad_message",          # poison input
    "unknown",
]


class ServiceHealth(BaseModel):
    """Measured state of one service. Filled from probes and traces, never by the LLM."""
    name: str
    status: Status
    reachable: bool = Field(description="Did the health endpoint answer?")
    error_rate_pct: float = Field(description="Failed requests, % of requests observed")
    p95_latency_ms: float
    requests_observed: int
    last_error_at: Optional[str] = None
    detail: str = Field(description="One short line on why the status is what it is")


class Incident(BaseModel):
    title: str = Field(description="One-line headline, e.g. 'inventory-svc is down'")
    affected_services: List[str] = Field(
        description="Services whose users are feeling it, including the root cause service")
    root_cause_component: str = Field(
        description="Exact node id from the graph: a service, pool, config item or deployment")
    root_cause: str = Field(description="One or two sentences: what broke and why")
    category: Category
    started_at: Optional[str] = Field(
        description="Earliest timestamp in the evidence showing the problem, "
                    "copied exactly as given (YYYY-MM-DD HH:MM:SS), or null")
    ended_at: Optional[str] = Field(
        description="When it recovered, copied exactly from the evidence, or null if ongoing")
    ongoing: bool
    causal_chain: List[str] = Field(
        description="Ordered steps from root cause to user-visible symptom")
    evidence: List[str] = Field(description="The specific observations that support this")
    remediation: List[str] = Field(description="Immediate actions, most important first")


class Diagnosis(BaseModel):
    """What the LLM must return."""
    overall_status: Status
    is_anything_breaking: bool
    answer: str = Field(description="Direct 2-3 sentence answer to the question, plain language")
    incident: Optional[Incident] = Field(description="null when nothing is wrong")
    confidence: Literal["high", "medium", "low"]


class HealthReport(BaseModel):
    """What the user gets back."""
    question: str
    checked_at: str
    window: str
    overall_status: Status
    is_anything_breaking: bool
    answer: str
    incident: Optional[Incident] = None
    confidence: Literal["high", "medium", "low"]
    services: List[ServiceHealth]
    reasoning: List[str] = Field(description="What the engine did to get here, step by step")
    generated_by: str
