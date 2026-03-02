#!/usr/bin/env python3
"""
Single entry script for the full ThinLTO oracle pipeline.

This script replaces the previous multi-script flow:
  1) IR pass-oracle build (off vs thin-after-import)
  2) Machine pass-oracle build (llc)
  3) Merge IR + machine reasons
  4) Puzzle alignment to binary diff universe (loc_reason + uncovered)

Default final output:
  cache/oracle/lto-thin/<crate>/{loc_reason.csv,uncovered.tsv}
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


_NOOPT_KEY_RE = re.compile(r"\.([A-Za-z0-9_]+)\.([0-9a-f]{8,32})-cgu\.(\d+)\.rcgu\.no-opt\.bc$")
_THIN_AFTER_IMPORT_KEY_RE = re.compile(
    r"\.([A-Za-z0-9_]+)\.([0-9a-f]{8,32})-cgu\.(\d+)\.rcgu\.o\.rcgu\.thin-lto-after-import\.bc$"
)
_THIN_AFTER_IMPORT_KEY_RE2 = re.compile(
    r"\.([A-Za-z0-9_]+)\.([0-9a-f]{8,32})-cgu\.(\d+)\.rcgu\.thin-lto-after-import\.bc$"
)


def _run(cmd: Sequence[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> None:
    p = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode != 0:
        sys.stderr.write(p.stdout)
        sys.stderr.write(p.stderr)
        raise SystemExit(f"Command failed ({p.returncode}): {' '.join(cmd)}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_pkg_name(driver_dir: Path) -> str:
    toml = driver_dir / "Cargo.toml"
    if not toml.is_file():
        raise SystemExit(f"Missing: {toml}")
    in_pkg = False
    for line in toml.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s == "[package]":
            in_pkg = True
            continue
        if s.startswith("[") and s.endswith("]") and s != "[package]":
            in_pkg = False
        if not in_pkg:
            continue
        if s.startswith("name"):
            parts = s.split("=", 1)
            if len(parts) != 2:
                continue
            rhs = parts[1].strip()
            if rhs.startswith('"') and rhs.endswith('"'):
                return rhs.strip('"')
    raise SystemExit(f"Failed to parse package name from: {toml}")


def _module_key_from_noopt_bc(path: Path) -> Optional[str]:
    m = _NOOPT_KEY_RE.search(path.name)
    if not m:
        return None
    crate, disamb, cgu = m.group(1), m.group(2), m.group(3)
    return f"{crate}.{disamb}-cgu.{cgu}"


def _module_key_from_thin_after_import_bc(path: Path) -> Optional[str]:
    m = _THIN_AFTER_IMPORT_KEY_RE.search(path.name) or _THIN_AFTER_IMPORT_KEY_RE2.search(path.name)
    if not m:
        return None
    crate, disamb, cgu = m.group(1), m.group(2), m.group(3)
    return f"{crate}.{disamb}-cgu.{cgu}"


def _collect_noopt_map(deps_dir: Path) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for p in sorted(deps_dir.glob("*.rcgu.no-opt.bc")):
        k = _module_key_from_noopt_bc(p)
        if not k:
            continue
        if k in out and out[k] != p:
            out[k] = max(out[k], p, key=lambda x: x.stat().st_mtime)
        else:
            out[k] = p
    return out


def _collect_thin_after_import_map(deps_dir: Path) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for p in sorted(deps_dir.glob("*.thin-lto-after-import.bc")):
        k = _module_key_from_thin_after_import_bc(p)
        if not k:
            continue
        if k in out and out[k] != p:
            out[k] = max(out[k], p, key=lambda x: x.stat().st_mtime)
        else:
            out[k] = p
    return out


def _read_modules_tsv(path: Path) -> Tuple[List[Path], Path]:
    off_noopt: List[Path] = []
    thin_after_any: Optional[Path] = None
    with path.open(newline="") as f:
        r = csv.DictReader(f, delimiter="\t")
        for row in r:
            off_p = (row.get("off_noopt") or "").strip()
            thin_after = (row.get("thin_after_import") or "").strip()
            if off_p:
                off_noopt.append(Path(off_p))
            if thin_after and thin_after_any is None:
                thin_after_any = Path(thin_after)
    if not off_noopt:
        raise SystemExit(f"No baseline modules found in: {path}")
    if thin_after_any is None:
        raise SystemExit(f"Failed to find thin_after_import in: {path}")
    return off_noopt, thin_after_any.parent


def _norm_path(p: str, repo_root: Optional[Path]) -> str:
    if p.startswith("wasisdk://"):
        return p

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

    if "inline" in s or "inliner" in s:
        return "inlining"
    if "globaldce" in s or re.search(r"(^|[^a-z])adce([^a-z]|$)", s) or re.search(r"(^|[^a-z])dce([^a-z]|$)", s):
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
    return abs(d_inst) + 16 * abs(d_call) + 4 * abs(d_br)


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _accumulate_scores(
    jsonl_paths: Sequence[Path],
    *,
    repo_root: Optional[Path],
    sign: int,
    out: DefaultDict[str, DefaultDict[str, int]],
) -> None:
    for p in jsonl_paths:
        for rec in _iter_jsonl(p):
            kind = rec.get("kind")

            if kind == "pass_oracle":
                loc_deltas = rec.get("loc_deltas") or []
                if not loc_deltas:
                    continue
                pass_name = str(rec.get("pass_name", ""))
                pass_id = str(rec.get("pass_id", ""))
                reason = _pass_reason(pass_name, pass_id)

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
                    out[f"{file}:{line}:{col}"][reason] += sign * sc

            elif kind == "machine_pass_oracle":
                loc_deltas = rec.get("loc_deltas") or []
                if not loc_deltas:
                    continue
                pass_name = str(rec.get("pass_name", ""))
                pass_id = str(rec.get("pass_id", ""))
                reason = f"machine:{pass_id}" if pass_id else f"machine:{pass_name}"

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
                    out[f"{file}:{line}:{col}"][reason] += sign * sc

            elif kind == "ir_loc_stats":
                loc_counts = rec.get("loc_counts") or []
                if not loc_counts:
                    continue
                reason = "thinlto_import"
                for lc in loc_counts:
                    file = _norm_path(str(lc.get("file", "")), repo_root)
                    line = int(lc.get("line", 0) or 0)
                    col = int(lc.get("col", 0) or 0)
                    inst = int(lc.get("inst", 0) or 0)
                    call = int(lc.get("call", 0) or 0)
                    br = int(lc.get("br", 0) or 0)
                    sc = inst + 16 * call + 4 * br
                    if sc == 0:
                        continue
                    out[f"{file}:{line}:{col}"][reason] += sign * sc


def _reduce_jsonl_to_reason_map(
    a_jsonls: Sequence[Path],
    b_jsonls: Sequence[Path],
    *,
    repo_root: Optional[Path],
) -> Dict[str, Dict[str, int]]:
    acc: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))
    _accumulate_scores(a_jsonls, repo_root=repo_root, sign=-1, out=acc)
    _accumulate_scores(b_jsonls, repo_root=repo_root, sign=1, out=acc)

    out: Dict[str, Dict[str, int]] = {}
    for loc, reason_map in acc.items():
        filtered = {r: int(v) for r, v in reason_map.items() if int(v) != 0}
        if filtered:
            out[loc] = filtered
    return out


def _read_loc_reason(path: Path) -> Dict[str, Dict[str, int]]:
    out: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not path.is_file():
        return {}
    with path.open(newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            loc = (row.get("loc") or "").strip()
            raw = (row.get("reason_diffs") or "").strip()
            if not loc or not raw:
                continue
            try:
                lst = json.loads(raw)
            except Exception:
                continue
            for item in lst:
                if not isinstance(item, list) or len(item) != 2:
                    continue
                reason, diff = item[0], item[1]
                if not isinstance(reason, str):
                    continue
                try:
                    d = int(diff)
                except Exception:
                    continue
                if d == 0:
                    continue
                out[loc][reason] += d
    return {k: dict(v) for k, v in out.items()}


def _write_loc_reason(path: Path, data: Dict[str, Dict[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["loc", "reason_diffs"])
        for loc in sorted(data.keys()):
            items = [(r, int(d)) for r, d in data[loc].items() if int(d) != 0]
            if not items:
                continue
            items.sort(key=lambda t: (-abs(t[1]), t[0]))
            w.writerow([loc, json.dumps(items, separators=(",", ":"))])


def _path_category(path: str) -> str:
    if path.startswith("wasisdk://"):
        return "wasisdk"
    if path.startswith("rust/library/"):
        return "rust/library"
    if path.startswith("cache/crate/"):
        return "crate"
    if path.startswith("/rust/deps/") or path.startswith("rust/deps/"):
        return "rust/deps"
    return "other"


def _discover_crates(drivers_dir: Path, only: Sequence[str]) -> List[Tuple[str, str]]:
    only_set = set(only)
    crates: List[Tuple[str, str]] = []
    for d in sorted(drivers_dir.glob("drv_*")):
        if not d.is_dir():
            continue
        crate = d.name[len("drv_") :]
        if only_set and crate not in only_set:
            continue
        pkg = _read_pkg_name(d)
        crates.append((crate, pkg))
    return crates


def build_ir_oracle(
    *,
    crates: Sequence[Tuple[str, str]],
    drivers_dir: Path,
    ir_out_dir: Path,
    off_target_base: Path,
    thin_target_base: Path,
    toolchain: str,
    target: str,
    opt: Path,
    clean: bool,
) -> None:
    ir_out_dir.mkdir(parents=True, exist_ok=True)
    off_target_base.parent.mkdir(parents=True, exist_ok=True)
    thin_target_base.parent.mkdir(parents=True, exist_ok=True)

    rustflags_common = "-C debuginfo=line-tables-only -C strip=none -C save-temps -C codegen-units=1 -C opt-level=3"

    for crate, pkg in crates:
        sys.stderr.write(f"\n== IR {crate}\n")
        crate_out = ir_out_dir / crate
        if crate_out.exists():
            shutil.rmtree(crate_out)
        crate_out.mkdir(parents=True, exist_ok=True)

        off_target_dir = off_target_base.with_name(off_target_base.name + f"__{crate}")
        thin_target_dir = thin_target_base.with_name(thin_target_base.name + f"__{crate}")
        if clean:
            shutil.rmtree(off_target_dir, ignore_errors=True)
            shutil.rmtree(thin_target_dir, ignore_errors=True)

        env_off = os.environ.copy()
        env_off.update(
            {
                "CARGO_TARGET_DIR": str(off_target_dir),
                "CARGO_INCREMENTAL": "0",
                "RUSTFLAGS": f"{rustflags_common} -C lto=off",
            }
        )
        _run(
            [
                "cargo",
                f"+{toolchain}",
                "build",
                "--locked",
                "-p",
                pkg,
                "--release",
                "--target",
                target,
            ],
            cwd=drivers_dir,
            env=env_off,
        )
        off_deps = off_target_dir / target / "release" / "deps"
        off_noopt = _collect_noopt_map(off_deps)
        if not off_noopt:
            raise SystemExit(f"No off no-opt modules found under: {off_deps}")

        env_thin = os.environ.copy()
        env_thin.update(
            {
                "CARGO_TARGET_DIR": str(thin_target_dir),
                "CARGO_INCREMENTAL": "0",
                "RUSTFLAGS": f"{rustflags_common} -C lto=thin -C embed-bitcode=yes",
            }
        )
        _run(
            [
                "cargo",
                f"+{toolchain}",
                "build",
                "--locked",
                "-p",
                pkg,
                "--release",
                "--target",
                target,
            ],
            cwd=drivers_dir,
            env=env_thin,
        )
        thin_deps = thin_target_dir / target / "release" / "deps"
        thin_noopt = _collect_noopt_map(thin_deps)
        thin_after = _collect_thin_after_import_map(thin_deps)
        if not thin_noopt:
            raise SystemExit(f"No thin no-opt modules found under: {thin_deps}")
        if not thin_after:
            raise SystemExit(f"No thin after-import modules found under: {thin_deps}")

        comparable_keys = sorted(set(off_noopt.keys()) & set(thin_noopt.keys()))
        if not comparable_keys:
            raise SystemExit(f"No comparable modules for crate: {crate}")

        ok_keys: List[str] = []
        bad_keys: List[str] = []
        modules_tsv = crate_out / "modules.tsv"
        with modules_tsv.open("w", encoding="utf-8") as f:
            f.write("key\toff_noopt\tthin_noopt\tthin_after_import\toff_sha256\n")
            for k in comparable_keys:
                off_p = off_noopt[k]
                thin_p = thin_noopt[k]
                off_sha = _sha256(off_p)
                thin_sha = _sha256(thin_p)
                if off_sha != thin_sha:
                    bad_keys.append(k)
                    continue
                after_p = thin_after.get(k)
                if after_p is None:
                    continue
                ok_keys.append(k)
                f.write(f"{k}\t{off_p}\t{thin_p}\t{after_p}\t{off_sha}\n")
        if bad_keys:
            raise SystemExit(f"Node mismatch in {crate}, first keys: {bad_keys[:10]}")
        if not ok_keys:
            raise SystemExit(f"No valid comparable keys left for crate: {crate}")

        off_jsonl = crate_out / "oracle.off.O3.jsonl"
        thin_jsonl = crate_out / "oracle.thin.after_import.O3.jsonl"
        for p in [off_jsonl, thin_jsonl]:
            if p.exists():
                p.unlink()

        for k in sorted(off_noopt.keys()):
            _run([str(opt), f"-pass-oracle={off_jsonl}", "-O3", str(off_noopt[k]), "-o", "/dev/null"], cwd=REPO_ROOT)

        for k in sorted(thin_after.keys()):
            _run([str(opt), f"-pass-oracle={thin_jsonl}", "-O3", str(thin_after[k]), "-o", "/dev/null"], cwd=REPO_ROOT)

        thin_noopt_oracle = crate_out / "oracle.thin.noopt.O3.jsonl"
        try:
            if thin_noopt_oracle.exists():
                thin_noopt_oracle.unlink()
            thin_noopt_oracle.symlink_to(Path("oracle.off.O3.jsonl"))
        except Exception:
            shutil.copyfile(off_jsonl, thin_noopt_oracle)

        reduced = _reduce_jsonl_to_reason_map([off_jsonl], [thin_jsonl], repo_root=REPO_ROOT)
        _write_loc_reason(crate_out / "loc_reason.csv", reduced)


def build_machine_and_merge(
    *,
    crates: Sequence[Tuple[str, str]],
    ir_out_dir: Path,
    machine_out_dir: Path,
    combined_out_dir: Path,
    llc: Path,
    target: str,
) -> None:
    machine_out_dir.mkdir(parents=True, exist_ok=True)
    combined_out_dir.mkdir(parents=True, exist_ok=True)

    for crate, _pkg in crates:
        sys.stderr.write(f"\n== Machine {crate}\n")
        crate_ir = ir_out_dir / crate
        modules_tsv = crate_ir / "modules.tsv"
        if not modules_tsv.is_file():
            raise SystemExit(f"Missing modules.tsv for crate {crate}: {modules_tsv}")

        off_mods, thin_deps = _read_modules_tsv(modules_tsv)
        thin_mods = sorted(thin_deps.glob("*.thin-lto-after-import.bc"))
        if not thin_mods:
            raise SystemExit(f"No thin after-import modules found under: {thin_deps}")

        crate_machine = machine_out_dir / crate
        if crate_machine.exists():
            shutil.rmtree(crate_machine)
        crate_machine.mkdir(parents=True, exist_ok=True)

        off_jsonl = crate_machine / "oracle.machine.off.O3.jsonl"
        thin_jsonl = crate_machine / "oracle.machine.thin.after_import.O3.jsonl"
        for p in [off_jsonl, thin_jsonl]:
            if p.exists():
                p.unlink()

        for bc in off_mods:
            if not bc.is_file():
                raise SystemExit(f"Missing bitcode: {bc}")
            _run(
                [
                    str(llc),
                    "-O3",
                    "-filetype=null",
                    f"-mtriple={target}",
                    f"-machine-pass-oracle={off_jsonl}",
                    str(bc),
                    "-o",
                    "/dev/null",
                ],
                cwd=REPO_ROOT,
            )

        for bc in thin_mods:
            if not bc.is_file():
                raise SystemExit(f"Missing bitcode: {bc}")
            _run(
                [
                    str(llc),
                    "-O3",
                    "-filetype=null",
                    f"-mtriple={target}",
                    f"-machine-pass-oracle={thin_jsonl}",
                    str(bc),
                    "-o",
                    "/dev/null",
                ],
                cwd=REPO_ROOT,
            )

        machine_map = _reduce_jsonl_to_reason_map([off_jsonl], [thin_jsonl], repo_root=REPO_ROOT)
        _write_loc_reason(crate_machine / "loc_reason.csv", machine_map)

        merged: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))
        ir_map = _read_loc_reason(crate_ir / "loc_reason.csv")
        mach_map = _read_loc_reason(crate_machine / "loc_reason.csv")
        for loc, reason_map in ir_map.items():
            for reason, diff in reason_map.items():
                merged[loc][reason] += int(diff)
        for loc, reason_map in mach_map.items():
            for reason, diff in reason_map.items():
                merged[loc][reason] += int(diff)

        merged_out: Dict[str, Dict[str, int]] = {}
        for loc, reason_map in merged.items():
            cleaned = {r: int(v) for r, v in reason_map.items() if int(v) != 0}
            if cleaned:
                merged_out[loc] = cleaned

        crate_combined = combined_out_dir / crate
        if crate_combined.exists():
            shutil.rmtree(crate_combined)
        crate_combined.mkdir(parents=True, exist_ok=True)
        _write_loc_reason(crate_combined / "loc_reason.csv", merged_out)


def align_to_puzzle(
    *,
    crates: Sequence[Tuple[str, str]],
    puzzle_dir: Path,
    oracle_in_dir: Path,
    final_out_dir: Path,
) -> None:
    if not puzzle_dir.is_dir():
        raise SystemExit(f"Puzzle dir does not exist: {puzzle_dir}")

    final_out_dir.mkdir(parents=True, exist_ok=True)

    for crate, _pkg in crates:
        loc_diff_tsv = puzzle_dir / crate / "loc_diff.tsv"
        oracle_csv = oracle_in_dir / crate / "loc_reason.csv"
        if not loc_diff_tsv.is_file():
            raise SystemExit(f"Missing puzzle loc_diff.tsv for crate {crate}: {loc_diff_tsv}")
        if not oracle_csv.is_file():
            raise SystemExit(f"Missing oracle loc_reason.csv for crate {crate}: {oracle_csv}")

        puzzle_rows: Dict[str, Tuple[int, str]] = {}
        with loc_diff_tsv.open(newline="") as f:
            r = csv.DictReader(f, delimiter="\t")
            for row in r:
                loc = (row.get("loc") or "").strip()
                if not loc:
                    continue
                try:
                    score = int(row.get("score", "0") or 0)
                except Exception:
                    score = 0
                if loc in puzzle_rows and puzzle_rows[loc][0] >= score:
                    continue
                path = loc.rsplit(":", 2)[0] if ":" in loc else loc
                puzzle_rows[loc] = (score, _path_category(path))

        oracle_map = _read_loc_reason(oracle_csv)
        matched = sorted(set(puzzle_rows.keys()) & set(oracle_map.keys()))
        uncovered = sorted(set(puzzle_rows.keys()) - set(oracle_map.keys()))

        crate_out = final_out_dir / crate
        if crate_out.exists():
            shutil.rmtree(crate_out)
        crate_out.mkdir(parents=True, exist_ok=True)

        matched_map: Dict[str, Dict[str, int]] = {loc: oracle_map[loc] for loc in matched}
        _write_loc_reason(crate_out / "loc_reason.csv", matched_map)

        with (crate_out / "uncovered.tsv").open("w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["loc", "category", "binary_score", "sample_locs"])
            uncovered_sorted = sorted(uncovered, key=lambda loc: (-puzzle_rows[loc][0], loc))
            for loc in uncovered_sorted:
                score, cat = puzzle_rows[loc]
                w.writerow([loc, cat, str(score), json.dumps([loc], separators=(",", ":"))])


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--toolchain", default="1.92.0-aarch64-apple-darwin")
    ap.add_argument("--target", default="wasm32-wasip1")
    ap.add_argument("--drivers-dir", default=str(REPO_ROOT / "cache" / "drivers"))
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--clean", action="store_true")

    ap.add_argument("--opt", default=str(REPO_ROOT / "cache" / "_llvm" / "build" / "bin" / "opt"))
    ap.add_argument("--llc", default=str(REPO_ROOT / "cache" / "_llvm" / "build" / "bin" / "llc"))
    ap.add_argument("--off-target-dir", default=str(REPO_ROOT / "cache" / "_oracle_build_off" / "target"))
    ap.add_argument("--thin-target-dir", default=str(REPO_ROOT / "cache" / "_oracle_build_thin" / "target"))

    ap.add_argument("--ir-out-dir", default=str(REPO_ROOT / "cache" / "_oracle_ir_lto_thin"))
    ap.add_argument("--machine-out-dir", default=str(REPO_ROOT / "cache" / "_oracle_machine_lto_thin"))
    ap.add_argument("--combined-out-dir", default=str(REPO_ROOT / "cache" / "_oracle_combined_lto_thin"))
    ap.add_argument("--puzzle-dir", default=str(REPO_ROOT / "cache" / "riddle" / "1.92.0-debug-lto-off-cg1__vs__1.92.0-debug-lto-cg1"))
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "cache" / "oracle" / "lto-thin"))

    ap.add_argument("--skip-ir", action="store_true", help="Skip IR oracle build")
    ap.add_argument("--skip-machine", action="store_true", help="Skip machine oracle build/merge")
    ap.add_argument("--skip-puzzle", action="store_true", help="Skip puzzle alignment")
    return ap.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)

    drivers_dir = Path(args.drivers_dir).resolve()
    opt = Path(args.opt).resolve()
    llc = Path(args.llc).resolve()
    off_target_base = Path(args.off_target_dir).resolve()
    thin_target_base = Path(args.thin_target_dir).resolve()
    ir_out_dir = Path(args.ir_out_dir).resolve()
    machine_out_dir = Path(args.machine_out_dir).resolve()
    combined_out_dir = Path(args.combined_out_dir).resolve()
    puzzle_dir = Path(args.puzzle_dir).resolve()
    out_dir = Path(args.out_dir).resolve()

    if not drivers_dir.is_dir():
        raise SystemExit(f"Not a directory: {drivers_dir}")
    if not args.skip_ir and not opt.is_file():
        raise SystemExit(f"Missing opt: {opt}")
    if (not args.skip_machine) and (not llc.is_file()):
        raise SystemExit(f"Missing llc: {llc}")

    crates = _discover_crates(drivers_dir, args.only)
    if not crates:
        raise SystemExit("No drivers found (check --drivers-dir / --only).")

    if not args.skip_ir:
        build_ir_oracle(
            crates=crates,
            drivers_dir=drivers_dir,
            ir_out_dir=ir_out_dir,
            off_target_base=off_target_base,
            thin_target_base=thin_target_base,
            toolchain=args.toolchain,
            target=args.target,
            opt=opt,
            clean=args.clean,
        )

    if not args.skip_machine:
        build_machine_and_merge(
            crates=crates,
            ir_out_dir=ir_out_dir,
            machine_out_dir=machine_out_dir,
            combined_out_dir=combined_out_dir,
            llc=llc,
            target=args.target,
        )

    if not args.skip_puzzle:
        oracle_src = combined_out_dir if not args.skip_machine else ir_out_dir
        align_to_puzzle(
            crates=crates,
            puzzle_dir=puzzle_dir,
            oracle_in_dir=oracle_src,
            final_out_dir=out_dir,
        )

    sys.stderr.write(f"\nDone. Final output: {out_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
