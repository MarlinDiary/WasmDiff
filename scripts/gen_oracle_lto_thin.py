#!/usr/bin/env python3
"""
Generate a "full" LLVM pass-oracle dataset for our driver workspace:

  baseline:  -C lto=off  (cg1)
  variant:   -C lto=thin (cg1), using the ThinLTO after-import bitcode

Outputs (per crate) under:
  cache/oracle/lto-thin/<crate>/
    oracle.off.O3.jsonl
    oracle.thin.after_import.O3.jsonl
    loc_reason.csv

This script intentionally runs the oracle in STRICT mode (no effective-loc).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


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
            # name = "wasmdiff_drv_xxx"
            parts = s.split("=", 1)
            if len(parts) != 2:
                continue
            rhs = parts[1].strip()
            if rhs.startswith('"') and rhs.endswith('"'):
                return rhs.strip('"')
    raise SystemExit(f"Failed to parse package name from: {toml}")


def _run(cmd: List[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> None:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout)
        sys.stderr.write(p.stderr)
        raise SystemExit(f"Command failed ({p.returncode}): {' '.join(cmd)}")


def _newest(path_glob: str) -> Path:
    # glob() does not expand braces like the shell; we just use it for a single pattern.
    files = list(Path().glob(path_glob))
    if not files:
        raise SystemExit(f"No files matched: {path_glob}")
    return max(files, key=lambda p: p.stat().st_mtime)


def _newest_in(dir_path: Path, pattern: str) -> Path:
    files = list(dir_path.glob(pattern))
    if not files:
        raise SystemExit(f"No files matched: {dir_path}/{pattern}")
    return max(files, key=lambda p: p.stat().st_mtime)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--toolchain",
        default="1.92.0-aarch64-apple-darwin",
        help="rustup toolchain to use (default: 1.92.0-aarch64-apple-darwin)",
    )
    ap.add_argument("--target", default="wasm32-wasip1", help="Rust target triple (default: wasm32-wasip1)")
    ap.add_argument(
        "--drivers-dir",
        default=str(REPO_ROOT / "cache" / "drivers"),
        help="Driver workspace dir (default: cache/drivers)",
    )
    ap.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "cache" / "oracle" / "lto-thin"),
        help="Output directory (default: cache/oracle/lto-thin)",
    )
    ap.add_argument(
        "--off-target-dir",
        default=str(REPO_ROOT / "cache" / "_oracle_build_off" / "target"),
        help="Cargo target dir for baseline/off build",
    )
    ap.add_argument(
        "--thin-target-dir",
        default=str(REPO_ROOT / "cache" / "_oracle_build_thin" / "target"),
        help="Cargo target dir for thin build",
    )
    ap.add_argument(
        "--opt",
        default=str(REPO_ROOT / "cache" / "_llvm" / "build" / "bin" / "opt"),
        help="Patched LLVM opt path (default: cache/_llvm/build/bin/opt)",
    )
    ap.add_argument("--clean", action="store_true", help="Delete off/thin cargo target dirs before building")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only run for these crate names (repeatable), e.g. --only aes --only anyhow",
    )
    args = ap.parse_args(argv)

    drivers_dir = Path(args.drivers_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    off_target_dir = Path(args.off_target_dir).resolve()
    thin_target_dir = Path(args.thin_target_dir).resolve()
    opt = Path(args.opt).resolve()

    if not drivers_dir.is_dir():
        raise SystemExit(f"Not a directory: {drivers_dir}")
    if not opt.is_file():
        raise SystemExit(f"Missing opt: {opt}")

    if args.clean:
        shutil.rmtree(off_target_dir, ignore_errors=True)
        shutil.rmtree(thin_target_dir, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    off_target_dir.mkdir(parents=True, exist_ok=True)
    thin_target_dir.mkdir(parents=True, exist_ok=True)

    # Discover drivers.
    crates: List[Tuple[str, str, Path]] = []
    for d in sorted(drivers_dir.glob("drv_*")):
        if not d.is_dir():
            continue
        crate = d.name[len("drv_") :]
        if args.only and crate not in set(args.only):
            continue
        pkg = _read_pkg_name(d)
        crates.append((crate, pkg, d))

    if not crates:
        raise SystemExit("No drivers found (check --drivers-dir / --only).")

    rustflags_common = "-C debuginfo=line-tables-only -C strip=none -C save-temps -C embed-bitcode=yes -C codegen-units=1 -C opt-level=3"

    for crate, pkg, _drv_dir in crates:
        sys.stderr.write(f"\n== {crate}\n")
        crate_out = out_dir / crate
        if crate_out.exists():
            shutil.rmtree(crate_out)
        crate_out.mkdir(parents=True, exist_ok=True)

        # 1) Build baseline (lto=off) and pick the newest export_main no-opt bitcode.
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
                f"+{args.toolchain}",
                "build",
                "--locked",
                "-p",
                pkg,
                "--release",
                "--target",
                args.target,
            ],
            cwd=drivers_dir,
            env=env_off,
        )

        off_deps = off_target_dir / args.target / "release" / "deps"
        off_bc = _newest_in(off_deps, "export_main-*.export_main.*-cgu.0.rcgu.no-opt.bc")
        _run(
            [
                str(opt),
                f"-pass-oracle={crate_out / 'oracle.off.O3.jsonl'}",
                "-O3",
                str(off_bc),
                "-o",
                "/dev/null",
            ],
            cwd=REPO_ROOT,
        )

        # 2) Build variant (lto=thin) and pick the newest export_main after-import bitcode.
        env_thin = os.environ.copy()
        env_thin.update(
            {
                "CARGO_TARGET_DIR": str(thin_target_dir),
                "CARGO_INCREMENTAL": "0",
                "RUSTFLAGS": f"{rustflags_common} -C lto=thin",
            }
        )
        _run(
            [
                "cargo",
                f"+{args.toolchain}",
                "build",
                "--locked",
                "-p",
                pkg,
                "--release",
                "--target",
                args.target,
            ],
            cwd=drivers_dir,
            env=env_thin,
        )

        thin_deps = thin_target_dir / args.target / "release" / "deps"
        thin_bc = _newest_in(thin_deps, "export_main-*.export_main.*-cgu.0.rcgu.thin-lto-after-import.bc")
        _run(
            [
                str(opt),
                f"-pass-oracle={crate_out / 'oracle.thin.after_import.O3.jsonl'}",
                "-O3",
                str(thin_bc),
                "-o",
                "/dev/null",
            ],
            cwd=REPO_ROOT,
        )

        # 3) Reduce into loc_reason.csv (+ minified mapping).
        _run(
            [
                "python3",
                "scripts/oracle_jsonl_loc_reason.py",
                "--a",
                str(crate_out / "oracle.off.O3.jsonl"),
                "--b",
                str(crate_out / "oracle.thin.after_import.O3.jsonl"),
                "--out",
                str(crate_out / "loc_reason.csv"),
                "--repo-root",
                str(REPO_ROOT),
            ],
            cwd=REPO_ROOT,
        )

    sys.stderr.write(f"\nWrote oracle dataset under: {out_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
