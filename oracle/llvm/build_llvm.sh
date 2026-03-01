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
PATCH="$ROOT/oracle/llvm/patches/0001-pass-oracle-jsonl.patch"
CLEAN_BUILD="${LLVM_CLEAN:-0}"

echo "[llvm] root=$ROOT"
echo "[llvm] llvm_dir=$LLVM_DIR"
echo "[llvm] build_dir=$BUILD_DIR"
echo "[llvm] patch=$PATCH"

mkdir -p "$ROOT/cache/_llvm"

if [[ ! -d "$LLVM_DIR/.git" ]]; then
  echo "[llvm] cloning llvm-project..."
  git clone --depth 1 --branch "$TAG" https://github.com/llvm/llvm-project.git "$LLVM_DIR"
else
  echo "[llvm] llvm-project already exists; skipping clone"
fi

if [[ -f "$PATCH" ]]; then
  echo "[llvm] applying patch..."
  if git -C "$LLVM_DIR" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
    echo "[llvm] patch already applied; skipping"
  else
    git -C "$LLVM_DIR" apply "$PATCH"
  fi
else
  echo "[llvm] warning: patch file missing: $PATCH"
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
    -DLLVM_ENABLE_PROJECTS="" \
    -DLLVM_TARGETS_TO_BUILD="WebAssembly;AArch64" \
    -DLLVM_ENABLE_ASSERTIONS=ON
else
  echo "[llvm] build dir already configured; skipping cmake"
fi

echo "[llvm] build..."
ninja -C "$BUILD_DIR" opt llc llvm-dis

echo "[llvm] done"

