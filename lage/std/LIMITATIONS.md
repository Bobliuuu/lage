# Limitations found while writing the stdlib

No blocking bugs were found; these are things to know.

- Tests cannot assert that an expression raises. `(test ...)` has no try/catch, so the `E_ASSERT` behaviour of
  `result/unwrap` on an err is covered by `tests/test_std.py` (runs a snippet and checks the error code), not by an
  in-module `(test ...)`.
- `get-in` returns nil for both a missing path and a present nil value. Use `std/json` `get-path`
  (returns `(err ...)` on a missing step) when the difference matters.
- `fmt` rewrites long `cond` forms one clause element per line; this is canonical, just verbose.
- `min` and `max` builtins take a list, not varargs (used as `(min [a b])`).
- Non-tail helpers (`flatten`, `get-in`, `assoc-in`) recurse only as deep as the data nests, not as long as the data is.

## type checker

- `(list a)` cannot express heterogeneous pairs, so `zip`, `enumerate`, `to-pairs`, `from-pairs` and `interleave`
  use `(list (list any))` / `(list any)` for the mixed parts.
