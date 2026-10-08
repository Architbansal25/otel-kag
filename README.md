# Order Platform: "Is anything breaking?" — OpenTelemetry + Knowledge-Graph RCA

A live demo. Four Spring Boot microservices run, every request is traced with
OpenTelemetry into Jaeger, and you can ask the system in plain English whether
anything is breaking. The answer comes back in a fixed, structured format
(a Pydantic `HealthReport`): what is down, why, and since when.

**Presenting?** [HOW-IT-WORKS.md](HOW-IT-WORKS.md) is the dry run: what happens
at each step, where every number on screen comes from, and answers to the
questions the audience will ask.

```
customer ─► order-api ─► inventory-svc ─► H2
               │              ▲
               └─► broker ─► notification-svc
                 (order.events)

every service ──OTLP──► Jaeger ──► knowledge graph ──► LLM ──► HealthReport (Pydantic)
health checks ───────────────────┘   (facts.yaml, deploys, live probes)
```

## The demo, step by step

| # | On stage | Where |
|---|----------|-------|
| 1 | **Show the app running.** Four green service cards and a live architecture diagram. | Console `http://localhost:8090` |
| 2 | **Open Swagger** and fire a request or two (`POST /orders`, `GET /products/{sku}/availability`). | Each card's *Swagger* link, e.g. `http://localhost:8081/swagger-ui.html` |
| 3 | **Show health:** click *Health* on a card (`/actuator/health`, with db / jms details). | `http://localhost:8081/actuator/health` |
| 4 | **Show Jaeger:** click *Place order* in the console, then *open trace in Jaeger* to see one request cross three services. Click a span and open **Logs** to see the log lines that request wrote. | `http://localhost:16686` |
| 5 | Click **Start steady traffic** so there is always something to observe. | Console, section 2 |
| 6 | **Ask: "Is anything breaking?"** The answer: `HEALTHY`, nothing breaking, every service measured. | Console, section 3 |
| 7 | **Break something** (open *Presenter controls* at the bottom). Either inject a delay or errors into inventory-svc's data fetching, or stop a service. | Console, section 4 |
| 8 | **Show the haystack:** click **Show raw logs**. Tens of thousands of lines from four services, stack traces, broker audit noise. *"Would you find the cause in here?"* | Console, section 3 |
| 9 | Wait ~20s, then **ask again.** The answer is `DEGRADED`/`DOWN`, with the root-cause service, the time it started, how it spread, evidence, and what to do. | Console, section 3 |
| 10 | Expand **"Structured answer — the Pydantic HealthReport JSON"** to show it is a typed contract, not free text. | Console |
| 11 | Optional: open the failing request in Jaeger from a `trace=` link in the raw logs; its span **Logs** show the same error the assistant quoted. | Jaeger |
| 12 | **Heal everything**, wait, ask *"Is it fixed?"* | Console |

What each fault looks like to the assistant:

| Fault (presenter control) | What the audience sees | What the assistant concludes |
|---|---|---|
| Inject delay 2500 ms | Orders fail with 504 after 2s | `inventory-svc` is slow (`high_latency`); order-api times out on it |
| Inject errors 0.5 | Half the orders fail with 502 | `inventory-svc` is failing (`error_spike`) |
| Stop `inventory-svc` | Orders fail with 503 | `inventory-svc` is down (`service_down`), since the monitor saw it go DOWN |
| Stop `broker` | Orders fail with 503 `broker_unavailable` | `broker` is down; order-api / notification-svc report jms DOWN |
| Stop `notification-svc` | Nothing visible to customers | `notification-svc` is down; order events pile up |

The delay and error injections are hidden from Swagger and leave no log line or
change record. The only way to find the cause is to reason from the telemetry.

## Logs: in Jaeger and in the console

Every service writes its log lines in this format, with the request's trace id:

```
2026-10-07 19:49:35.221  INFO [order-api] [trace=4c812c2e…] [nio-8081-exec-4] c.n.d.order.OrderController : Order b88f… placed for sku SKU-1001
```

- **In Jaeger:** each log line written while handling a request is also attached
  to that request's span (the shared `services/log-to-trace` module does this).
  Open a trace, click a span, expand **Logs**. A failing span shows the ERROR line
  and the exception with its stack trace.
- **From a log line to its trace:** copy the `trace=` id into Jaeger's *Lookup by
  Trace ID* box, or click it in the console's raw-logs panel. One order has the
  same trace id in order-api and notification-svc, across the queue.
- **All of it at once:** the console's **Show raw logs** merges the last 5 minutes
  of all four services (`./ops/demo.sh logs-json` underneath), with counts of
  lines, errors and warnings.
- **One service, live:** `./ops/demo.sh logs inventory-svc`

## Running it

### Windows (WSL2), the original setup

```
START-DEMO.cmd      starts Jaeger + services in WSL, the console on Windows, opens the browser
STOP-DEMO.cmd       stops everything
```

The services run inside WSL because endpoint security on the presentation
laptop stops the Windows JVM from opening sockets. WSL2 forwards the ports, so
everything is still on `localhost`.

### Linux / macOS

```bash
./ops/demo.sh deps            # once: download Jaeger
./ops/demo.sh build           # only if you changed Java code (jars are committed)
./ops/demo.sh start           # Jaeger + 4 services
cd kag && pip install -r requirements.txt && python3 ui.py   # console on :8090
```

### The LLM (optional)

Without a key the assistant answers from rules, in the same `HealthReport`
format, so the demo never breaks. With a key, the LLM reasons over the retrieved
subgraph and fills the schema. Pick **one**:

**Groq** (one variable is enough; the model defaults to `openai/gpt-oss-120b`,
Groq's successor to the retired `llama-3.3-70b-versatile`):

```
setx GROQ_API_KEY "gsk_..."
```

If the configured model has been retired or is not enabled for your key, the
assistant asks Groq which models the key can use and switches to the best one,
printing which it picked.

**Anthropic** (default model `claude-opus-5-5`):

```
setx ANTHROPIC_API_KEY "sk-ant-..."
setx KAG_EFFORT "low"                            # optional: faster answers on stage
```

**Any other OpenAI-compatible endpoint** (OpenAI, Azure, a gateway):

```
setx LLM_BASE_URL "https://..."
setx LLM_API_KEY  "..."
setx KAG_MODEL    "model-name"
```

Open a **new** terminal after `setx`, then run the pre-flight check:

```
cd kag
py llm.py          # provider, model, the models your key can use, and a test call
```

The console window prints the provider and model it picked
(`LLM: groq / openai/gpt-oss-120b`), and so does the header badge. If the LLM ever
contradicts the measured health (says healthy while a service is down), its answer
is discarded and the rule-based one is shown, with the reason.

How the choice is made: a Groq key (`gsk_...`) is recognised in any key variable,
even `ANTHROPIC_API_KEY`. Anthropic wins when it has a real key, unless `KAG_MODEL`
names a non-Claude model and an OpenAI-compatible key is also set.
`KAG_PROVIDER=groq|openai|anthropic` forces it. `KAG_MODEL` only overrides the model
within the chosen provider; a Claude name is never sent to Groq, and a Groq name is
never sent to Anthropic.

On Anthropic the answer is constrained by structured outputs. On Groq and other
OpenAI-compatible providers it uses JSON mode, is validated with Pydantic, gets one
corrective retry, and waits briefly and retries once on a rate limit (HTTP 429).

### From the terminal

```bash
cd kag
python3 ask.py "is anything breaking?"       # pretty answer
python3 ask.py --json                         # the raw HealthReport
python3 ask.py --show-prompt                  # exactly what the LLM saw

../ops/demo.sh chaos latency 2500             # or: chaos errors 0.5 | chaos clear
../ops/demo.sh kill inventory-svc             # or: restart <svc> | heal
../ops/demo.sh load 30 4                      # 30s of traffic, 4 workers
```

## How the answer is produced

`kag/assistant.py` runs four stages; the console shows each one as it happens.

1. **Observe.** Health-check every service (a crashed process emits no traces,
   so this is the only way to see it). Read the last 5 minutes of traces from
   Jaeger. Probe the connection pool and queue. Load the change log and the
   health monitor's DOWN/UP history.
2. **Seed.** Turn what hurts into symptoms: a service down, a request error rate
   ≥ 5%, an edge p95 ≥ 1s, or queue lag.
3. **Rank.** Walk the knowledge graph backwards from the symptoms. Score each
   candidate cause by whether it explains *all* the symptoms and whether every
   hop on the way is corroborated. Components that live probes show healthy are
   ruled out.
4. **Reason.** The LLM gets the measured service table, a timeline, and the
   ranked subgraph, and fills the `Diagnosis` schema.

The per-service table in the answer is measured, never generated by the LLM.

### The answer format (`kag/report.py`)

```python
class HealthReport(BaseModel):
    question: str
    checked_at: str
    window: str
    overall_status: Literal["HEALTHY", "DEGRADED", "DOWN"]
    is_anything_breaking: bool
    answer: str                       # 2-3 plain sentences
    incident: Optional[Incident]      # null when healthy
    confidence: Literal["high", "medium", "low"]
    services: List[ServiceHealth]     # measured
    reasoning: List[str]
    generated_by: str                 # "anthropic:claude-opus-5-5" or "rules (...)"

class Incident(BaseModel):
    title: str
    affected_services: List[str]
    root_cause_component: str         # exact graph node id
    root_cause: str
    category: Literal["service_down", "high_latency", "error_spike",
                      "bad_deployment", "resource_exhaustion", "bad_message", "unknown"]
    started_at: Optional[str]
    ended_at: Optional[str]
    ongoing: bool
    causal_chain: List[str]
    evidence: List[str]
    remediation: List[str]
```

## Files

| Path | What it is |
|------|------------|
| `services/` | The four Spring Boot services (Swagger via springdoc, actuator health with details). `inventory-svc` has hidden `/admin/chaos` fault injection. |
| `ops/demo.sh` | Start/stop, traffic, faults (`chaos`, `kill`, `restart`, `heal`), plus the original scenarios. |
| `kag/ui.py`, `kag/ui_index.html` | The demo console. |
| `kag/assistant.py`, `kag/ask.py` | The question-answering pipeline and its CLI. |
| `kag/report.py` | The Pydantic answer format. |
| `kag/graph.py`, `kag/kag_engine.py`, `kag/facts.yaml` | The knowledge graph and the ranking. |
| `kag/monitor.py` | Records exactly when services go DOWN / UP. |
| `kag/llm.py` | Provider-agnostic LLM client (`complete()`, `structured()`). |
| `kag/rca.py`, `kag/rag_baseline.py`, `/classic` | The original flat-RAG vs KAG comparison, still available. |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| A card stays red after *Heal* | `./ops/demo.sh logs <svc>`; ports 8081-8084 must be free |
| The answer says healthy right after a fault | Traffic must be flowing: click *Start steady traffic* and wait ~20s |
| An old incident shows up in a new answer | The trace window is 5 minutes. Faults are separated by the monitor's last recovery, but waiting a few minutes between runs gives the cleanest story |
| `generated_by: rules (LLM call failed)` | The LLM error is shown under the answer; check the key / model name |
| `Anthropic returned 404 ... model: llama-...` | Old versions sent the Groq setup to Anthropic. Update, and keep the Groq key in `GROQ_API_KEY` |
| `The model llama-3.3-70b-versatile does not exist or you do not have access to it` | Groq retired it in August 2026. Update (the assistant now switches models by itself), or `reg delete HKCU\Environment /v KAG_MODEL /f` to use the default |
| `LLM returned 413` / `429` from Groq | Free-tier token limits: wait a minute, or set `KAG_MODEL` to a smaller model such as `openai/gpt-oss-20b` |
| Every answer reads the same | That is the rule-based fallback (no working LLM). Fix the LLM and answers follow the question |
| Swagger shows a 500 | The response body now carries the reason; the `x-trace-id` header opens the trace at `http://localhost:16686/trace/<id>`. "Stock lookup failed: storage read error" means *Inject errors* is on: *Clear* it |
| Asked right after breaking something, answer still healthy | Spans reach Jaeger within ~1s, but give traffic 10-20s to show a pattern |
| No traces in Jaeger | `./ops/demo.sh status`; Jaeger must be up before the services start |
