"""Demo console: a local web UI for the OTel + KAG root cause demo.

Runs on Windows (Python can bind sockets here even though the JVM cannot) and
talks to the services running inside WSL over forwarded localhost ports. It is
the single control surface for the session -- inject scenarios, drive load, and
run the analysis without ever showing a terminal.

    py ui.py            then open http://localhost:8090

Two deliberate design choices:

  * Background thread + polling, not SSE. Werkzeug can buffer streamed responses
    in ways that are fine on a laptop and embarrassing on stage; a 250ms poll is
    invisible to an audience and cannot surprise you.

  * The baseline and KAG analyses run CONCURRENTLY in separate threads, so both
    panels fill at the same time. Watching them race is the argument.
"""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

import graph as graph_mod
import kag_engine
import llm
import rag_baseline

HERE = Path(__file__).resolve().parent
DEMO_SH = "/mnt/c/Project/flo/flo2026 demo/otel-kag/ops/demo.sh"
WSL_DISTRO = "Ubuntu-22.04"

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


def _run_control(action):
    args, _ = ACTIONS[action]
    try:
        proc = subprocess.run(
            ["wsl", "-d", WSL_DISTRO, "--", "bash", DEMO_SH, *args],
            capture_output=True, text=True, timeout=600)
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


# --- pages -----------------------------------------------------------------
@app.get("/")
def index():
    return send_file(HERE / "ui_index.html")


@app.get("/graph.html")
def graph_page():
    path = HERE / "graph.html"
    if not path.exists():
        return "<p style='font:14px sans-serif;padding:2rem'>" \
               "No graph yet - run an analysis first.</p>"
    return send_file(path)


if __name__ == "__main__":
    print("\n  Demo console -> http://localhost:8090\n")
    app.run(host="127.0.0.1", port=8090, threaded=True, debug=False)
