# LLVM Pass Oracle (Patch + Workflow)

This folder contains a small patch to LLVM that adds a **structured, pass-level IR oracle**
output as JSONL.

The intent is to answer questions like:

- which LLVM pass changed what
- where (function + debug location, when available)
- by how much (instruction/call/branch/const deltas, etc.)

## Build (patched LLVM)

```bash
./oracle/llvm/build_llvm.sh
```

Tools are built under:

- `cache/_llvm/build/bin/opt`
- `cache/_llvm/build/bin/llc`
- `cache/_llvm/build/bin/llvm-dis`
- `cache/_llvm/build/bin/lld` (multi-call)
- `cache/_llvm/build/bin/wasm-ld` (symlink to `lld`, wasm flavor)

## opt Usage

The patch adds this (hidden) flag:

- `-pass-oracle=<path>`: enable and append JSONL records to `<path>`

Example:

```bash
cache/_llvm/build/bin/opt \
  -pass-oracle=/tmp/oracle.jsonl \
  -O3 input.bc -o /dev/null
```

## Output (JSONL)

Each line is a JSON object for one `(pass, function)` event:

- `pass_id`, `pass_name`
- `module` (module identifier)
- `function`
- `before` / `after`:
  - `inst/call/br/const_op/alloca/load/store`
  - `dbg_loc_inst/dbg_loc_unique` (strict: only real `!dbg`)
- `delta` (when both before/after exist)
- `loc_deltas`: list of debug locations (file/line/col) whose counters changed

Limitations:

- If a function has **no debug info at all** (no `!dbg` and no `DISubprogram`), we still cannot
  map it to a real `file:line:col`. In that case `loc_deltas` will be empty.

## wasm-ld GC Oracle (Link-Time GC Evidence)

The patch set also adds a small wasm-ld / lld instrumentation that emits **GC decisions**
as JSONL. This is the "ironclad" way to explain sysroot/object-code deltas (e.g. `wasisdk://...`)
that the LLVM IR oracle cannot see.

This oracle file also includes **archive extraction evidence** (see below) so we can distinguish:

- code was present but removed by `--gc-sections`
- code was never present because the `.a/.rlib` member was never extracted

### Enable (recommended)

Use an environment variable so rustc flags (and crate disambiguators) do not change:

```bash
export LLD_GC_ORACLE_JSONL=/tmp/lld.gc.jsonl
```

Then link as usual with our `wasm-ld`:

```bash
RUSTFLAGS="-C linker=$PWD/cache/_llvm/build/bin/wasm-ld ..." cargo build ...
```

This produces logs without changing the linked output bytes.

### Enable (explicit flag)

You can also pass:

- `--gc-oracle-jsonl=<path>`

Note: if you pass this via `rustc -C link-arg=...`, it becomes part of rustc's command line
and may change crate disambiguators and the final wasm. Prefer `LLD_GC_ORACLE_JSONL`.

### Output (JSONL)

One JSON object per line:

- header: `{"kind":"gc_oracle","version":1,...}`
- roots: `{"kind":"root","reason":"entry|exported|no_strip|call_dtors",...}`
- removed chunks: `{"kind":"removed","chunk_kind":"function|segment|...","chunk":"<obj>:(<name>)"}`
- summary counters: `{"kind":"summary",...}`

## wasm-ld Archive Extraction Oracle (Archive / rlib Evidence)

When `LLD_GC_ORACLE_JSONL` (or `--gc-oracle-jsonl`) is enabled, wasm-ld will also log
each time it extracts a lazy archive member (from `.a` or `.rlib`).

This is the "ironclad" evidence for diffs where sysroot/library code is absent because
it was never extracted (not because it was extracted and then GC'd).

### Output (JSONL)

One JSON object per extraction:

- `{"kind":"extract","version":1,"output":"...","reference":"...","extracted_file":"...","symbol":"..."}`

In addition, when enabled wasm-ld will log a per-output-function mapping so
binary-side tools can join `wasm function index (;N;)` to its input origin:

- `{"kind":"func_index","version":1,"output":"...","index":134,"chunk":"...:(symbol)","input_file":"...","symbol":"..."}`
