# lage standard library

Pure-lage modules bundled with the interpreter. Import with `(import "std/list" :as l)` and call `(l/take 2 xs)`.
Higher-order functions take the function first. Maps use string keys (`:a` and `"a"` are the same key).
Run a module's own tests with `python3 -m lage test lage/std/list.lg`.

## std/list

| Function | Description |
|---|---|
| `take n xs` / `drop n xs` | first n elements / the rest |
| `take-while f xs` / `drop-while f xs` | prefix while f holds / the rest |
| `zip xs ys` / `zip-with f xs ys` | pairs (or `f x y`) up to the shorter list |
| `enumerate xs` | `[[i x] ...]` |
| `flatten xs` | deep flatten nested lists |
| `concat-map f xs` | map to lists and concatenate |
| `partition f xs` | `[matching others]` |
| `group-by f xs` | map of `(str (f x))` to elements |
| `uniq xs` | drop duplicates, keep first |
| `count f xs` | number of matches |
| `find f xs` | first match or nil |
| `any? f xs` / `all? f xs` / `none? f xs` | quantifiers |
| `sum-by f xs` | sum of `(f x)` |
| `min-by f xs` / `max-by f xs` | element with smallest/largest key (nil for `[]`) |
| `last-n n xs` | last n elements |
| `chunk n xs` | lists of n (requires n > 0) |
| `interleave xs ys` | alternate elements, leftovers appended |
| `repeat n x` | n copies |
| `index-by f xs` | map of `(str (f x))` to x, later wins |

## std/str

| Function | Description |
|---|---|
| `pad-left s n ch` / `pad-right s n ch` | pad to length n |
| `repeat-str s n` | repeat a string |
| `lines s` / `unlines ls` | split / join on newlines |
| `words s` / `unwords ws` | split on whitespace / join with spaces |
| `capitalize s` | upper-case first character |
| `center s n ch` | pad both sides |
| `truncate s n` | at most n chars, ends in `...` |
| `surround s left right` | wrap a string |
| `count-substr s sub` | non-overlapping occurrences |

## std/map

| Function | Description |
|---|---|
| `merge a b` / `merge-all ms` | union, later wins |
| `select-keys m ks` | keep listed keys |
| `map-vals f m` / `filter-vals f m` / `map-keys f m` | transform entries |
| `update m k f` | set k to `(f current)` |
| `get-in m path` | nested lookup through maps and lists, nil if missing |
| `assoc-in m path v` | nested set, creating maps |
| `from-pairs ps` / `to-pairs m` | convert to and from `[[k v] ...]` |
| `invert m` | swap keys and values (values via `str`) |

## std/math

| Function | Description |
|---|---|
| `clamp x lo hi` | limit to a range |
| `gcd a b` / `lcm a b` | integer gcd and lcm |
| `sign x` / `even? n` / `odd? n` | basic predicates |
| `factorial n` | n! (tail recursive) |
| `fib-iter n` | n-th Fibonacci (tail recursive) |
| `digits n` | decimal digits as a list |
| `mean xs` / `median xs` | statistics of a non-empty number list |
| `divmod a b` | `[quotient remainder]`, floor semantics |

## std/result

| Function | Description |
|---|---|
| `map-ok f r` / `map-err f r` | transform the ok or err value |
| `and-then f r` / `or-else f r` | chain result-returning functions |
| `collect rs` | list of results to result of list (first err wins) |
| `all-ok? rs` | every result is ok |
| `unwrap r` | value of ok; raises `E_ASSERT` on err |
| `partition-results rs` | `{:ok [..] :err [..]}` |

## std/json

| Function | Description |
|---|---|
| `pretty v` | two-space indented JSON text |
| `get-path data path` | `(ok v)` or `(err msg)` following keys and indexes |
