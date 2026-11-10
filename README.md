# lage

A small programming language designed for LLM agents to write, run, and debug.
Reference implementation in Python (stdlib only). Files use the `.lg` extension.

```lisp
(defn fact [n]
  "factorial"
  (requires (>= n 0))
  (ensures (>= result 1))
  (if (<= n 1) 1 (* n (fact (- n 1)))))

(test "fact 5" (= (fact 5) 120))
```

## Why it is agent-shaped

| Agent problem | lage answer |
|---|---|
| Syntax slips (indentation, precedence, quoting) | One S-expression grammar. Whitespace-insensitive, no operators, no precedence. Brackets are the only structure, so truncated output is detected as `E_PARSE unclosed '('`. |
| Hidden side effects | Effects (`fs.read`, `fs.write`, `net`, `time`) must be declared per function **and** granted per run: `--allow fs.read`. `--json` reports `effects_used`. |
| "Does it work?" needs a harness | `requires` / `ensures` contracts and `(test ...)` forms live beside the code. `lage test` reports `left` / `right` on failed `=`. |
| Free-text stack traces | Every failure is a JSON diagnostic: `code`, `line`, `col`, `source`, `hint` (e.g. `did you mean: print`), compressed `stack`. |
| One error per run, slow fix loops | `lage check` reports **every** unbound name (with suggestions), wrong arity, undeclared effect, bad form and bad import in one pass, before anything executes. `run` and `test` do this first. |
| Wrong-type bugs that only show at runtime | Optional gradual types: `(sig [int (list str)] (result int str))`. Generics via lowercase type variables, `nil`-safety (`(if c 1 nil)` is not an `int`), result/`?`/`match` checking, builtin signatures. Unannotated code is never an error source; types are static only. |
| Runaway loops | Fuel budget (`--fuel`) and a call-depth cap. `E_FUEL` / `E_DEPTH`, never a hang. Tail calls run in constant depth. |
| Growing past one file | `(import "lib/m.lg" :as m)` with `m/name` calls. Imports are confined to the entry file's directory, cycles are errors, `_private` names stay private, the checker follows imports. |
| Variadic / optional params | `(defn f [a (b 10) & more] ...)`: `(b 10)` is optional (default evaluated per call, sees earlier params), `& more` collects the rest into a list; `(sig [int int & str] int)` types them. |
| Exceptions to forget | No exceptions. Fallible builtins return `(ok v)` / `(err e)`; `(? r)` propagates. |
| Non-determinism | Maps keep insertion order; no hidden clocks or randomness (`now` is an effect). |
| Style drift between edits | `lage fmt` is canonical and idempotent. |
| Needing docs in context | `lage ref` prints the whole language in one screen. |

## Standard library

Pure-lage modules, bundled: `(import "std/list" :as l)` then `(l/zip xs ys)`.
`std/list`, `std/str`, `std/map`, `std/math`, `std/result`, `std/json`. Function list: `lage/std/README.md`.

## Use

```sh
python3 -m lage ref                          # the language, one screen
python3 -m lage check examples/use_lib.lg --json
python3 -m lage run examples/hello.lg
python3 -m lage test examples/hello.lg --json
python3 -m lage run examples/files.lg --allow fs.read --json
echo '(+ 1 2)' | python3 -m lage run - --json
python3 -m lage fmt examples/hello.lg --check
python3 -m unittest discover -s tests
```

`--json` on `run` prints one object, `{"ok":true,"value":..,"stdout":..,"steps":..,"effects_used":[..]}`
or `{"ok":false,"error":{"code":"E_UNBOUND","message":..,"line":1,"col":2,"hint":..}}`.
Exit codes: 0 ok, 1 runtime or test failure, 2 parse or usage error.

## Speed

The default backend transpiles lage to Python source and execs it; the closure interpreter stays as the
reference (`--backend interp`) and the two are differential-tested on 130+ programs. `python3 bench/bench.py`:

| case | interp (s) | py (s) |
|---|---|---|
| fib(27) | 2.5 | 0.06 |
| tail-recursive sum to 1e6 | 3.2 | 0.09 |
| map/filter/reduce over 200k | 0.76 | 0.085 |

## Status

v0.4: transpiling backend with tail calls and no practical nesting limit, gradual static types,
a fully type-annotated stdlib (6 modules, 71 functions), rest and optional parameters, modules,
whole-program static checker, `lage api`, formatter, test runner, 322 tests (run on both backends).
Not yet: native/bytecode compilation beyond Python, lage `fn`s nested ~100 deep (Python indentation limit).
