"""Demo console: a local web UI for the OTel + KAG root cause demo.

Runs on Windows (Python can bind sockets here even though the JVM cannot) and
talks to the services running inside WSL over forwarded localhost ports. Also
runs as-is on Linux / macOS. It is the single control surface for the session.

    py ui.py            then open http://localhost:8090

    /           the story: the live app, try it, ask "is anything breaking?",
                and (folded away) the presenter's fault-injection controls
    /classic    the original flat-RAG vs KAG side-by-side race

Two deliberate design choices:

  * Background thread + polling, not SSE. Werkzeug can buffer streamed responses
    in ways that are fine on a laptop and embarrassing on stage; a 250ms poll is
    invisible to an audience and cannot surprise you.

  * The baseline and KAG analyses run CONCURRENTLY in separate threads, so both
    panels fill at the same time. Watching them race is the argument.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

import assistant
import graph as graph_mod
import kag_engine
import llm
import monitor
import rag_baseline

HERE = Path(__file__).resolve().parent
ON_WINDOWS = os.name == "nt"
WSL_DISTRO = os.environ.get("WSL_DISTRO", "Ubuntu-22.04")


def _default_demo_sh() -> str:
    script = HERE.parent / "ops" / "demo.sh"
    if not ON_WINDOWS:
        return str(script)
    # C:\Project\x\ops\demo.sh -> /mnt/c/Project/x/ops/demo.sh
    drive, rest = os.path.splitdrive(str(script))
    return "/mnt/" + drive.rstrip(":").lower() + rest.replace("\\", "/")


DEMO_SH = os.environ.get("DEMO_SH") or _default_demo_sh()
HOST = graph_mod.SERVICE_HOST
JAEGER_UI = os.environ.get("JAEGER_UI", "http://localhost:16686")

SERVICES = [
    # name, port, one-line role shown on the card
    ("order-api", 8081, "Customer-facing API: place and look up orders"),
    ("inventory-svc", 8082, "Stock levels and reservations (H2 database)"),
    ("notification-svc", 8083, "Consumes order events, notifies customers"),
    ("broker", 8084, "JMS broker hosting the order.events queue"),
]

app = Flask(__name__)

# --- shared job state ------------------------------------------------------
_lock = threading.Lock()

analysis = {
    "running": False,
    "started_at": None,
    "question": "",
    "stages": [],
    "kag": {},
    "rag": {},
}

control = {"running": False, "action": "", "log": "", "done": False}


def _blank_stages():
    return [
        {"id": "seed", "label": "SEED", "hint": "find observed symptoms",
         "status": "pending", "detail": ""},
        {"id": "traverse", "label": "TRAVERSE", "hint": "walk the impact graph backwards",
         "status": "pending", "detail": ""},
        {"id": "rank", "label": "RANK", "hint": "score candidates by blast fit + mechanism",
         "status": "pending", "detail": ""},
        {"id": "reason", "label": "REASON", "hint": "LLM over the retrieved subgraph",
         "status": "pending", "detail": ""},
    ]


def _set_stage(stage_id, status, detail=""):
    with _lock:
        for st in analysis["stages"]:
            if st["id"] == stage_id:
                st["status"] = status
                if detail:
                    st["detail"] = detail


# --- status probing --------------------------------------------------------
def _probe(url, timeout=2):
    try:
        r = requests.get(url, timeout=timeout)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def _health(port):
    data = _probe(f"http://localhost:{port}/actuator/health")
    return bool(data and data.get("status") == "UP")


@app.get("/api/ping")
def api_ping():
    """Cheap liveness check.

    /api/status fans out to Jaeger plus six service health endpoints, so it can
    take over a second to answer -- too slow to use as a "has the server bound
    yet?" probe. START-DEMO.ps1 polls this instead.
    """
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with ThreadPoolExecutor(max_workers=8) as pool:
        jaeger_f = pool.submit(_probe, "http://localhost:16686/api/services")
        svc_f = {name: pool.submit(_health, port) for name, port in (
            ("order-api", 8081), ("inventory-svc", 8082),
            ("notification-svc", 8083), ("broker", 8084))}
        pool_f = pool.submit(_probe, "http://localhost:8082/admin/pool")
        stats_f = pool.submit(_probe, "http://localhost:8083/admin/stats")

        jaeger = jaeger_f.result()
        services = [{"name": n, "up": f.result()} for n, f in svc_f.items()]
        hikari = pool_f.result() or {}
        stats = stats_f.result() or {}

    services.insert(0, {"name": "jaeger", "up": jaeger is not None})

    max_size = hikari.get("maxPoolSize")
    peak_lag = stats.get("maxLagMs", stats.get("lastLagMs", 0)) or 0

    return jsonify({
        "services": services,
        "pool": {
            "max": max_size,
            "active": hikari.get("active"),
            "awaiting": hikari.get("awaitingConnection"),
            # Baseline is 20; anything smaller is the injected fault.
            "degraded": max_size is not None and max_size < 20,
        },
        "queue": {
            "peakLagMs": peak_lag,
            "lastLagMs": stats.get("lastLagMs", 0),
            "processed": stats.get("processed", 0),
            "failed": stats.get("failed", 0),
            "idle": stats.get("idle", True),
            "degraded": peak_lag >= 1000,
        },
        "llm": {"provider": llm.provider(), "model": llm.model_name()},
        "control": control,
    })


# --- scenario / load control (shells out to demo.sh inside WSL) ------------
ACTIONS = {
    "scenario1": (["scenario", "1"], "Injecting poison message"),
    "scenario2": (["scenario", "2"], "Redeploying inventory-svc with pool=2"),
    "reset":     (["reset"], "Resetting to a healthy system"),
    # 30 workers, not 20. At 20 the wait for a connection lands right on
    # order-api's 2s timeout and the failure rate swings between 10% and 57%
    # run to run depending on whether a queue backlog was carried in; at 30 it
    # is decisively past the timeout and reproducible, which is what you need
    # when a room is watching.
    "load":      (["load", "75", "30"], "Driving load (30 workers, 75s)"),
    "loadlight": (["load", "45", "8"], "Driving baseline load (8 workers, 45s)"),
}


def _demo_sh(*args):
    cmd = (["wsl", "-d", WSL_DISTRO, "--", "bash", DEMO_SH, *args] if ON_WINDOWS
           else ["bash", DEMO_SH, *args])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    # demo.sh colours its output for terminals; the browser wants plain text.
    proc.stdout = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout or "")
    proc.stderr = re.sub(r"\x1b\[[0-9;]*m", "", proc.stderr or "")
    return proc


def _run_control(action):
    args, _ = ACTIONS[action]
    try:
        proc = _demo_sh(*args)
        out = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:                      # noqa: BLE001 - surfaced in the UI
        out = f"failed: {exc}"
    with _lock:
        control.update(running=False, done=True, log=out.strip()[-4000:])


@app.post("/api/control")
def api_control():
    action = (request.json or {}).get("action", "")
    if action not in ACTIONS:
        return jsonify({"error": "unknown action"}), 400
    with _lock:
        if control["running"]:
            return jsonify({"error": "another action is already running"}), 409
        control.update(running=True, action=action, done=False,
                       log=ACTIONS[action][1] + "...")
    threading.Thread(target=_run_control, args=(action,), daemon=True).start()
    return jsonify({"ok": True, "message": ACTIONS[action][1]})


# --- the analysis ----------------------------------------------------------
def _run_rag(question):
    try:
        prompt = rag_baseline.build_prompt()
        flat_input = rag_baseline.collect_flat_context()
        if question:
            prompt += "\n\n## Specific question\n" + question
        try:
            answer = llm.complete(prompt, system=rag_baseline.SYSTEM_PROMPT)
            err = None
        except (llm.NoLLMConfigured, llm.LLMError) as exc:
            answer, err = None, str(exc)
        with _lock:
            analysis["rag"] = {"input": flat_input, "prompt": prompt,
                               "answer": answer, "error": err,
                               "lines": len(flat_input.splitlines())}
    except Exception as exc:                      # noqa: BLE001
        with _lock:
            analysis["rag"] = {"error": f"baseline failed: {exc}"}


def _run_kag(question):
    try:
        _set_stage("seed", "running")
        tg = graph_mod.build()
        symptoms = tg.symptoms()
        if not symptoms:
            _set_stage("seed", "empty", "no symptoms - system looks healthy")
            for sid in ("traverse", "rank", "reason"):
                _set_stage(sid, "skipped")
            with _lock:
                analysis["kag"] = {"error": "No symptoms detected. Inject a scenario first."}
            return
        _set_stage("seed", "done",
                   f"{len(symptoms)} symptom{'s' if len(symptoms) != 1 else ''}: "
                   + ", ".join(s.replace("symptom:", "") for s in symptoms))

        _set_stage("traverse", "running")
        reached = kag_engine.traverse(tg)
        _set_stage("traverse", "done",
                   f"{len(reached)} nodes within {kag_engine.MAX_HOPS} hops "
                   f"(graph: {tg.g.number_of_nodes()} nodes, {tg.g.number_of_edges()} edges)")

        _set_stage("rank", "running")
        candidates = kag_engine.rank(tg)
        _set_stage("rank", "done", f"{len(candidates)} candidates scored")

        cand_view = [{
            "id": c.node_id, "kind": c.kind, "score": round(c.score, 3),
            "blast": round(c.blast_fit, 2), "mech": round(c.mechanism, 2),
            "change": round(c.change_signal, 2), "anom": round(c.anomaly_signal, 2),
            "explains": len(c.explains), "total": len(symptoms),
            "paths": [kag_engine._render_path(tg, p) for p in c.paths],
        } for c in candidates]
        with _lock:
            analysis["kag"] = {"candidates": cand_view, "symptoms": symptoms}

        prompt = kag_engine.build_prompt(tg, candidates)
        if question:
            prompt += "\n\n## Specific question\n" + question

        _set_stage("reason", "running", f"querying {llm.model_name()}")
        try:
            answer = llm.complete(prompt, system=kag_engine.SYSTEM_PROMPT)
            err = None
            _set_stage("reason", "done", "answer received")
        except (llm.NoLLMConfigured, llm.LLMError) as exc:
            answer, err = None, str(exc)
            _set_stage("reason", "empty", "no LLM configured")

        with _lock:
            analysis["kag"].update(prompt=prompt, answer=answer, error=err)

        # Refresh the graph page so the highlighted path matches this run.
        try:
            import viz
            viz.render(tg, candidates)
        except Exception:                          # noqa: BLE001 - cosmetic only
            pass
    except Exception as exc:                      # noqa: BLE001
        _set_stage("reason", "error", str(exc))
        with _lock:
            analysis["kag"] = {"error": f"analysis failed: {exc}"}


def _run_analysis(question):
    rag_thread = threading.Thread(target=_run_rag, args=(question,), daemon=True)
    rag_thread.start()
    _run_kag(question)
    rag_thread.join(timeout=180)
    with _lock:
        analysis["running"] = False


@app.post("/api/analyze")
def api_analyze():
    question = (request.json or {}).get("question", "").strip()
    with _lock:
        if analysis["running"]:
            return jsonify({"error": "analysis already running"}), 409
        analysis.update(running=True, started_at=time.time(), question=question,
                        stages=_blank_stages(), kag={}, rag={})
    threading.Thread(target=_run_analysis, args=(question,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/analysis")
def api_analysis():
    with _lock:
        elapsed = round(time.time() - analysis["started_at"], 1) if analysis["started_at"] else 0
        return jsonify({**analysis, "elapsed": elapsed})


# --- the story console ------------------------------------------------------
health_monitor = monitor.HealthMonitor()


def _svc_url(port, path):
    return f"http://{HOST}:{port}{path}"


@app.get("/api/overview")
def api_overview():
    state = health_monitor.snapshot()
    with ThreadPoolExecutor(max_workers=6) as pool:
        missing = {n: pool.submit(monitor.check, p) for n, p in
                   ((n, p) for n, p, _ in SERVICES) if n not in state}
        jaeger_f = pool.submit(_probe, JAEGER_UI + "/api/services")
        chaos_f = pool.submit(_probe, _svc_url(8082, "/admin/chaos"))
        live = {n: f.result()[0] for n, f in missing.items()}
        jaeger_up = jaeger_f.result() is not None
        chaos = chaos_f.result() or {}

    services = [{
        "name": n, "port": p, "role": role,
        "state": state.get(n) or live.get(n, "UNKNOWN"),
        "swagger": _svc_url(p, "/swagger-ui.html"),
        "health": _svc_url(p, "/actuator/health"),
        "traces": f"{JAEGER_UI}/search?service={n}&lookback=15m&limit=20",
    } for n, p, role in SERVICES]
    with _lock:
        traffic_view = dict(traffic, recent=list(traffic["recent"]))
    return jsonify({
        "services": services,
        "jaeger": {"up": jaeger_up, "url": JAEGER_UI},
        "chaos": chaos,
        "traffic": traffic_view,
        "events": monitor.recent_events(12),
        "llm": {"provider": llm.provider(), "model": llm.model_name(),
                "warning": llm.config_warning()},
        "control": control,
    })


# --- "try it": proxied so the browser needs no CORS ----------------------
TRY = {
    "place_order":  ("POST", 8081, "/orders"),
    "availability": ("GET", 8081, "/products/{sku}/availability"),
    "recent":       ("GET", 8081, "/orders"),
    "inventory":    ("GET", 8082, "/inventory"),
}


def _call(method, port, path, body=None, timeout=10):
    started = time.time()
    try:
        r = requests.request(method, _svc_url(port, path), json=body, timeout=timeout)
        try:
            payload = r.json()
        except ValueError:
            payload = r.text[:500]
        return {"status": r.status_code, "ms": round((time.time() - started) * 1000),
                "body": payload, "traceId": r.headers.get("X-Trace-Id")}
    except requests.RequestException as exc:
        return {"status": 0, "ms": round((time.time() - started) * 1000),
                "body": {"error": "connection failed",
                         "detail": "refused" if "refused" in str(exc).lower() else "timed out"},
                "traceId": None}


@app.post("/api/try")
def api_try():
    req = request.json or {}
    action = req.get("action", "")
    if action not in TRY:
        return jsonify({"error": "unknown action"}), 400
    method, port, path = TRY[action]
    sku = req.get("sku") or "SKU-1001"
    out = _call(method, port, path.format(sku=sku),
                body={"sku": sku} if method == "POST" else None)
    out["request"] = f"{method} {_svc_url(port, path.format(sku=sku))}"
    if out.get("traceId"):
        out["jaeger"] = f"{JAEGER_UI}/trace/{out['traceId']}"
    return jsonify(out)


# --- background traffic, so there is always something to observe ---------
traffic = {"on": False, "workers": 0, "sent": 0, "ok": 0, "failed": 0,
           "since": None, "recent": []}
_traffic_stop = threading.Event()


def _traffic_worker():
    skus = ["SKU-1001", "SKU-1002", "SKU-1003"]
    while not _traffic_stop.is_set():
        if random.random() < 0.8:
            res = _call("POST", 8081, "/orders", body={"sku": random.choice(skus)}, timeout=10)
        else:
            res = _call("GET", 8081, f"/products/{random.choice(skus)}/availability", timeout=10)
        ok = 200 <= res["status"] < 300
        with _lock:
            traffic["sent"] += 1
            traffic["ok" if ok else "failed"] += 1
            traffic["recent"] = (traffic["recent"] + [1 if ok else 0])[-60:]
        _traffic_stop.wait(0.35)


@app.post("/api/traffic")
def api_traffic():
    on = bool((request.json or {}).get("on"))
    with _lock:
        if on and not traffic["on"]:
            _traffic_stop.clear()
            traffic.update(on=True, workers=4, sent=0, ok=0, failed=0,
                           since=time.time(), recent=[])
            for _ in range(4):
                threading.Thread(target=_traffic_worker, daemon=True).start()
        elif not on and traffic["on"]:
            _traffic_stop.set()
            traffic.update(on=False, workers=0)
    return jsonify({"ok": True, "on": on})


# --- presenter fault injection --------------------------------------------
@app.post("/api/chaos")
def api_chaos():
    req = request.json or {}
    action = req.get("action", "")
    if action == "latency":
        ms = int(req.get("ms") or 2500)
        out = _call("POST", 8082, f"/admin/chaos/latency?ms={ms}", timeout=5)
        return jsonify({"ok": out["status"] == 200, "message": f"inventory-svc fetches +{ms} ms", **out})
    if action == "errors":
        rate = float(req.get("rate") or 0.5)
        out = _call("POST", 8082, f"/admin/chaos/errors?rate={rate}", timeout=5)
        return jsonify({"ok": out["status"] == 200,
                        "message": f"inventory-svc fails {rate:.0%} of fetches", **out})
    if action == "clear":
        out = _call("DELETE", 8082, "/admin/chaos", timeout=5)
        return jsonify({"ok": out["status"] == 200, "message": "latency/errors cleared", **out})

    svc = req.get("service", "")
    if action in ("kill", "restart") and svc not in {n for n, _, _ in SERVICES}:
        return jsonify({"error": "unknown service"}), 400
    args = {"kill": ["kill", svc], "restart": ["restart", svc], "heal": ["heal"]}.get(action)
    if not args:
        return jsonify({"error": "unknown action"}), 400
    label = {"kill": f"Stopping {svc}", "restart": f"Restarting {svc}",
             "heal": "Healing everything"}[action]
    with _lock:
        if control["running"]:
            return jsonify({"error": "another action is already running"}), 409
        control.update(running=True, action=action, done=False, log=label + "...")

    def run():
        try:
            proc = _demo_sh(*args)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:                  # noqa: BLE001 - surfaced in the UI
            out = f"failed: {exc}"
        with _lock:
            control.update(running=False, done=True, log=out.strip()[-4000:])

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": label})


# --- ask ---------------------------------------------------------------------
asking = {"running": False, "question": "", "started_at": None, "stages": [],
          "report": None, "prompt": "", "candidates": [], "error": None, "elapsed": 0}


def _ask_stages():
    return [{"id": i, "label": l, "hint": h, "status": "pending", "detail": ""} for i, l, h in (
        ("observe", "OBSERVE", "health checks, traces, pool, queue"),
        ("seed", "SEED", "turn what hurts into symptoms"),
        ("rank", "RANK", "walk the knowledge graph back to a cause"),
        ("reason", "REASON", "LLM fills the structured answer"))]


def _on_ask_stage(stage_id, status, detail=""):
    with _lock:
        for st in asking["stages"]:
            if st["id"] == stage_id:
                st.update(status=status, detail=detail or st["detail"])


def _run_ask(question):
    try:
        res = assistant.ask(question, on_stage=_on_ask_stage)
        with _lock:
            asking.update(report=res.report.model_dump(), prompt=res.prompt,
                          candidates=res.candidates, error=res.llm_error,
                          elapsed=res.elapsed_s)
    except Exception as exc:                      # noqa: BLE001
        with _lock:
            asking["error"] = f"ask failed: {exc}"
    finally:
        with _lock:
            asking["running"] = False


@app.post("/api/ask")
def api_ask():
    question = (request.json or {}).get("question", "").strip()
    with _lock:
        if asking["running"]:
            return jsonify({"error": "already answering"}), 409
        asking.update(running=True, question=question, started_at=time.time(),
                      stages=_ask_stages(), report=None, prompt="", candidates=[],
                      error=None, elapsed=0)
    threading.Thread(target=_run_ask, args=(question,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/ask")
def api_ask_state():
    with _lock:
        out = dict(asking)
        if out["running"] and out["started_at"]:
            out["elapsed"] = round(time.time() - out["started_at"], 1)
        return jsonify(out)


# --- pages -----------------------------------------------------------------
@app.get("/")
def index():
    return send_file(HERE / "ui_index.html")


@app.get("/classic")
def classic():
    return send_file(HERE / "ui_classic.html")


@app.get("/graph.html")
def graph_page():
    path = HERE / "graph.html"
    if not path.exists():
        return "<p style='font:14px sans-serif;padding:2rem'>" \
               "No graph yet - run an analysis first.</p>"
    return send_file(path)


if __name__ == "__main__":
    health_monitor.start()
    print("\n  LLM: " + llm.provider() + " / " + llm.model_name())
    if llm.config_warning():
        print("  WARNING: " + llm.config_warning())
    print("\n  Demo console -> http://localhost:8090\n")
    app.run(host="127.0.0.1", port=8090, threaded=True, debug=False)
