# OTel Demo → Jaeger → OpenSPG KAG Bridge

A proof-of-concept that pipes error traces from the OpenTelemetry Demo into an
OpenSPG **KAG** (Knowledge Augmented Generation) reasoning engine for automated
root-cause analysis.

```
microservices → otel-collector → Jaeger (traces) / Prometheus (metrics)
                                        │
                                        ▼
                          jaeger_kag_bridge.py → KAG (LLM reasoning)
```

## Contents

| File | Purpose |
|------|---------|
| [docker-compose.yml](docker-compose.yml) | OTel Demo services + Jaeger + Prometheus + KAG on a shared `telemetry-net` |
| [otel-collector-config.yaml](otel-collector-config.yaml) | Collector pipeline: traces → Jaeger, metrics → Prometheus |
| [prometheus.yml](prometheus.yml) | Prometheus scrape config |
| [jaeger_kag_bridge.py](jaeger_kag_bridge.py) | Fetches error traces, builds context, queries KAG |
| [requirements.txt](requirements.txt) | Python dependencies |

## Prerequisites

- **Docker Desktop** (with `docker compose`)
- **Python 3.9+**
- A **Groq API key** (optional — only needed for the LLM reasoning step)

---

## Step 1 — Start the telemetry + KAG stack

From the project folder:

```powershell
docker compose up -d
```

This launches the microservices, OTel Collector, Jaeger, Prometheus, and the
`kag-engine` container, all on the `telemetry-net` network.

Verify the backends are up:

| Service | URL |
|---------|-----|
| Jaeger UI | http://localhost:16686 |
| Prometheus | http://localhost:9090 |
| Frontend | http://localhost:8080 |
| KAG | http://localhost:8888 |

Check container status:

```powershell
docker compose ps
```

## Step 2 — Generate some error traces

Interact with the frontend (http://localhost:8080) so the services emit spans.
The demo naturally produces some failing / HTTP 500 traces that Jaeger records.

Confirm traces exist in Jaeger by selecting `frontend` or `checkoutservice` in
the UI service dropdown and searching.

## Step 3 — Set up the Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> The bridge runs steps 1–3 (fetch → parse → format context) **without** the
> KAG SDK or any LLM. Those pieces are only needed for the reasoning step.

## Step 4 — (Optional) Configure the Groq LLM

The KAG reasoning step needs an LLM. To use Groq, set your key as an
environment variable:

```powershell
setx GROQ_API_KEY "gsk_your_key_here"
```

> Open a **new** terminal after `setx` so the variable is picked up.

Then point KAG's `chat_llm` at Groq's OpenAI-compatible endpoint. See the
**"REFERENCE ONLY — Groq LLM configuration"** comment block inside
[jaeger_kag_bridge.py](jaeger_kag_bridge.py) for the exact `kag_config.yaml`
snippet.

> Note: Groq serves chat models only — configure a separate embedding provider
> for KAG's vector store.

## Step 5 — Run the bridge

```powershell
python jaeger_kag_bridge.py
```

Expected output:

1. A **Jaeger Error Trace Context** block — trace IDs, failing services, span
   parent-child relationships, and error logs.
2. A **KAG Root-Cause Analysis** section.
   - If KAG + an LLM are configured, this is the model's analysis.
   - If not, the script gracefully prints the *grounded prompt* that would be
     sent to the LLM (so you can still demo the full data flow).

## Step 6 — Shut down

```powershell
docker compose down
```

Add `-v` to also remove the persisted KAG volume:

```powershell
docker compose down -v
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Cannot connect to Jaeger` | Ensure `docker compose ps` shows `jaeger` healthy; check port 16686 |
| `No error traces collected` | Generate traffic on the frontend first; widen `LOOKBACK` in the script |
| `OpenSPG KAG SDK not available` | `pip install openspg-kag`, or run without it to see the grounded prompt |
| KAG returns nothing | Verify `GROQ_API_KEY` is set and the `chat_llm` config is correct |
