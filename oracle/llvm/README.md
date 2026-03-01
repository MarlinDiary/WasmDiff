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
