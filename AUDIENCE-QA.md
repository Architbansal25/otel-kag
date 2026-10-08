# Audience Q&A: managers and DevOps engineers

Likely questions after the demo, with answers you can give as-is. The answers
are honest about what this is: a working proof of concept, not a product. Saying
so up front buys more credibility than any feature.

- [Part 1: Questions from managers](#part-1-questions-from-managers)
- [Part 2: Questions from DevOps / SRE engineers](#part-2-questions-from-devops--sre-engineers)
- [Part 3: Tough questions, honest answers](#part-3-tough-questions-honest-answers)

For how the demo works and where each number comes from, see [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

---

## Part 1: Questions from managers

### What problem does this solve?

The slowest part of an incident is usually not the fix, it is finding *what* broke.
Someone gets paged, opens five dashboards and four log streams, and tries to work out
which of many red signals is the cause and which are side effects. This answers
"what is broken, why, and since when" in a few seconds, with the evidence attached,
so the engineer starts from a hypothesis instead of a blank screen.

### Does it replace our on-call engineers?

No. It is a triage assistant. It points at the most likely cause and shows its
evidence; a person decides and acts. It deliberately does not restart or roll back
anything on its own.

### How accurate is it? Can it be wrong?

Yes, it can be wrong, and the design assumes that:

- The numbers (which services are up, error rates, latency) are **measured**, not
  written by the AI. The AI cannot change them.
- The AI picks the root cause from a **ranked shortlist** built from the system's
  structure, and must quote evidence.
- If the AI's answer contradicts the measurements (says "healthy" while a service is
  down), it is thrown out and a rule-based answer is shown instead.
- Every answer shows a confidence level and exactly what the AI was given.

In the demo scenarios (service stopped, slow database access, injected errors,
broker down, a bad configuration change) it names the right component and start
time. That is a controlled environment; accuracy on a real system has to be measured
in a pilot (see "How would we measure success?").

### How fast is it?

About 1 second to collect and rank the evidence, plus 2-10 seconds for the AI to
write the answer, depending on the model.

### What does it cost to run?

- **AI:** each question sends roughly 2,000-4,000 tokens. On Claude Opus 5.5
  ($4 per million input tokens, $20 per million output) that is a few cents per
  question. Groq's free tier was enough for the demo; check Groq's current pricing
  for paid use. A question is asked during an incident, not continuously, so volume
  is low.
- **Infrastructure:** OpenTelemetry is free and open source. The trace store (Jaeger
  here) needs storage that grows with traffic and retention. That is the real cost
  at scale, and it is the same cost any tracing setup has.

### Is our data sent to an external AI provider?

Only a summary, never raw logs or request contents. The AI receives service names,
error counts, timings, timestamps, the ranked causes, and short error messages from
failed requests (for example "Connection refused"). You can read the exact text under
"What the LLM was given".

Two cautions: error messages can contain data if developers put it there, and for
regulated environments you can point it at a model you host yourself or one under an
enterprise agreement (any OpenAI-compatible endpoint works via `LLM_BASE_URL`).

### Are we locked into a vendor?

No, at each layer:

- **Instrumentation:** OpenTelemetry is the vendor-neutral industry standard.
- **Trace store:** Jaeger is open source; reading traces is one small function that
  can be adapted to another backend.
- **AI:** works with Anthropic Claude, Groq, OpenAI, Azure, or a self-hosted model.
  The demo already survived one model being retired by its provider: it switched to
  the successor automatically.

### What would it take to use this on our systems?

Four ingredients, most of which teams often have already:

1. **OpenTelemetry tracing** on the services. For Java, attaching an agent at startup;
   no code changes. Other languages have agents or SDKs.
2. **A trace backend** (Jaeger, or another one with an adapter).
3. **Health endpoints** on each service (Spring Boot actuator, Kubernetes probes).
4. **A description of the topology**: what calls what, what runs where, which
   database pool belongs to which service. Here it is a hand-written `facts.yaml`;
   in production it should be generated from your CMDB, Helm charts or Terraform.

### What is demo-only, and what is production-ready?

| Demo-only (would need work) | Reusable as-is |
|---|---|
| Hand-written topology file | The ranking approach (graph + scoring) |
| Jaeger keeps traces in memory; restart loses them | The structured answer format and its validation |
| One machine, four services | The guardrails (measured vs generated, contradiction check) |
| No login on the console; fault-injection switches are open | Provider-agnostic AI client |
| Thresholds tuned for this demo | OpenTelemetry instrumentation |

### How would we measure success in a pilot?

Run it alongside the normal on-call process for a few weeks, on real incidents:

- How often was the top-ranked cause the real one?
- How long until the on-call engineer had the right hypothesis, with and without it?
- Mean time to recovery (MTTR) before and during the pilot.
- How often did engineers find the answer misleading?

### Can it alert us, or open a ticket automatically?

Not yet. Today you ask it. Every answer is structured JSON, so it is a small step to
call it from an alert (for example an Alertmanager webhook) and attach the answer to
the incident ticket or Slack channel. Automatic remediation is deliberately left out.

### What if the AI provider is down or rate-limits us?

It waits briefly and retries once on a rate limit. If the AI still fails, a
rule-based answer in the same format is shown, with the AI's error underneath. The
console never shows an empty result.

---

## Part 2: Questions from DevOps / SRE engineers

### How are traces collected?

The OpenTelemetry Java agent (`vendor/opentelemetry-javaagent.jar`) is attached with
`-javaagent` at startup. It auto-instruments Spring MVC, the HTTP client, JMS (Artemis)
and JDBC, and exports spans over OTLP/gRPC to Jaeger on `:4317`, batched every second
(`otel.bsp.schedule.delay=1000`). Sampling is `always_on` for the demo. Context
propagates over HTTP headers and JMS message properties, so one order is one trace
across all three services.

### Did you change application code?

Three small, optional additions: a Logback appender that copies log lines into the
current span (`services/log-to-trace`), a filter that returns `X-Trace-Id`, and the
demo's chaos switch. Tracing itself needed no code.

### How do the logs get into Jaeger?

`SpanEventAppender` runs on every log call. If a span is active, it adds the line as a
span event with `log.severity`, `log.logger`, `log.message` and exception details. The
trace id also goes into each log line via the agent's MDC (`trace=…` in the log format).
In production you would ship logs to a log backend (Loki, Elastic, OpenSearch) over
OTLP and correlate by `trace_id`, rather than store them in spans.

### How is "down" different from "slow"?

- **Down:** the health monitor polls `/actuator/health` every 2 s with a 2 s timeout.
  Connection refused or no answer means DOWN, and the exact transition time is
  recorded in `kag/health_events.json`.
- **Slow:** p95 of *entry spans* (server and consumer spans, not DB or client spans)
  at or above 1 s, with at least 8 requests to judge from.
- A subtlety the demo shows: when inventory-svc's DB pool is saturated, its own health
  check (which needs a DB connection) times out, so a very slow service can also
  read as DOWN (no response).

### How is the error rate calculated?

Per **request (trace)**, not per span: one failed request emits several error spans,
and counting spans skews the rate. Health checks, admin polling, Swagger and
startup SQL are excluded because they are not user traffic. The engine looks at
both the 5-minute window and the last 60 seconds and uses the worse, so a fresh
incident is not averaged away by healthy history. A symptom is raised at ≥ 5% failed
requests, or when one endpoint fails ≥ 50% of its calls (at least 2 failures).

### How does it rank the root cause?

It builds a directed graph where an edge means "if A breaks, B can break" (calls,
pool usage, queue hosting, shared host, config changes). From each symptom it walks
backwards up to 6 hops and scores each candidate:

| Factor | Weight |
|---|---|
| Explains all the symptoms (blast fit) | 0.35 |
| Every hop on the path is independently corroborated | 0.30 |
| Changed recently (20-minute half-life) | 0.20 |
| Is itself visibly broken | 0.15 |
| Distance | −0.05 |

Two refinements matter in practice:

- **Error origin:** for each failed trace, it finds where the failure *started*: the
  deepest failing span that is not an outbound call. A service whose own code or
  database throws outranks its neighbours.
- **Exoneration:** components that live probes show healthy are ruled out. A host
  stays clear while another service on it is fine; the pool stays clear while no thread
  waits for a connection.

### What exactly does the LLM receive?

The question, the measured per-service table, a timeline (first and last failures,
health transitions, deployments) and the top 5 ranked candidates with paths and
evidence. Nothing else. See it under "What the LLM was given" in the console, or run
`py ask.py --show-prompt`.

### How is the structured output enforced?

- **Anthropic:** structured outputs (`output_config.format` with a JSON schema
  generated from the Pydantic model). The response is guaranteed to match.
- **Groq / OpenAI-compatible:** JSON mode, then Pydantic validation; on failure the
  validation error is sent back for one corrected attempt. Small wording slips
  (`"Latency"` for `high_latency`) are normalised.
- **After parsing:** the overall status is replaced with the measured one, and an
  answer that contradicts measured health is discarded.

### What happens when a model is retired?

If the provider answers "model does not exist", the client lists the models the key
can use (`/models`), picks the best match from a preference list, retries, and says
which model it switched to. `py llm.py` is a pre-flight check that shows the provider,
model, available models and a test call.

### Does it handle sampling?

Not yet, and it matters. Error rates assume every request is traced. With head
sampling at, say, 10%, rates stay roughly right but small incidents may be missed. The
usual fix is tail sampling in an OpenTelemetry Collector (keep all error and slow
traces, sample the rest), or computing rates from metrics and using traces only for
the causal path.

### What about a service that is not in `facts.yaml`?

It is ignored by the graph: traces from unknown services are not turned into nodes.
That is why the topology should be generated from a source of truth, not maintained
by hand.

### Would it work on Kubernetes?

The ideas map directly (health from readiness probes, hosts from nodes, topology from
the Kubernetes API and Helm values, deploys from the rollout history), but none of that
is implemented. This demo runs plain JVMs in WSL.

### What about two incidents at once?

The ranking scores every candidate against all symptoms, and the LLM sees the top 5,
but the answer reports one primary incident. Two unrelated simultaneous failures would
be described as one, with the second visible in the services table.

### What are the overheads?

The OpenTelemetry Java agent typically adds a few percent CPU and a small amount of
latency per request; measure it on your workload. Asking a question costs a few Jaeger
API calls (up to 500 traces per service) and one LLM call. The health monitor sends
four tiny HTTP requests every 2 s.

### Can I use it from scripts or CI?

`py ask.py --json` prints the full `HealthReport`. It currently always exits 0; an exit
code based on `overall_status` would be a one-line change for use in pipelines or
smoke tests.

### Where is state stored, and what survives a restart?

Traces live in Jaeger's memory (all-in-one default) and are lost on restart; production
Jaeger uses Elasticsearch, OpenSearch, Cassandra or Badger. The health monitor's
transitions are in `kag/health_events.json`. The change log is `kag/deploys.json`.
Service logs are files in `~/otel-kag-demo/logs` inside WSL.

### Is it secure to run?

As a demo on a laptop, yes: the console listens on `127.0.0.1` only. The services and
their `/admin/chaos` endpoints have no authentication and listen on all interfaces of
the WSL VM. Never expose them on a shared network.

### How would I add a new kind of failure (disk full, CPU, certificate expiry)?

Add a probe that sets an attribute on the right node and, past a threshold, creates a
Symptom node with evidence (`kag/graph.py`, next to `probe_health`). If a new kind of
component is involved, add it to `facts.yaml` with its relationships. The ranking and
the LLM step need no change.

---

## Part 3: Tough questions, honest answers

**"Isn't this just ChatGPT reading your logs?"**
No, and the `/classic` page shows the difference side by side. Pasting logs into a model
finds the loudest error. Here the model never sees the raw logs: it gets a ranked,
evidence-backed shortlist built from the system's structure, and the numbers it reports
are measured, not generated.

**"Did you tune it until the demo worked?"**
Partly, yes. Thresholds (5% errors, 1 s latency, 2 s health timeout) are set for this
system, and the faults are clean, single causes. Real incidents are messier. The
mechanisms (graph, origin detection, exoneration, guardrails) are general; the numbers
would need tuning on your data.

**"What if the AI confidently names the wrong service?"**
Three layers limit the damage: it can only choose from ranked candidates, its status
must agree with the measurements, and the evidence and full prompt are shown so an
engineer can check it in seconds. It is a starting hypothesis, not a verdict.

**"Why should we trust the start time?"**
It is copied, not estimated: either the second the health monitor saw the service stop
answering, or the time of the first failed request in the traces. Both are on screen as
evidence.

**"Why not just buy an AIOps product?"**
Commercial observability platforms do topology-aware root-cause analysis too, and at much
larger scale. This shows the same idea built from open, inspectable parts:
OpenTelemetry, a graph, and an LLM you choose. It is useful for understanding what to ask
of a vendor, or as a base if you want to own the logic.

**"Is it production-ready?"**
No. It is a proof of concept that runs end to end on real telemetry. The path to production
is mostly plumbing: generated topology, durable trace storage, sampling strategy,
authentication, and alert integration. The table under "What is demo-only?" lists it.
