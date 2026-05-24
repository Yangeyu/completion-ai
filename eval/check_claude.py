"""Benchmark: feed `claude --help` (and selected subcommands) to the LLM
and score the result against a golden fixture.

Run:
    DASHSCOPE_API_KEY=... python -m eval.check_claude

Cases are defined in fixtures/claude_expected.json. Each case targets one
node (root or a subcommand) and validates:
  - discover_subcommands precision / recall / F1
  - _extract_node flag coverage (must-have set)
  - positional coverage (name match, optional repeatable check)
  - per-flag detail checks (short / takes_value / choices)

Exit code 0 if every case passes its thresholds, else 1.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from completion_ai.crawler import HelpNode
from completion_ai.llm import DEFAULT_MODEL, _extract_node, discover_subcommands

FIXTURE_DIR = Path(__file__).parent / "fixtures"
EXPECTED_FILE = FIXTURE_DIR / "claude_expected.json"

SUBCMD_F1_MIN = 0.95
FLAG_RECALL_MIN = 0.90
FLAG_DETAIL_MIN = 0.85


def prf(expected: set[str], actual: set[str]) -> tuple[float, float, float]:
    tp = len(expected & actual)
    p = tp / len(actual) if actual else (1.0 if not expected else 0.0)
    r = tp / len(expected) if expected else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1


def check_flag_detail(actual_flag: dict, expected: dict) -> list[str]:
    errs = []
    if "short" in expected and actual_flag.get("short") != expected["short"]:
        errs.append(f"short={actual_flag.get('short')!r} want {expected['short']!r}")
    if "takes_value" in expected:
        if bool(actual_flag.get("takes_value")) != expected["takes_value"]:
            errs.append(
                f"takes_value={actual_flag.get('takes_value')!r} "
                f"want {expected['takes_value']!r}"
            )
    if "choices" in expected:
        got = set(actual_flag.get("choices") or [])
        missing = set(expected["choices"]) - got
        if missing:
            errs.append(f"choices missing {sorted(missing)}")
    return errs


def run_case(case: dict) -> bool:
    path = case["path"]
    label = " ".join(path)
    print(f"\n=== {label} ===")
    help_text = (FIXTURE_DIR / case["help_file"]).read_text()

    # 1. discover
    actual_subs = set(discover_subcommands(help_text))
    expected_subs = set(case["subcommands"])
    p, r, f1 = prf(expected_subs, actual_subs)
    missing = sorted(expected_subs - actual_subs)
    extra = sorted(actual_subs - expected_subs)
    print(f"[discover] precision={p:.2f} recall={r:.2f} f1={f1:.2f}")
    if missing: print(f"           missing: {missing}")
    if extra:   print(f"           extra:   {extra}")
    sub_ok = f1 >= SUBCMD_F1_MIN
    print(f"           -> {'PASS' if sub_ok else 'FAIL'}")

    # 2. extract
    node = HelpNode(path=path, help_text=help_text)
    result = _extract_node(node, model=DEFAULT_MODEL, verbose=False)
    actual_flags = {f.get("long"): f for f in result.get("flags", []) if f.get("long")}

    # 2a. flag coverage
    must = set(case["must_have_flags"])
    found = must & actual_flags.keys()
    flag_recall = len(found) / len(must) if must else 1.0
    flag_missing = sorted(must - actual_flags.keys())
    print(f"[flags]    {len(found)}/{len(must)} recall={flag_recall:.2f}")
    if flag_missing: print(f"           missing: {flag_missing}")
    flag_ok = flag_recall >= FLAG_RECALL_MIN
    print(f"           -> {'PASS' if flag_ok else 'FAIL'}")

    # 2b. positionals
    actual_pos = {p.get("name", "").lower(): p for p in result.get("positionals", [])}
    pos_ok = True
    if case["positionals"]:
        print("[positionals]")
        for want in case["positionals"]:
            name = want["name"].lower()
            ap = actual_pos.get(name)
            if not ap:
                print(f"           {want['name']:14s} MISSING"); pos_ok = False; continue
            errs = []
            if want.get("repeatable") and not ap.get("repeatable"):
                errs.append("repeatable=false want true")
            status = "OK" if not errs else f"FAIL {'; '.join(errs)}"
            if errs: pos_ok = False
            print(f"           {want['name']:14s} {status}")
        print(f"           -> {'PASS' if pos_ok else 'FAIL'}")

    # 2c. flag details
    detail_ok = True
    if case["flag_details"]:
        print("[details]")
        passed = 0
        for spec in case["flag_details"]:
            long = spec["long"]
            af = actual_flags.get(long)
            if not af:
                print(f"           {long:22s} MISSING"); continue
            errs = check_flag_detail(af, spec)
            if errs:
                print(f"           {long:22s} FAIL  {'; '.join(errs)}")
            else:
                print(f"           {long:22s} OK"); passed += 1
        total = len(case["flag_details"])
        rate = passed / total if total else 1.0
        detail_ok = rate >= FLAG_DETAIL_MIN
        print(f"           {passed}/{total} rate={rate:.2f} "
              f"-> {'PASS' if detail_ok else 'FAIL'}")

    return sub_ok and flag_ok and pos_ok and detail_ok


def main() -> int:
    spec = json.loads(EXPECTED_FILE.read_text())
    results = [(c["path"], run_case(c)) for c in spec["cases"]]

    print("\n=== Summary ===")
    for path, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {' '.join(path)}")
    overall = all(ok for _, ok in results)
    print(f"  overall: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
