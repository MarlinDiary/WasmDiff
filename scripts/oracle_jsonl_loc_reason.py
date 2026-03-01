#!/usr/bin/env python3
# Aggregate LLVM pass-oracle JSONL into a per-location "reason" CSV.
#
# This is intended to be used on the JSONL emitted by our patched LLVM
# (pass-oracle), where each record corresponds to one pass acting on one
# function and optionally includes `loc_deltas`.
#
# The "oracle" is the JSONL (what passes changed what, and where). This script
# is just a reducer that converts raw per-pass events into a compact table:
#   file:line:col -> { reason -> diff_score }
#
# We compare two sets of JSONL files (A baseline vs B variant) and compute, for
# each debug location, how much *more* each category contributes in B vs A.

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple


def _norm_path(p: str, repo_root: Optional[Path]) -> str:
    if p.startswith("wasisdk://"):
        return p

    # Canonicalize Rust stdlib source paths so A/B match even when one side uses
    # the "repro" virtual prefix (/rustc/<hash>/...) and the other side points
    # at the local rust-src component under the toolchain.
    #
    # Examples we want to unify:
    #   /rustc/<hash>/library/core/src/...
    #   /Users/.../.rustup/toolchains/.../lib/rustlib/src/rust/library/core/src/...
    m = re.match(r"^/rustc/[0-9a-f]{40}/library/(.+)$", p)
    if m:
        return f"rust/library/{m.group(1)}"
    m = re.match(r"^(.*/)?lib/rustlib/src/rust/library/(.+)$", p)
    if m:
        return f"rust/library/{m.group(2)}"

    if repo_root is not None:
        rr = str(repo_root.resolve())
        if p.startswith(rr + "/"):
            return os.path.relpath(p, rr)
    home = str(Path.home())
    if p.startswith(home + "/"):
        return "~/" + p[len(home) + 1 :]
    return p


def _pass_reason(pass_name: str, pass_id: str) -> str:
    s = (pass_name + " " + pass_id).lower()

    # Order matters: inline/DCE/CFG are fairly distinct signals.
    if "inline" in s or "inliner" in s:
        return "inlining"

    if "globaldce" in s or re.search(r"(^|[^a-z])adce([^a-z]|$)", s) or re.search(
        r"(^|[^a-z])dce([^a-z]|$)", s
    ):
        return "dead_code_elimination"

    if "deadarg" in s or "elim-avail-extern" in s or "strip-dead-prototypes" in s:
        return "dead_code_elimination"

    if "simplifycfg" in s:
        return "cfg_simplification"

    if "sccp" in s or "ipsccp" in s or "constprop" in s or "correlated-propagation" in s:
        return "constant_folding_or_propagation"

    if "instcombine" in s:
        return "constant_folding_or_propagation"

    if "memcpyopt" in s or "memmoveopt" in s:
        return "memcpy_optimization"

    if "sroa" in s or "mem2reg" in s:
        return "scalar_replacement"

    return "other"


def _score(d_inst: int, d_call: int, d_br: int) -> int:
    # Same weights we used inside LLVM when ranking loc deltas.
    return abs(d_inst) + 16 * abs(d_call) + 4 * abs(d_br)


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def load_scores(
    jsonl_paths: Sequence[Path],
    *,
    repo_root: Optional[Path],
) -> Tuple[
    DefaultDict[str, DefaultDict[str, int]],
    DefaultDict[str, DefaultDict[str, int]],
]:
    """
    Returns:
      - scores[loc][reason] = total score
      - pass_scores[loc][pass_key] = total score (pass_key = "pass_name (pass_id)")
    """
    scores: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))
    pass_scores: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))

    for p in jsonl_paths:
        for rec in _iter_jsonl(p):
            if rec.get("kind") != "pass_oracle":
                continue
            loc_deltas = rec.get("loc_deltas") or []
            if not loc_deltas:
                continue

            pass_name = str(rec.get("pass_name", ""))
            pass_id = str(rec.get("pass_id", ""))
            reason = _pass_reason(pass_name, pass_id)
            pass_key = f"{pass_name} ({pass_id})"

            for ld in loc_deltas:
                file = _norm_path(str(ld.get("file", "")), repo_root)
                line = int(ld.get("line", 0) or 0)
                col = int(ld.get("col", 0) or 0)

                d_inst = int(ld.get("d_inst", 0) or 0)
                d_call = int(ld.get("d_call", 0) or 0)
                d_br = int(ld.get("d_br", 0) or 0)

                sc = _score(d_inst, d_call, d_br)
                if sc == 0:
                    continue

                loc = f"{file}:{line}:{col}"
                scores[loc][reason] += sc
                pass_scores[loc][pass_key] += sc

    return scores, pass_scores


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", action="append", default=[], help="Baseline JSONL (repeatable)")
    ap.add_argument("--b", action="append", default=[], help="Variant JSONL (repeatable)")
    ap.add_argument("--out", required=True, help="Output CSV path")
    ap.add_argument(
        "--out-pass-diff",
        default="",
        help="Optional output: write per-(loc,pass) rows with positive score diff (B-A)",
    )
    ap.add_argument("--repo-root", default="", help="Repo root for path normalization")
    ap.add_argument("--min-diff-score", type=int, default=1, help="Only emit rows with diff score >= N")
    ap.add_argument(
        "--max-extra-passes",
        type=int,
        default=8,
        help="Max number of extra passes to include per loc in the 'extra_passes' column (0 = unlimited)",
    )
    args = ap.parse_args(list(argv))

    if not args.b:
        print("Need at least one --b JSONL", file=sys.stderr)
        return 2

    repo_root: Optional[Path] = None
    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()

    a_paths = [Path(p).resolve() for p in args.a]
    b_paths = [Path(p).resolve() for p in args.b]

    if a_paths:
        a_scores, a_pass_scores = load_scores(a_paths, repo_root=repo_root)
    else:
        a_scores = defaultdict(lambda: defaultdict(int))
        a_pass_scores = defaultdict(lambda: defaultdict(int))

    b_scores, b_pass_scores = load_scores(b_paths, repo_root=repo_root)

    all_locs = sorted(set(a_scores.keys()) | set(b_scores.keys()))

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pass_diff_rows: List[Tuple[str, str, str, int, int, int]] = []

    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["loc", "reason_diffs"])

        for loc in all_locs:
            a_by_reason = a_scores.get(loc, {})
            b_by_reason = b_scores.get(loc, {})

            # Keep only positive diffs (what B does *more* than A at this loc).
            reason_diffs: Dict[str, int] = {}
            reasons = set(a_by_reason.keys()) | set(b_by_reason.keys())
            for r in reasons:
                a_sc = int(a_by_reason.get(r, 0))
                b_sc = int(b_by_reason.get(r, 0))
                d = b_sc - a_sc
                if d > 0:
                    reason_diffs[r] = d

            if not reason_diffs:
                continue

            max_diff = max(reason_diffs.values())
            if max_diff < args.min_diff_score:
                continue

            # Also collect "extra passes" for this loc: passes whose score increased in B vs A.
            a_by_pass = a_pass_scores.get(loc, {})
            b_by_pass = b_pass_scores.get(loc, {})
            pass_keys = set(a_by_pass.keys()) | set(b_by_pass.keys())

            extra: List[Tuple[int, int, int, str]] = []
            for pk in pass_keys:
                a_sc = int(a_by_pass.get(pk, 0))
                b_sc = int(b_by_pass.get(pk, 0))
                d = b_sc - a_sc
                if d <= 0:
                    continue
                extra.append((d, a_sc, b_sc, pk))

            extra.sort(key=lambda t: (-t[0], t[3]))

            if args.max_extra_passes != 0 and len(extra) > args.max_extra_passes:
                extra = extra[: args.max_extra_passes]

            extra_passes = "; ".join([f"{pk}:{d}" for (d, _a, _b, pk) in extra])

            if args.out_pass_diff:
                for (d, a_sc, b_sc, pk) in extra:
                    # Store for later writing (avoid holding file open twice).
                    pass_diff_rows.append((loc, pk, "", d, a_sc, b_sc))

            # Emit as a JSON list sorted by descending diff, then reason name for stability.
            reason_list = sorted(reason_diffs.items(), key=lambda kv: (-kv[1], kv[0]))
            w.writerow([loc, json.dumps(reason_list, separators=(",", ":"))])

    print(f"Wrote: {out_path}", file=sys.stderr)

    if args.out_pass_diff:
        out_pass = Path(args.out_pass_diff).resolve()
        out_pass.parent.mkdir(parents=True, exist_ok=True)
        with out_pass.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["loc", "pass", "diff_score", "a_score", "b_score"])
            for (loc, pk, _reason, d, a_sc, b_sc) in pass_diff_rows:
                w.writerow([loc, pk, str(d), str(a_sc), str(b_sc)])
        print(f"Wrote: {out_pass}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
