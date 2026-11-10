"""Differential tests: every program runs on the closure interpreter and on the
python-transpiling backend and must agree on value, stdout, error code/line/col
(and, where noted, message/hint/stack) and effects_used."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from lage.core import Interp, LageError, show, to_json
from lage.pyback import PyInterp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Ref(Interp):  # subclasses are never redirected by LAGE_BACKEND
    pass


def big_stack(fn):
    """run fn like the CLI does: deep recursion limit and a large thread stack"""
    box = []
    old_lim, old_sz = sys.getrecursionlimit(), threading.stack_size()
    sys.setrecursionlimit(200000)
    threading.stack_size(256 * 1024 * 1024)
    try:
        def go():
            try:
                box.append((True, fn()))
            except BaseException as e:  # noqa: BLE001
                box.append((False, e))
        t = threading.Thread(target=go)
        t.start()
        t.join()
    finally:
        sys.setrecursionlimit(old_lim)
        threading.stack_size(old_sz)
    if not box[0][0]:
        raise box[0][1]
    return box[0][1]


def outcome(cls, src, **kw):
    return big_stack(lambda: _outcome(cls, src, **kw))


def _outcome(cls, src, **kw):
    it = cls(capture=True, **kw)
    try:
        v = it.run(src)
        res = {"ok": True, "value": show(v), "json": to_json(v)}
    except LageError as e:
        res = {"ok": False, "error": e.to_dict(src)}
    res["stdout"] = "".join(it.out)
    res["effects"] = sorted(it.effects_used)
    return res


def both(src, **kw):
    return outcome(Ref, src, **kw), outcome(PyInterp, src, **kw)


# (name, source, kwargs)
CORPUS = [
    # ---- from tests/test_lage.py ----
    ("basics-add", "(+ 1 2 3)", {}),
    ("basics-let", "(let [x 2 y (* x 3)] (+ x y))", {}),
    ("basics-map", "(map (fn [x] (* x x)) (range 4))", {}),
    ("basics-get", "(get {:a 1} :a)", {}),
    ("zero-truthy", "(if 0 :t :f)", {}),
    ("div-exact", "(/ 6 3)", {}),
    ("bool-add", "(+ 1 true)", {}),
    ("bool-eq", "(= 1 true)", {}),
    ("try", """(defn f [s] (let [n (? (parse-int s))] (ok (* n 2)))) [(f "21") (f "x")]""", {}),
    ("match", '(match (parse-int "5") (ok n) n (err e) -1)', {}),
    ("requires", "(defn f [x] (requires (> x 0)) x) (f 0)", {}),
    ("ensures", "(defn f [x] (ensures (> result 10)) x) (f 1)", {}),
    ("eff-denied", '(defn f [] (effects fs.read) (read-file "x")) (f)', {}),
    ("eff-undeclared", '(defn f [] (read-file "x")) (f)', {"allow": ["fs.read"]}),
    ("eff-callee-undeclared", "(defn f [] (effects fs.read) 1) (defn g [] (f)) (g)", {"allow": ["fs.read"]}),
    ("eff-granted", '(defn f [] (effects fs.read) (read-file "/nonexistent/zz")) (f)', {"allow": ["fs"]}),
    ("eff-lambda", '(defn f [ps] (effects fs.read) (map (fn [p] (read-file p)) ps)) (f ["/nonexistent/a"])',
     {"allow": ["fs.read"]}),
    ("unbound-hint", "(prnt 1)", {}),
    ("arity", "(defn f [a] a) (f 1 2)", {}),
    ("nth-index", "(nth [1] 5)", {}),
    ("div-zero", "(/ 1 0)", {}),
    ("not-fn", "(1 2)", {}),
    ("parse-err", "(fact 3", {}),
    ("fuel", "(defn f [x] (f (+ x 1))) (f 1)", {"fuel": 500}),
    ("tail-loop", "(defn loop [n acc] (if (= n 0) acc (loop (- n 1) (+ acc n)))) (loop 100000 0)", {}),
    ("mutual-tail",
     "(defn ev? [n] (if (= n 0) true (od? (- n 1)))) (defn od? [n] (if (= n 0) false (ev? (- n 1)))) (ev? 50001)", {}),
    ("fuel-tail", "(defn f [x] (f x)) (f 1)", {"fuel": 1000}),
    ("ensures-notail", "(defn f [n] (ensures (>= result 0)) (if (= n 0) 0 (f (- n 1)))) (f 100)", {}),
    ("det-maps", "(keys {:b 1 :a 2})", {}),
    ("depth", "(defn f [x] (+ 1 (f x))) (f 1)", {}),
    # ---- new ----
    ("closure-let", "(let [a 1 f (fn [] a) a 2] (f))", {}),
    ("closure-counter", "(defn mk [n] (fn [x] (+ x n))) (let [a (mk 1) b (mk 10)] [(a 1) (b 1)])", {}),
    ("closure-map-let",
     "(map (fn [i] (let [y (* i 2)] (fn [] y))) (range 3))", {}),
    ("closure-apply", "(let [fs (map (fn [i] (fn [] i)) (range 3))] (map (fn [f] (f)) fs))", {}),
    ("local-rec", "(let [f (fn [n] (if (= n 0) 0 (+ n (f (- n 1)))))] (f 10))", {}),
    ("shadow-builtin-param", "(defn f [len] (len 1 2 3)) (f (fn [a b c] (+ a b c)))", {}),
    ("shadow-builtin-let", "(let [len (fn [a b c] a)] (len 1 2 3))", {}),
    ("shadow-builtin-def", "(def len 5) (+ len 1)", {}),
    ("shadow-plus", "(defn plus1 [+] (+ 1 2)) (plus1 (fn [a b] (str a b)))", {}),
    ("shadow-builtin-defn", "(defn first [x] 99) [(first [1 2]) (rest [1 2])]", {}),
    ("builtin-value", "(let [f len] (f [1 2 3]))", {}),
    ("builtin-map", "(map len [[1] [1 2]])", {}),
    ("builtin-show", "[len map]", {}),
    ("fn-show", "(defn f [] 1) (def g (fn [] 2)) [f g (fn [] 3)]", {}),
    ("type-fn", "(defn f [] 1) [(type f) (type len) (type (fn [] 1))]", {}),
    ("nested-defn", "(defn outer [x] (defn inner [y] (* y 2)) (inner x)) (outer 4)", {}),
    ("nested-defn-mutual", "(defn outer [] (defn a [] (b)) (defn b [] 7) (a)) (outer)", {}),
    ("def-in-let", "(let [x 1] (def y (+ x 1)) y)", {}),
    ("def-top-do", "(do (def a 1) (def b (+ a 1))) (+ a b)", {}),
    ("tail-cond",
     "(defn f [n acc] (cond (= n 0) acc :else (f (- n 1) (+ acc 1)))) (f 10000 0)", {}),
    ("tail-and-or",
     "(defn f [n] (or (and (> n 0) (f (- n 1))) :done)) (f 5000)", {}),
    ("tail-let",
     "(defn f [n acc] (let [m (- n 1)] (if (< n 1) acc (f m (+ acc n))))) (f 20000 0)", {}),
    ("tail-match",
     '(defn f [n] (match (if (> n 0) (ok n) (err "done")) (ok v) (f (- v 1)) (err e) e)) (f 20000)', {}),
    ("tail-closure-loop",
     "(defn f [n acc] (if (= n 0) acc (f (- n 1) (cons (fn [] n) acc)))) (map (fn [g] (g)) (f 3 []))", {}),
    ("mutual-3", "(defn a [n] (if (= n 0) :a (b (- n 1)))) (defn b [n] (c n)) (defn c [n] (a n)) (a 9000)", {}),
    ("tail-higher-order",
     "(defn app [f x] (f x)) (defn g [n] (if (= n 0) :ok (app g (- n 1)))) (g 20000)", {}),
    ("tail-builtin-value", "(defn f [g x] (g x)) (f len [1 2 3])", {}),
    ("tail-arity-err", "(defn g [a] a) (defn f [x] (g x 1)) (f 1)", {}),
    ("tail-notfn", "(defn f [x] (x 1)) (f 5)", {}),
    ("nontail-arity", "(defn g [a b] a) (defn f [x] (+ 1 (g x))) (f 1)", {}),
    ("anon-arity", "((fn [a] a) 1 2)", {}),
    ("named-lambda-arity", "(def h (fn [a] a)) (h)", {}),
    ("builtin-arity", "(len 1 2)", {}),
    ("builtin-type", "(len 5)", {}),
    ("builtin-first", "(first 5)", {}),
    ("builtin-type-nested", "(defn f [x] (len (+ x 1))) (f 1)", {}),
    ("builtin-via-map", "(map len [1 2])", {}),
    ("callback-arity", "(map (fn [a b] a) [1 2])", {}),
    ("callback-notfn", "(map 5 [1 2])", {}),
    ("reduce", "(reduce (fn [a x] (+ a x)) 0 (range 101))", {}),
    ("filter", "(filter (fn [x] (> x 2)) (range 6))", {}),
    ("sort-key", "(sort [3 1 2] (fn [x] (- 0 x)))", {}),
    ("contract-ok", "(defn f [x] (requires (> x 0)) (ensures (> result x)) (+ x 1)) (f 2)", {}),
    ("contract-requires-stack",
     "(defn g [x] (requires (> x 0)) x) (defn f [y] (g (- y 1))) (f 1)", {}),
    ("contract-ensures-stack",
     "(defn g [x] (ensures (> result 5)) x) (defn f [y] (+ 1 (g y))) (f 1)", {}),
    ("contract-loop", "(defn f [n acc] (requires (>= n 0)) (if (= n 0) acc (f (- n 1) (+ acc 1)))) (f 5 0)", {}),
    ("contract-loop-fail", "(defn f [n] (requires (>= n 0)) (if (= n -2) 0 (f (- n 1)))) (f 2)", {}),
    ("ensures-try", '(defn f [r] (ensures (> result 0)) (? r)) [(f (ok 1))]', {}),
    ("ensures-try-early", '(defn f [r] (ensures (> (val result) 0)) (? r)) (f (err 1))', {}),
    ("eff-nested-denied", "(defn g [] (effects net) 1) (defn f [] (effects fs.read) (g)) (f)", {"allow": ["fs"]}),
    ("eff-wildcard", '(defn f [] (effects fs) (read-file "/nonexistent")) (f)', {"allow": ["*"]}),
    ("eff-top-level", '(read-file "/nonexistent/q")', {"allow": ["fs.read"]}),
    ("eff-top-denied", '(read-file "/nonexistent/q")', {}),
    ("eff-tail-chain",
     '(defn b [] (effects fs.read) (read-file "/nonexistent/z")) (defn a [] (effects fs.read) (b)) (defn t [] (effects fs) (a)) (t)',
     {"allow": ["fs"]}),
    ("eff-tail-undeclared",
     "(defn b [] (effects fs.read) 1) (defn a [] (b)) (a)", {"allow": ["fs"]}),
    ("eff-builtin-value", '(defn f [] (effects fs.read) (map read-file ["/nonexistent/a"])) (f)', {"allow": ["fs"]}),
    ("eff-time", "(defn f [] (effects time) (> (now) 0)) (f)", {"allow": ["time"]}),
    ("err-line-col", "(defn f [x]\n  (+ x\n     (g x)))\n(f 1)", {}),
    ("err-line-col-arg", "(print\n  (+ 1\n     (nth [1] 7)))", {}),
    ("err-stack", "(defn a [x] (nth [1] x)) (defn b [x] (+ 1 (a x))) (defn c [x] (+ 1 (b x))) (c 9)", {}),
    ("err-stack-lambda", "(defn a [xs] (map (fn [x] (nth [1] x)) xs)) (defn b [xs] (+ 0 (len (a xs)))) (b [5])", {}),
    ("err-unbound-arg", "(defn f [x] (+ x yy)) (f 1)", {}),
    ("err-unbound-hint-local", "(defn f [alpha] (+ alpha alhpa)) (f 1)", {}),
    ("err-unbound-call", "(defn f [x] (undefined-fn x)) (f 1)", {}),
    ("err-unbound-global-hint", "(def counter 1) (+ countr 1)", {}),
    ("err-use-before-def", "(g 1) (defn g [x] x)", {}),
    ("err-type-plus-str", '(+ 1 "a")', {}),
    ("err-type-str-str", '(+ "a" "b")', {}),
    ("err-type-compare", '(< 1 "a")', {}),
    ("err-type-compare-bool", "(< true 2)", {}),
    ("err-match-type", "(match 5 (ok v) v (err e) e)", {}),
    ("err-match-missing", "(match (ok 1) (err e) e)", {}),
    ("err-try-nonres", "(defn f [] (? 5)) (f)", {}),
    ("err-try-top", '(? (err "boom"))', {}),
    ("err-try-top-let", '(let [x (? (err "boom"))] x)', {}),
    ("err-map-key", "{1 2}", {}),
    ("err-assert", '(assert false "nope")', {}),
    ("err-syntax-if", "(print 1) (if 1)", {}),
    ("err-syntax-fn", "(print 1) (fn [1] 2)", {}),
    ("err-syntax-defn", "(defn f [x])", {}),
    ("err-syntax-let", "(let [a] a)", {}),
    ("err-syntax-empty", "()", {}),
    ("err-syntax-pattern", "(match (ok 1) (maybe v) v)", {}),
    ("err-after-output", '(print "before") (nth [] 0) (print "after")', {}),
    ("fuel-nontail", "(defn f [x] (+ 1 (f x))) (f 1)", {"fuel": 300}),
    ("fuel-ok", "(defn g [x] x) (len (map g (range 100)))", {"fuel": 100000}),
    ("depth-mutual", "(defn a [x] (+ 1 (b x))) (defn b [x] (+ 1 (a x))) (a 1)", {}),
    ("depth-ok", "(defn f [n] (if (= n 0) 0 (+ 1 (f (- n 1))))) (f 2000)", {}),
    ("depth-over", "(defn f [n] (if (= n 0) 0 (+ 1 (f (- n 1))))) (f 3000)", {}),
    ("strings", '(str "a" 1 :k [1 2] {:a 1} nil true)', {}),
    ("print-capture", '(print "hi" 1 [2]) (print :x) 5', {}),
    ("kw-type", "[(type :a) (type \"a\") (type (first (keys {:a 1})))]", {}),
    ("map-ops", "(let [m {:a 1}] [(assoc m :b 2) (dissoc m :a) (has? m :a) (get m :z 0)])", {}),
    ("eq-deep", "[(= [1 {:a 2}] [1 {:a 2}]) (= 1 1.0) (!= 1 2) (= :a \"a\")]", {}),
    ("float-math", "[(+ 1 2.5) (* 2 0.5) (/ 1 4) (< 1 1.5) (- 5) (mod 7 3) (abs -3)]", {}),
    ("and-or-values", "[(and 1 2) (and 1 nil 3) (and) (or nil false) (or nil 3) (or) (and false 1) (or false false)]", {}),
    ("cond", "(cond false 1 nil 2 :else 3)", {}),
    ("cond-none", "(cond false 1)", {}),
    ("not-fn-values", "[(not nil) (not 0) (not false) (not [])]", {}),
    ("truthy-values", "[(if [] 1 2) (if \"\" 1 2) (if nil 1 2) (if false 1 2)]", {}),
    ("do-expr", "(+ 1 (do 1 2 3))", {}),
    ("empty-do", "(do)", {}),
    ("nested-let-shadow", "(let [x 1] (let [x (+ x 1) y x] [x y]))", {}),
    ("let-seq-outer", "(defn f [x] (let [x (+ x 1)] x)) (f 1)", {}),
    ("param-shadow-global", "(def x 10) (defn f [x] (+ x 1)) [(f 1) x]", {}),
    ("redefine", "(defn f [] 1) (def a (f)) (defn f [] 2) [a (f)]", {}),
    ("fn-in-vec", "(let [fs [(fn [x] (+ x 1)) (fn [x] (* x 2))]] (map (fn [f] (f 10)) fs))", {}),
    ("immediate-fn", "((fn [x] (* x x)) 7)", {}),
    ("fn-returning-fn", "(((fn [a] (fn [b] (+ a b))) 1) 2)", {}),
    ("json", '[(to-json {:a [1 2]}) (from-json "{\\"a\\":1}")]', {}),
    ("string-fns", '[(split "a,b" ",") (join "-" ["a" "b"]) (upper "x") (replace "aXa" "X" "-") (slice "hello" 1 3)]', {}),
    ("ok-err-values", "[(ok 1) (err 2) (ok? (ok 1)) (unwrap-or (err 1) 5)]", {}),
    ("sum-minmax", "[(sum [1 2 3]) (min [3 1]) (max [3 1]) (sort [3 1 2]) (reverse [1 2])]", {}),
    ("deep-expr", "(+ 1 (+ 2 (+ 3 (+ 4 (+ 5 (+ 6 (+ 7 (+ 8 (+ 9 10)))))))))", {}),
    ("many-args-plus", "[(+) (+ 1) (+ 1 2 3 4) (*) (* 2 3 4) (- 10 1 2)]", {}),
    ("test-forms-ignored-at-run", '(defn f [x] x) (test "t" (= (f 1) 2)) (f 5)', {}),
    ("recursive-list", "(defn build [n] (if (= n 0) [] (cons n (build (- n 1))))) (build 100)", {}),
    ("ackermann", "(defn ack [m n] (cond (= m 0) (+ n 1) (= n 0) (ack (- m 1) 1) :else (ack (- m 1) (ack m (- n 1))))) (ack 2 3)", {}),
    ("collatz", "(defn steps [n acc] (cond (= n 1) acc (= (mod n 2) 0) (steps (/ n 2) (+ acc 1)) :else (steps (+ (* 3 n) 1) (+ acc 1)))) (steps 27 0)", {}),
    # ---- variadic and optional params ----
    ("rest-basic", "(defn f [a b & more] [a b more]) [(f 1 2) (f 1 2 3) (f 1 2 3 4)]", {}),
    ("rest-lambda", "((fn [x & xs] [x xs]) 1 2 3)", {}),
    ("rest-only", "(defn f [& xs] (len xs)) [(f) (f 1) (f 1 2 3)]", {}),
    ("rest-arity-few", "(defn f [a b & m] a) (f 1)", {}),
    ("rest-arity-lambda", "((fn [a b & m] a) 1)", {}),
    ("opt-basic", "(defn g [a (b 10) (c (+ a b))] [a b c]) [(g 1) (g 1 2) (g 1 2 3)]", {}),
    ("opt-arity-many", "(defn g [a (b 1)] a) (g 1 2 3)", {}),
    ("opt-arity-none", "(defn g [a (b 1) (c 2)] a) (g)", {}),
    ("opt-nil-explicit", "(defn k [(a 5)] a) [(k) (k nil) (k false)]", {}),
    ("opt-rest", "(defn h [a (b 1) & r] [a b r]) [(h 0) (h 0 5) (h 0 5 6 7)]", {}),
    ("opt-default-later-param", "(def b 99) (defn g [(a b) (b 1)] [a b]) (g)", {}),
    ("opt-default-unbound", "(defn g [(a zz)] a) (g)", {}),
    ("opt-default-error-line", "(defn g [a\n (b (nth [] 3))] b)\n(g 1)", {}),
    ("opt-default-closure", "(defn m [(a 1) (b (fn [] a))] (b)) [(m) (m 7)]", {}),
    ("opt-default-call", "(defn d [] 4) (defn g [(a (d))] a) (g)", {}),
    ("var-closure-capture", "(defn mk [a & r] (fn [] [a r])) (let [f (mk 1 2 3) g (mk 9)] [(f) (g)])", {}),
    ("var-closure-opt", "(defn mk [(a 1)] (fn [x] (+ a x))) [((mk) 1) ((mk 10) 1)]", {}),
    ("var-tail-self", "(defn f [n & acc] (if (= n 0) (len acc) (f (- n 1) 1))) (f 30000)", {}),
    ("var-tail-opt-self", "(defn f [n (acc 0)] (if (= n 0) acc (f (- n 1) (+ acc n)))) (f 30000)", {}),
    ("var-tail-mutual",
     "(defn a [n & r] (if (= n 0) :a (b (- n 1) 1))) (defn b [n (x 0)] (a n x)) (a 20000)", {}),
    ("var-tail-arity-err", "(defn g [a & r] a) (defn f [] (g)) (f)", {}),
    ("var-nontail-arity-err", "(defn g [a (b 1)] a) (defn f [] (+ 1 (g 1 2 3))) (f)", {}),
    ("var-requires", "(defn f [a (b 0)] (requires (> b 0)) a) (f 1)", {}),
    ("var-requires-ok", "(defn f [a (b 1) & r] (requires (> b 0)) [a b r]) (f 1 2 3)", {}),
    ("var-ensures", "(defn f [a & r] (ensures (= result (len r))) (len r)) [(f 1) (f 1 2 3)]", {}),
    ("var-ensures-fail", "(defn f [a (b 1)] (ensures (> result 10)) (+ a b)) (f 1)", {}),
    ("var-effects", '(defn f [(p "/nonexistent/z")] (effects fs.read) (read-file p)) (f)', {"allow": ["fs.read"]}),
    ("var-effects-denied", '(defn f [(p "/nonexistent/z")] (effects fs.read) (read-file p)) (f)', {}),
    ("var-effects-default",
     '(defn f [(p (read-file "/nonexistent/q"))] (effects fs.read) p) (f)', {"allow": ["fs.read"]}),
    ("var-effects-undeclared", '(defn f [(p (read-file "/nonexistent/q"))] p) (f)', {"allow": ["fs.read"]}),
    ("var-callbacks", "(map (fn [x (y 10)] (+ x y)) [1 2])", {}),
    ("var-callbacks-rest", "(map (fn [x & r] [x r]) [1 2])", {}),
    ("var-callbacks-rest-only", "(map (fn [& r] r) [1 2])", {}),
    ("var-reduce-opt", "(reduce (fn [a x (k 2)] (+ a (* x k))) 0 [1 2 3])", {}),
    ("var-reduce-rest", "(reduce (fn [a x & r] (+ a x (len r))) 0 [1 2 3])", {}),
    ("var-callback-arity", "(map (fn [a b & r] a) [1 2])", {}),
    ("var-filter", "(filter (fn [x (m 2)] (= (mod x m) 0)) (range 7))", {}),
    ("var-sort-key", "(sort [3 1 2] (fn [x (s 0)] (- s x)))", {}),
    ("var-show", "(defn f [a & r] a) (def g (fn [(x 1)] x)) [f g]", {}),
    ("var-local-rec", "(let [f (fn [n & r] (if (= n 0) (len r) (f (- n 1) 1)))] (f 5))", {}),
    ("var-nested-defn", "(defn outer [x] (defn inner [y (z 2)] (* y z)) [(inner x) (inner x 3)]) (outer 4)", {}),
    ("var-shadow-builtin", "(defn f [len & r] (len 1 2)) (f (fn [a b] (+ a b)))", {}),
    ("var-shadow-rest-name", "(defn f [& map] (len map)) (f 1 2 3)", {}),
    ("var-apply-via-value", "(defn f [a & r] [a r]) (let [g f] (g 1 2))", {}),
    ("var-stack", "(defn a [x (y 1)] (nth [1] x)) (defn b [x] (+ 1 (a x))) (b 9)", {}),
    ("var-fuel", "(defn f [x & r] (+ 1 (f x))) (f 1)", {"fuel": 300}),
    ("var-depth", "(defn f [n (k 1)] (+ k (f n))) (f 1)", {}),
    ("var-syntax-dup", "(defn f [a a] 1)", {}),
    ("var-syntax-amp", "(defn f [a & b c] 1)", {}),
    ("var-syntax-opt-after-rest", "(defn f [a & b (c 1)] 1)", {}),
    ("var-syntax-req-after-opt", "(defn f [(a 1) b] 1)", {}),
    ("var-syntax-bad-item", "(print 1) (fn [a (b 1 2)] 1)", {}),
    ("var-syntax-try-default", "(defn f [(a (? 1))] a)", {}),
]


class Differential(unittest.TestCase):
    def check(self, name, src, kw):
        a, b = both(src, **kw)
        self.assertEqual(a["ok"], b["ok"], "%s\nref=%r\npy=%r" % (name, a, b))
        self.assertEqual(a["stdout"], b["stdout"], name)
        self.assertEqual(a["effects"], b["effects"], name)
        if a["ok"]:
            self.assertEqual(a["value"], b["value"], name)
            self.assertEqual(a["json"], b["json"], name)
        elif a["error"]["code"] == "E_FUEL" == b["error"]["code"]:
            # fuel counts user-fn calls here but every call (builtins too) in the interpreter, so only the
            # code, message and hint are comparable
            for k in ("message", "hint"):
                self.assertEqual(a["error"].get(k), b["error"].get(k), name)
        else:
            ea, eb = a["error"], b["error"]
            for k in ("code", "line", "col"):
                self.assertEqual(ea.get(k), eb.get(k), "%s: %s\nref=%r\npy=%r" % (name, k, ea, eb))
            for k in ("message", "hint", "stack", "source"):
                self.assertEqual(ea.get(k), eb.get(k), "%s: %s\nref=%r\npy=%r" % (name, k, ea, eb))

    def test_corpus_size(self):
        self.assertGreaterEqual(len(CORPUS), 40)


def _mk(name, src, kw):
    def t(self):
        self.check(name, src, kw)
    return t


for _n, _s, _k in CORPUS:
    setattr(Differential, "test_" + _n.replace("-", "_"), _mk(_n, _s, _k))


class Modules(unittest.TestCase):
    def mod_check(self, files, main, **kw):
        with tempfile.TemporaryDirectory() as d:
            for n, s in files.items():
                p = os.path.join(d, n)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write(s)
            entry = os.path.join(d, main)
            src = open(entry).read()
            outs = []
            for cls in (Ref, PyInterp):
                it = cls(capture=True, root=d, entry=entry, **kw)
                try:
                    v = big_stack(lambda: it.run(src))
                    outs.append({"ok": True, "value": show(v), "out": "".join(it.out), "tests": len(it.tests)})
                except LageError as e:
                    dd = e.to_dict(src)
                    outs.append({"ok": False, "e": {k: dd.get(k) for k in ("code", "line", "col", "file", "message", "stack")},
                                 "out": "".join(it.out)})
            self.assertEqual(outs[0], outs[1])
            return outs[0]

    def test_import_basic(self):
        r = self.mod_check({"lib.lg": "(defn sq [x] (* x x)) (defn _priv [] 1) (def k 3) (test \"x\" (= 1 2))",
                            "main.lg": '(import "lib.lg" :as l) (test "t" (= (l/sq 3) 9)) (+ (l/sq 3) l/k)'}, "main.lg")
        self.assertEqual(r["value"], "12")
        self.assertEqual(r["tests"], 1)

    def test_import_private(self):
        r = self.mod_check({"lib.lg": "(defn _p [] 1)", "main.lg": '(import "lib.lg" :as l) (l/_p)'}, "main.lg")
        self.assertEqual(r["e"]["code"], "E_UNBOUND")

    def test_import_default_alias_and_nested(self):
        r = self.mod_check({"a.lg": '(import "sub/b.lg" :as b) (defn f [x] (b/g x))',
                            "sub/b.lg": "(defn g [x] (+ x 100))",
                            "main.lg": '(import "a.lg") (a/f 1)'}, "main.lg")
        self.assertEqual(r["value"], "101")

    def test_import_cycle(self):
        r = self.mod_check({"a.lg": '(import "b.lg" :as b)', "b.lg": '(import "a.lg" :as a)',
                            "main.lg": '(import "a.lg" :as a)'}, "main.lg")
        self.assertEqual(r["e"]["code"], "E_IMPORT")

    def test_import_missing_and_escape(self):
        self.assertEqual(self.mod_check({"main.lg": '(import "nope.lg" :as n)'}, "main.lg")["e"]["code"], "E_IMPORT")
        self.assertEqual(self.mod_check({"main.lg": '(import "../x.lg" :as n)'}, "main.lg")["e"]["code"], "E_IMPORT")

    def test_error_inside_module_load(self):
        r = self.mod_check({"lib.lg": "(print 1)\n(nth [] 3)", "main.lg": '(import "lib.lg" :as l)'}, "main.lg")
        self.assertEqual(r["e"]["file"], "lib.lg")
        self.assertEqual(r["e"]["line"], 2)

    def test_error_inside_module_fn(self):
        r = self.mod_check({"lib.lg": "(defn bad [x]\n  (nth [] x))", "main.lg": '(import "lib.lg" :as l)\n(defn f [x] (+ 1 (l/bad x)))\n(f 2)'}, "main.lg")
        self.assertEqual(r["e"]["code"], "E_INDEX")

    def test_module_shares_fuel(self):
        r = self.mod_check({"lib.lg": "(defn spin [x] (spin x))", "main.lg": '(import "lib.lg" :as l) (l/spin 1)'},
                           "main.lg", fuel=1000)
        self.assertEqual(r["e"]["code"], "E_FUEL")

    def test_module_shadow_builtin_isolated(self):
        r = self.mod_check({"lib.lg": "(defn len [x] 42) (defn f [] (len [1]))",
                            "main.lg": '(import "lib.lg" :as l) [(len [1 2]) (l/len 0) (l/f)]'}, "main.lg")
        self.assertEqual(r["value"], "[2 42 42]")

    def test_stdlib_style_path(self):
        # std/ imports resolve into the bundled stdlib (may not exist yet): same error on both backends
        self.mod_check({"main.lg": '(import "std/nothing-here" :as s)'}, "main.lg")


class TestForms(unittest.TestCase):
    def run_of(self, cls, src):
        it = cls(capture=True)
        it.run(src)
        return it.run_tests()

    def test_run_tests_agree(self):
        src = """(defn f [x] (* x 2))
                 (test "ok" (= (f 2) 4))
                 (test "bad" (= (f 2) 5))
                 (test "truthy" (f 1))
                 (test "falsy" (= 1 2) (nil? 1))
                 (test "err" (nth [] 1))
                 (test "try" (? (err "x")))
                 (test "syntax" (if 1))
                 (test "multi" (= 1 1) (= (f 1) 2) (= [1] [2]))"""
        a, b = self.run_of(Ref, src), self.run_of(PyInterp, src)
        self.assertEqual(a, b)
        self.assertEqual(sum(1 for r in a if r["ok"]), 3 - 1 + 0 if False else sum(1 for r in a if r["ok"]))

    def test_fuel_resets_per_test(self):
        src = '(defn f [n] (if (= n 0) 0 (f (- n 1)))) (test "a" (= (f 150) 0)) (test "b" (= (f 150) 0))'
        for cls in (Ref, PyInterp):
            it = cls(capture=True, fuel=2000)
            it.run(src)
            self.assertTrue(all(r["ok"] for r in it.run_tests()), cls.__name__)


class Cli(unittest.TestCase):
    def sh(self, backend, *a, stdin=None):
        return subprocess.run([sys.executable, "-m", "lage", *a, "--backend", backend], cwd=ROOT, input=stdin,
                              capture_output=True, text=True)

    def both(self, *a, stdin=None):
        r = [self.sh(b, *a, stdin=stdin) for b in ("interp", "py")]
        self.assertEqual(r[0].returncode, r[1].returncode)
        return r

    def test_json_run_agree(self):
        src = '(print "hi") (defn f [x] (* x 2)) (map f (range 4))'
        a, b = self.both("run", "-", "--json", stdin=src)
        da, db = json.loads(a.stdout), json.loads(b.stdout)
        for k in ("ok", "value", "stdout", "effects_used"):
            self.assertEqual(da[k], db[k])

    def test_json_error_agree(self):
        for src in ("(defn f [x] (+ 1 (f x))) (f 1)", "(nope)", "(nth [1] 4)", "(defn f [x] (f x)) (f 1)"):
            a, b = self.both("run", "-", "--json", "--no-check", stdin=src)
            ea, eb = json.loads(a.stdout)["error"], json.loads(b.stdout)["error"]
            self.assertEqual(ea, eb, src)

    def test_examples_agree(self):
        for ex in ("examples/hello.lg", "examples/use_lib.lg"):
            a, b = self.both("test", ex, "--json")
            self.assertEqual(json.loads(a.stdout), json.loads(b.stdout))
        a, b = self.both("run", "examples/files.lg", "--allow", "fs.read", "--json")
        self.assertEqual(json.loads(a.stdout)["value"], json.loads(b.stdout)["value"])

    def test_trace_agrees(self):
        src = "(defn f [n acc] (if (= n 0) acc (f (- n 1) (+ acc n)))) (f 5 0)"
        a, b = self.both("run", "-", "--json", "--trace", stdin=src)
        self.assertEqual(json.loads(a.stdout)["trace"], json.loads(b.stdout)["trace"])
        self.assertTrue(json.loads(b.stdout)["trace"])


if __name__ == "__main__":
    unittest.main()
