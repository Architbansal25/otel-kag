"""Renders the knowledge graph to a standalone interactive HTML file.

Worth the 40 lines: the single most persuasive moment in the session is the
audience seeing the causal path light up across four different entity types --
deployment, config item, connection pool, service -- and land on a symptom.
A table of scores does not land the same way.

No Neo4j, no server, no Docker. Opens straight in a browser.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from pyvis.network import Network

from graph import TelemetryGraph

HERE = Path(__file__).resolve().parent

COLOURS = {
    "Service":        "#4C8BF5",
    "Host":           "#8E8E93",
    "Queue":          "#F5A623",
    "Datastore":      "#7B61FF",
    "ConnectionPool": "#00A88F",
    "ConfigItem":     "#E8B400",
    "Deployment":     "#D64545",
    "Symptom":        "#FF3B30",
}
CAUSAL_PATH_COLOUR = "#FF3B30"


def render(tg: TelemetryGraph, candidates: List = None, out: Path = None) -> Path:
    out = out or (HERE / "graph.html")
    candidates = candidates or []

    # cdn_resources="in_line" embeds vis.js into the file. Without it the page is
    # a 12 KB stub that fetches the library at open time -- which is a bad bet on
    # conference wifi. Inlined it is ~700 KB and works with the network unplugged.
    net = Network(height="820px", width="100%", directed=True,
                  bgcolor="#ffffff", font_color="#1a1a1a",
                  cdn_resources="in_line")
    net.barnes_hut(gravity=-9000, spring_length=190, spring_strength=0.02)

    # Edges and nodes on the winning candidate's causal paths get highlighted.
    highlighted = set()
    on_causal_path = set()
    if candidates:
        for path in candidates[0].paths:
            on_causal_path.update(path)
            for i in range(len(path) - 1):
                highlighted.add((path[i], path[i + 1]))
                highlighted.add((path[i + 1], path[i]))  # underlying edge may run either way

    for node_id, attrs in tg.g.nodes(data=True):
        kind = attrs.get("kind", "?")
        evidence = attrs.get("evidence") or []
        tooltip = [kind + ": " + node_id]
        for key in ("title", "error_rate", "p95_ms", "max_size", "baseline_max_size",
                    "awaiting", "last_lag_ms", "value", "previous", "note", "at"):
            if attrs.get(key) is not None:
                tooltip.append(str(key) + " = " + str(attrs[key]))
        for ev in evidence[:4]:
            tooltip.append("[" + ev.kind + "] " + ev.text)

        on_path = node_id in on_causal_path
        net.add_node(
            node_id,
            label=node_id.replace("symptom:", "").replace("config:", ""),
            title="\n".join(tooltip),
            color=COLOURS.get(kind, "#CCCCCC"),
            shape="box" if kind in ("Deployment", "ConfigItem") else "dot",
            size=30 if kind == "Symptom" else (26 if on_path else 18),
            borderWidth=4 if on_path else 1,
        )

    for u, v, rel in tg.g.edges(keys=True):
        is_hot = (u, v) in highlighted
        net.add_edge(u, v, title=rel, label=rel if is_hot else "",
                     color=CAUSAL_PATH_COLOUR if is_hot else "#C8C8C8",
                     width=4 if is_hot else 1)

    # pyvis's own write_html opens the file with the platform default encoding,
    # which on Windows is cp1252 and cannot represent the inlined vis.js. Generate
    # the markup and write it as UTF-8 ourselves.
    html = net.generate_html(notebook=False)

    # pyvis's template also pulls Bootstrap from a CDN purely for chrome. The graph
    # itself does not need it, so drop those tags and the page is fully offline.
    for tag in (
        '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3/dist/js/bootstrap.bundle.min.js"'
        ' integrity="sha384-JEW9xMcG8R+pH31jmWH6WWP0WintQrMb4s7ZOdauHnUtxwoG2vI5DkLtS3qm9Ekf"'
        ' crossorigin="anonymous"></script>',
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3/dist/css/bootstrap.min.css"'
        ' integrity="sha384-eOJMYsd53ii+scO/bJGFsiCZc+5NDVN2yr8+0RDqr0Ql0h+rP48ckxlpbzKgwra6"'
        ' crossorigin="anonymous" />',
    ):
        html = html.replace(tag, "")
    # Belt and braces: kill any bootstrap tag whose hash differs by pyvis version.
    html = re.sub(r'<(script|link)[^>]*cdn\.jsdelivr\.net[^>]*>(</script>)?', "", html)

    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    import graph as graph_mod
    import kag_engine

    tg = graph_mod.build()
    print("Wrote " + str(render(tg, kag_engine.rank(tg))))
