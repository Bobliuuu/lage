"""Benchmark the closure interpreter against the python-transpiling backend.

    python3 bench/bench.py            # table, best of 3
    python3 bench/bench.py --reps 5

Each case is run in-process (parse + compile + run), on a big-stack thread like the CLI does.
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lage.core import Interp  # noqa: E402
from lage.pyback import PyInterp  # noqa: E402


class Ref(Interp):  # subclasses are not redirected by LAGE_BACKEND
    pass


CASES = [
    ("fib(27)", "(defn fib [n] (if (< n 2) n (+ (fib (- n 1)) (fib (- n 2))))) (fib 27)", 196418),
    ("tail sum 1e6",
     "(defn sum [n acc] (if (= n 0) acc (sum (- n 1) (+ acc n)))) (sum 1000000 0)", 500000500000),
    ("map/filter/reduce 200k",
     "(reduce (fn [a x] (+ a x)) 0 (filter (fn [x] (= (mod x 3) 0)) (map (fn [x] (* x 2)) (range 200000))))",
     13333266666),
    ("string building",
     '(defn build [n acc] (if (= n 0) acc (build (- n 1) (concat acc "ab"))))'
     ' (+ (len (build 20000 "")) (len (join "," (map (fn [i] (str "item-" i)) (range 50000)))))', None),
]


def run_case(cls, src):
    out = []

    def go():
        it = cls(capture=True, fuel=10 ** 12)
        t = time.perf_counter()
        v = it.run(src)
        out.append((time.perf_counter() - t, v))
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(200000)
    threading.stack_size(256 * 1024 * 1024)
    th = threading.Thread(target=go)
    th.start()
    th.join()
    sys.setrecursionlimit(old)
    return out[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    reps = ap.parse_args().reps
    rows = []
    for name, src, want in CASES:
        res = {}
        for label, cls in (("interp", Ref), ("py", PyInterp)):
            best, val = None, None
            for _ in range(reps):
                dt, val = run_case(cls, src)
                best = dt if best is None else min(best, dt)
            res[label] = (best, val)
        if res["interp"][1] != res["py"][1] or (want is not None and res["py"][1] != want):
            print("MISMATCH in %s: interp=%r py=%r want=%r" % (name, res["interp"][1], res["py"][1], want))
            return 1
        rows.append((name, res["interp"][0], res["py"][0]))
    print("%-26s %10s %10s %9s" % ("case", "interp (s)", "py (s)", "speedup"))
    for name, a, b in rows:
        print("%-26s %10.3f %10.3f %8.1fx" % (name, a, b, a / b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
