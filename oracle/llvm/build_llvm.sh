#!/usr/bin/env bash
set -euo pipefail

# Build a patched LLVM under cache/_llvm (gitignored).
#
# Usage:
#   ./oracle/llvm/build_llvm.sh
#
# If you want a clean rebuild:
#   LLVM_CLEAN=1 ./oracle/llvm/build_llvm.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LLVM_DIR="$ROOT/cache/_llvm/llvm-project"
BUILD_DIR="$ROOT/cache/_llvm/build"

TAG="llvmorg-21.1.3"
PATCH_DIR="$ROOT/oracle/llvm/patches"
CLEAN_BUILD="${LLVM_CLEAN:-0}"

echo "[llvm] root=$ROOT"
echo "[llvm] llvm_dir=$LLVM_DIR"
echo "[llvm] build_dir=$BUILD_DIR"
echo "[llvm] patch_dir=$PATCH_DIR"

mkdir -p "$ROOT/cache/_llvm"

if [[ ! -d "$LLVM_DIR/.git" ]]; then
  echo "[llvm] cloning llvm-project..."
  git clone --depth 1 --branch "$TAG" https://github.com/llvm/llvm-project.git "$LLVM_DIR"
else
  echo "[llvm] llvm-project already exists; skipping clone"
fi

if [[ -d "$PATCH_DIR" ]]; then
  for PATCH in "$PATCH_DIR"/*.patch; do
    if [[ ! -f "$PATCH" ]]; then
      continue
    fi
    echo "[llvm] applying patch: $PATCH"
    if git -C "$LLVM_DIR" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
      echo "[llvm] patch already applied; skipping"
    else
      git -C "$LLVM_DIR" apply "$PATCH"
    fi
  done
else
  echo "[llvm] warning: patch dir missing: $PATCH_DIR"
fi

echo "[llvm] preparing build dir..."
if [[ "$CLEAN_BUILD" == "1" ]]; then
  echo "[llvm] LLVM_CLEAN=1: removing build dir"
  rm -rf "$BUILD_DIR"
fi
mkdir -p "$BUILD_DIR"

if [[ ! -f "$BUILD_DIR/build.ninja" ]]; then
  echo "[llvm] configure..."

  cmake -S "$LLVM_DIR/llvm" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_ENABLE_PROJECTS="lld" \
    -DLLVM_TARGETS_TO_BUILD="WebAssembly;AArch64" \
    -DLLVM_ENABLE_ASSERTIONS=ON
else
  echo "[llvm] build dir already configured; skipping cmake"
fi

echo "[llvm] build..."
ninja -C "$BUILD_DIR" opt llc llvm-dis lld

# LLVM's build typically produces a multi-call `lld` binary; create a stable
# `wasm-ld` entrypoint for rustc to invoke.
if [[ -x "$BUILD_DIR/bin/lld" ]]; then
  ln -sf "lld" "$BUILD_DIR/bin/wasm-ld"
fi

echo "[llvm] done"
