"""Demo entrypoint. Runs the flat-log baseline and the KAG pipeline side by side.

    py rca.py                  # both, the money shot
    py rca.py --mode kag       # KAG only
    py rca.py --mode rag       # baseline only
    py rca.py --show-prompt    # reveal exactly what each one sent the model
    py rca.py --viz            # also write the interactive graph to graph.html
"""

from __future__ import annotations

import argparse
import sys

import graph as graph_mod
import kag_engine
import llm
import rag_baseline

WIDTH = 78


def rule(title: str = "", char: str = "=") -> None:
    if not title:
        print(char * WIDTH)
        return
    pad = WIDTH - len(title) - 4
    print("\n" + char * 3 + " " + title + " " + char * max(pad, 3))


def run_rag(lookback: str, show_prompt: bool) -> None:
    rule("BASELINE: flat log retrieval + LLM  (what most demos do)")
    prompt, answer, error = rag_baseline.analyze(lookback)

    if show_prompt:
        print("\n--- prompt sent ---\n" + prompt + "\n--- end prompt ---\n")

    if answer:
        print(answer)
    else:
        print("[no LLM configured: " + str(error) + "]")
        print("\nThe grounded prompt it WOULD have sent:\n")
        print(prompt)


def run_kag(tg, lookback: str, show_prompt: bool) -> None:
    rule("KAG: knowledge-graph retrieval + LLM")
    result = kag_engine.analyze(tg)

    if not result.candidates:
        print(result.error or "No candidates found.")
        return

    print("\nRetrieval trace (this is the part a flat search cannot do):")
    print("  graph: " + tg.summary())
    print("  symptoms seeded: " + ", ".join(tg.symptoms()))
    print("\n  ranked candidates:")
    for i, cand in enumerate(result.candidates, 1):
        marker = " <-- selected" if i == 1 else ""
        print("   " + str(i) + ". " + cand.node_id.ljust(52) + " " + cand.why() + marker)
        if i == 1:
            for path in cand.paths:
                print("        path: " + kag_engine._render_path(tg, path))

    if show_prompt:
        print("\n--- prompt sent ---\n" + result.prompt + "\n--- end prompt ---\n")

    print()
    if result.answer:
        print(result.answer)
    else:
        print("[no LLM configured: " + str(result.error) + "]")
        print("\nThe retrieved subgraph it WOULD have sent:\n")
        print(result.prompt)


def main() -> int:
    parser = argparse.ArgumentParser(description="OTel -> KAG root cause analysis")
    parser.add_argument("--mode", choices=["kag", "rag", "both"], default="both")
    parser.add_argument("--lookback", default="15m")
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--viz", action="store_true", help="write graph.html")
    args = parser.parse_args()

    rule()
    print("Root Cause Analysis  |  window=" + args.lookback
          + "  |  llm=" + llm.provider() + ":" + llm.model_name())
    rule()

    tg = graph_mod.build(lookback=args.lookback)
    if not tg.symptoms():
        print("\nNo symptoms detected in the last " + args.lookback + ".")
        print("The system looks healthy -- inject a scenario first, then re-run:")
        print("  wsl -d Ubuntu-22.04 -- bash ../ops/demo.sh scenario 2")
        print("  wsl -d Ubuntu-22.04 -- bash ../ops/demo.sh load 90 20")
        return 0

    if args.mode in ("rag", "both"):
        run_rag(args.lookback, args.show_prompt)
    if args.mode in ("kag", "both"):
        run_kag(tg, args.lookback, args.show_prompt)

    if args.viz:
        import viz
        path = viz.render(tg, kag_engine.rank(tg))
        print("\nInteractive graph written to " + str(path))

    print()
    rule()
    return 0


if __name__ == "__main__":
    sys.exit(main())
