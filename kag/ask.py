"""Ask the system about its own health, from the terminal.

    py ask.py                                   # "is anything breaking?"
    py ask.py "what is down and since when?"
    py ask.py --json                            # the raw HealthReport (Pydantic) JSON
    py ask.py --show-prompt                     # what the LLM was given
"""

from __future__ import annotations

import argparse
import sys

import llm
from assistant import DEFAULT_WINDOW, ask

COLOUR = {"HEALTHY": "\033[0;32m", "DEGRADED": "\033[0;33m", "DOWN": "\033[0;31m"}
RESET = "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the platform whether anything is breaking")
    parser.add_argument("question", nargs="*")
    parser.add_argument("--window", default=DEFAULT_WINDOW, help="trace lookback, e.g. 5m")
    parser.add_argument("--json", action="store_true", help="print the HealthReport JSON")
    parser.add_argument("--no-llm", action="store_true", help="rule-based answer only")
    parser.add_argument("--show-prompt", action="store_true")
    args = parser.parse_args()

    result = ask(" ".join(args.question), window=args.window, use_llm=not args.no_llm)
    r = result.report

    if args.show_prompt:
        print("--- prompt ---\n" + result.prompt + "\n--- end prompt ---\n")
    if args.json:
        print(r.model_dump_json(indent=2))
        return 0

    print()
    print(COLOUR.get(r.overall_status, "") + "  " + r.overall_status + RESET
          + "   " + r.question)
    print()
    print("  " + r.answer)
    if r.incident:
        i = r.incident
        print()
        print("  Incident    " + i.title + ("  (ongoing)" if i.ongoing else "  (resolved)"))
        print("  Root cause  " + i.root_cause_component + " -- " + i.root_cause)
        print("  Started     " + str(i.started_at) + ("" if i.ongoing else "   Ended " + str(i.ended_at)))
        print("  Affected    " + ", ".join(i.affected_services))
        print("  Chain")
        for step in i.causal_chain:
            print("    - " + step)
        print("  Fix")
        for step in i.remediation:
            print("    - " + step)
    print()
    for s in r.services:
        print(f"  {COLOUR.get(s.status, '')}{s.status:<9}{RESET} {s.name:<17} {s.detail}")
    print()
    print(f"  confidence={r.confidence}  by={r.generated_by}  in {result.elapsed_s}s")
    if llm.config_warning():
        print("  note: " + llm.config_warning())
    if result.llm_error:
        print("  LLM error: " + result.llm_error[:300])
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
