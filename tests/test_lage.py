import json
import os
import subprocess
import sys
import unittest

from lage.check import check
from lage.core import Interp, LageError, format_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(src, **kw):
    it = Interp(capture=True, **kw)
    return it.run(src), it


def err(src, **kw):
    try:
        run(src, **kw)
    except LageError as e:
        return e
    raise AssertionError("expected LageError")


class Lang(unittest.TestCase):
    def test_basics(self):
        self.assertEqual(run("(+ 1 2 3)")[0], 6)
        self.assertEqual(run("(let [x 2 y (* x 3)] (+ x y))")[0], 8)
        self.assertEqual(run("(map (fn [x] (* x x)) (range 4))")[0], [0, 1, 4, 9])
        self.assertEqual(run("(get {:a 1} :a)")[0], 1)
        self.assertEqual(run("(if 0 :t :f)")[0], "t")  # 0 is truthy
        self.assertEqual(run("(/ 6 3)")[0], 2)

    def test_bool_not_number(self):
        self.assertEqual(err("(+ 1 true)").code, "E_TYPE")
        self.assertFalse(run("(= 1 true)")[0])

    def test_results_and_try(self):
        src = """(defn f [s] (let [n (? (parse-int s))] (ok (* n 2))))
                 [(f "21") (f "x")]"""
        v, _ = run(src)
        self.assertTrue(v[0].ok and v[0].val == 42)
        self.assertFalse(v[1].ok)

    def test_match(self):
        self.assertEqual(run('(match (parse-int "5") (ok n) n (err e) -1)')[0], 5)

    def test_contracts(self):
        pre = err("(defn f [x] (requires (> x 0)) x) (f 0)")
        self.assertEqual(pre.code, "E_REQUIRES")
        post = err("(defn f [x] (ensures (> result 10)) x) (f 1)")
        self.assertEqual(post.code, "E_ENSURES")

    def test_effects(self):
        e = err('(defn f [] (effects fs.read) (read-file "x")) (f)')
        self.assertEqual(e.code, "E_EFFECT_DENIED")
        self.assertIn("--allow fs.read", e.hint)
        e = err('(defn f [] (read-file "x")) (f)', allow=["fs.read"])
        self.assertEqual(e.code, "E_EFFECT_UNDECLARED")
        e = err('(defn f [] (effects fs.read) 1) (defn g [] (f)) (g)', allow=["fs.read"])
        self.assertEqual(e.code, "E_EFFECT_UNDECLARED")
        v, it = run('(defn f [] (effects fs.read) (read-file "/nonexistent/zz")) (f)', allow=["fs"])
        self.assertFalse(v.ok)
        self.assertEqual(it.effects_used, {"fs.read"})

    def test_lambda_inherits_effects(self):
        v, _ = run('(defn f [ps] (effects fs.read) (map (fn [p] (read-file p)) ps)) (f ["/nonexistent/a"])',
                   allow=["fs.read"])
        self.assertFalse(v[0].ok)

    def test_diagnostics(self):
        e = err("(prnt 1)")
        self.assertEqual((e.code, e.line, e.col), ("E_UNBOUND", 1, 2))
        self.assertIn("print", e.hint)
        self.assertEqual(err("(defn f [a] a) (f 1 2)").code, "E_ARITY")
        self.assertEqual(err("(nth [1] 5)").code, "E_INDEX")
        self.assertEqual(err("(/ 1 0)").code, "E_DIV")
        self.assertEqual(err("(1 2)").code, "E_NOT_FN")
        self.assertEqual(err("(fact 3").code, "E_PARSE")

    def test_fuel_and_depth(self):
        self.assertEqual(err("(defn f [x] (f (+ x 1))) (f 1)", fuel=500).code, "E_FUEL")

    def test_tail_calls_are_constant_depth(self):
        v, _ = run("(defn loop [n acc] (if (= n 0) acc (loop (- n 1) (+ acc n)))) (loop 100000 0)")
        self.assertEqual(v, 5000050000)
        v, _ = run("(defn ev? [n] (if (= n 0) true (od? (- n 1)))) (defn od? [n] (if (= n 0) false (ev? (- n 1)))) (ev? 50001)")
        self.assertFalse(v)
        self.assertEqual(err("(defn f [x] (f x)) (f 1)", fuel=1000).code, "E_FUEL")

    def test_ensures_blocks_tail_call_but_still_works(self):
        v, _ = run("(defn f [n] (ensures (>= result 0)) (if (= n 0) 0 (f (- n 1)))) (f 100)")
        self.assertEqual(v, 0)

    def test_tests_report_left_right(self):
        it = Interp(capture=True)
        it.run('(defn f [x] x) (test "ok" (= (f 1) 1)) (test "bad" (= (f 1) 2))')
        res = it.run_tests()
        self.assertTrue(res[0]["ok"])
        self.assertEqual((res[1]["left"], res[1]["right"]), (1, 2))

    def test_deterministic_maps(self):
        self.assertEqual(run("(keys {:b 1 :a 2})")[0], ["b", "a"])


def codes(src, **kw):
    return sorted(d["code"] for d in check(src, root=ROOT, **kw))


class Static(unittest.TestCase):
    def test_clean_programs(self):
        self.assertEqual(codes("(defn f [x] (g x)) (defn g [y] y) (f 1)"), [])  # forward ref in fn ok
        self.assertEqual(codes('(defn f [p] (effects fs.read) (map (fn [q] (read-file q)) [p]))'), [])
        self.assertEqual(codes("(defn f [n] (ensures (> result 0)) 1) (test \"t\" (= (f 1) 1))"), [])

    def test_reports_everything_at_once(self):
        src = "(defn f [x] (prnt x)) (f 1 2) (nth [1]) (zzz)"
        self.assertEqual(codes(src), ["E_ARITY", "E_ARITY", "E_UNBOUND", "E_UNBOUND"])

    def test_top_level_use_before_define(self):
        d = check("(g 1) (defn g [x] x)", root=ROOT)
        self.assertEqual(d[0]["code"], "E_UNBOUND")
        self.assertIn("move it above", d[0]["hint"])

    def test_effects(self):
        self.assertEqual(codes("(defn f [p] (read-file p))"), ["E_EFFECT_UNDECLARED"])
        self.assertEqual(codes("(defn g [] (effects fs.read) 1) (defn f [] (g))"), ["E_EFFECT_UNDECLARED"])
        self.assertEqual(codes("(defn g [] (effects fs.read) 1) (defn f [] (effects fs) (g))"), [])

    def test_syntax_and_parse(self):
        self.assertEqual(codes("(if 1)"), ["E_SYNTAX"])
        self.assertEqual(codes("(+ 1"), ["E_PARSE"])
        self.assertEqual(codes("(1 2)"), ["E_NOT_FN"])

    def test_locals_shadow_builtins_and_defs(self):
        self.assertEqual(codes("(defn f [len] (len 1 2 3))"), [])
        self.assertEqual(codes("(let [len (fn [a b c] a)] (len 1 2 3))"), [])


class Types(unittest.TestCase):
    def test_clean(self):
        ok = """(defn add [a b] (sig [int int] int) (+ a b))
                (defn f [x] (sig [(list int)] int) (reduce (fn [acc y] (+ acc y)) 0 x))
                (defn id [x] (sig [t] t) x)
                (defn p [s] (sig [str] (result int str)) (let [n (? (parse-int s))] (ok (+ n 1))))
                (add 1 2) (f [1 2 3]) (id "s") (id 1)
                (defn unannotated [a b] (+ a b)) (unannotated "x" "y")"""
        self.assertEqual(codes(ok), [])  # unannotated code is never a type error source

    def test_arg_and_return_mismatch(self):
        self.assertEqual(codes('(defn add [a b] (sig [int int] int) (+ a b)) (add 1 "2")'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes('(defn f [] (sig [] int) "x")'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(defn f [c] (sig [bool] int) (if c 1 nil))"), ["E_TYPE_MISMATCH"])  # nil safety

    def test_generics_bind_per_call(self):
        self.assertEqual(codes('(defn first-or [xs d] (sig [(list t) t] t) d) (first-or [1] "x")'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(defn first-or [xs d] (sig [(list t) t] t) d) (first-or [1] 2) (first-or [\"a\"] \"b\")"), [])

    def test_builtin_sigs_and_lambdas(self):
        self.assertEqual(codes('(map (fn [x] (+ x 1)) ["a"])'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(defn g [s] (sig [str] str) s) (map g [1 2])"), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes('(+ 1 "x")'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(reduce (fn [a x] (+ a x)) 0 [1 2 3])"), [])

    def test_variadic_builtin_as_function_value(self):
        self.assertEqual(codes("(defn g [f xs] (sig [(fn [a] b) (list a)] (list b)) (map f xs)) (g str [1 2])"), [])

    def test_try_and_match(self):
        self.assertEqual(codes('(defn f [s] (sig [str] int) (? (parse-int s)))'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes('(match (parse-int "1") (ok n) (+ n 1) (err e) (+ e 1))'), ["E_TYPE_MISMATCH"])

    def test_bad_sig(self):
        self.assertEqual(codes("(defn f [a] (sig [int int] int) a)"), ["E_SIG"])
        self.assertEqual(codes("(defn f [a] (sig [(lst int)] int) a)"), ["E_SIG"])

    def test_sigs_cross_modules(self):
        p = os.path.join(ROOT, "examples", "typed_use.lg")
        self.assertEqual(check(open(p).read(), path=p), [])
        d = check('(import "lib/typedlib.lg" :as t) (t/scale "x" 2)', root=ROOT,
                  path=os.path.join(ROOT, "examples", "x.lg"))
        self.assertEqual([x["code"] for x in d], ["E_TYPE_MISMATCH"])


class Modules(unittest.TestCase):
    def test_import_run_and_checker(self):
        p = os.path.join(ROOT, "examples", "use_lib.lg")
        src = open(p).read()
        self.assertEqual(check(src, path=p), [])
        it = Interp(capture=True, root=os.path.dirname(p), entry=p)
        self.assertEqual(it.run(src), 25)
        self.assertEqual(len(it.tests), 1)  # module tests are not collected

    def test_private_names_not_exported(self):
        p = os.path.join(ROOT, "examples", "use_lib.lg")
        src = open(p).read() + "(m/_sq 2)"
        self.assertEqual(codes(src, path=p), ["E_UNBOUND"])

    def test_checker_flags_bad_call_into_module(self):
        p = os.path.join(ROOT, "examples", "use_lib.lg")
        d = check('(import "lib/mathx.lg" :as m) (m/hypot2 1)', root=ROOT, path=os.path.join(ROOT, "examples", "x.lg"))
        self.assertEqual([x["code"] for x in d], ["E_ARITY"])

    def test_escape_and_missing(self):
        self.assertEqual(err('(import "../../etc/passwd" :as p)', root=ROOT).code, "E_IMPORT")
        self.assertEqual(err('(import "nope.lg" :as p)', root=ROOT).code, "E_IMPORT")


class Fmt(unittest.TestCase):
    def test_idempotent_and_keeps_top_comments(self):
        src = "; hi\n(defn   f [x]\n (+ x 1))\n(def y   {:a 1})"
        out = format_source(src)
        self.assertEqual(out, "; hi\n(defn f [x] (+ x 1))\n\n(def y {:a 1})\n")
        self.assertEqual(format_source(out), out)

    def test_long_form_breaks_and_is_stable(self):
        src = "(defn f [x] (cond (< x 0) :negative-number-here (= x 0) :zero-value-here :else :positive-number-here))"
        out = format_source(src)
        self.assertIn("\n", out)
        self.assertEqual(format_source(out), out)
        self.assertEqual(run(src + "(f 3)")[0], "positive-number-here")

    def test_inner_comment_refused(self):
        with self.assertRaises(LageError) as c:
            format_source("(do ; no\n 1)")
        self.assertEqual(c.exception.code, "E_FMT_COMMENT")

    def test_examples_canonical(self):
        for d, _, names in os.walk(os.path.join(ROOT, "examples")):
            for n in names:
                s = open(os.path.join(d, n)).read()
                self.assertEqual(format_source(s), s, n)


class Cli(unittest.TestCase):
    def sh(self, *a, stdin=None):
        return subprocess.run([sys.executable, "-m", "lage", *a], cwd=ROOT, input=stdin,
                              capture_output=True, text=True)

    def test_json_run(self):
        r = self.sh("run", "-", "--json", stdin='(print "hi") (+ 1 2)')
        d = json.loads(r.stdout)
        self.assertEqual((d["ok"], d["value"], d["stdout"]), (True, 3, "hi\n"))

    def test_json_error_exit(self):
        r = self.sh("run", "-", "--json", stdin="(nope)")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["error"]["code"], "E_UNBOUND")

    def test_examples(self):
        self.assertEqual(self.sh("test", "examples/hello.lg").returncode, 0)
        self.assertEqual(self.sh("run", "examples/files.lg").returncode, 1)
        self.assertEqual(self.sh("run", "examples/files.lg", "--allow", "fs.read").returncode, 0)

    def test_depth_limit_compact_stack(self):
        d = json.loads(self.sh("run", "-", "--json", stdin="(defn f [x] (+ 1 (f x))) (f 1)").stdout)
        self.assertEqual(d["error"]["code"], "E_DEPTH")
        self.assertEqual(d["error"]["stack"], ["f x2500"])

    def test_run_prechecks(self):
        r = self.sh("run", "-", "--json", stdin="(defn f [x] (prnt x)) (print 1)")
        d = json.loads(r.stdout)
        self.assertEqual((r.returncode, d["error"]["code"], d["steps"]), (1, "E_UNBOUND", 0))
        r = self.sh("run", "-", "--json", "--no-check", stdin="(defn f [x] (prnt x)) (+ 1 2)")
        self.assertEqual(json.loads(r.stdout)["value"], 3)

    def test_check_cmd(self):
        r = self.sh("check", "-", "--json", stdin="(nth [1]) (zz)")
        d = json.loads(r.stdout)
        self.assertEqual((r.returncode, d["ok"], len(d["diagnostics"])), (1, False, 2))
        self.assertEqual(self.sh("check", "examples/use_lib.lg").returncode, 0)

    def test_api(self):
        d = json.loads(self.sh("api", "examples/lib/typedlib.lg", "--json").stdout)
        names = [f["name"] for f in d["functions"]]
        self.assertEqual(names, ["scale", "parse-scaled"])
        self.assertEqual(d["functions"][0]["sig"], "[int int] -> int")
        self.assertEqual(d["functions"][0]["doc"], "multiply x by k")
        self.assertIn("list", self.sh("api", "std/list").stdout.lower())
        self.assertEqual(self.sh("api", "nope.lg").returncode, 2)

    def test_ref(self):
        self.assertIn("lage 0.3", self.sh("ref").stdout)


class Params(unittest.TestCase):
    def test_rest(self):
        self.assertEqual(run("(defn f [a b & more] [a b more]) [(f 1 2) (f 1 2 3 4)]")[0], [[1, 2, []], [1, 2, [3, 4]]])
        self.assertEqual(run("((fn [x & xs] [x xs]) 1 2 3)")[0], [1, [2, 3]])
        self.assertEqual(run("((fn [& xs] (len xs)))")[0], 0)

    def test_optional(self):
        src = "(defn g [a (b 10) (c (+ a b))] [a b c]) [(g 1) (g 1 2) (g 1 2 3)]"
        self.assertEqual(run(src)[0], [[1, 10, 11], [1, 2, 3], [1, 2, 3]])
        self.assertEqual(run("(defn h [a (b 1) & r] [a b r]) [(h 0) (h 0 5) (h 0 5 6 7)]")[0],
                         [[0, 1, []], [0, 5, []], [0, 5, [6, 7]]])
        self.assertEqual(run("(defn k [(a nil)] a) [(k) (k 3)]")[0], [None, 3])  # nil passed explicitly is not "missing"
        self.assertEqual(run("(defn m [(a 1) (b (fn [] a))] (b)) (m 7)")[0], 7)

    def test_arity_messages(self):
        self.assertEqual(err("(defn f [a b & m] a) (f 1)").message, "f takes 2+ args [a b & m], got 1")
        self.assertEqual(err("(defn g [a (b 1)] a) (g 1 2 3)").message, "g takes 1-2 args [a b?], got 3")
        self.assertEqual(err("(defn g [a (b 1) (c 2)] a) (g)").message, "g takes 1-3 args [a b? c?], got 0")
        self.assertEqual(err("(defn f [a] a) (f)").message, "f takes 1 args [a], got 0")

    def test_syntax_errors(self):
        for src, frag in [("(defn f [a a] 1)", "duplicate"), ("(defn f [a & b c] 1)", "exactly one"),
                          ("(defn f [a &] 1)", "exactly one"), ("(defn f [& & b] 1)", "exactly one"),
                          ("(defn f [(a 1) b] 1)", "after an optional"), ("(defn f [a (b 1 2)] 1)", "parameter must be"),
                          ("(defn f [a (b (? 1))] 1)", "'?'"), ("(defn f [a & (b 1)] 1)", "exactly one")]:
            e = err(src)
            self.assertEqual(e.code, "E_SYNTAX", src)
            self.assertIn(frag, e.message, src)
        self.assertIn("before &", err("(defn f [a & b (c 1)] 1)").hint)
        self.assertIn("before &", err("(defn f [a & (c 1)] 1)").hint)

    def test_contracts_see_defaults(self):
        self.assertEqual(err("(defn f [a (b 0)] (requires (> b 0)) a) (f 1)").code, "E_REQUIRES")
        self.assertEqual(run("(defn f [a & r] (ensures (= result (len r))) (len r)) (f 1 2 3)")[0], 2)

    def test_tail_calls_through_variadic(self):
        src = "(defn f [n & acc] (if (= n 0) (len acc) (f (- n 1) 1))) (f 100000)"
        self.assertEqual(run(src, fuel=10 ** 9)[0], 1)

    def test_closures_capture_params(self):
        self.assertEqual(run("(defn mk [a & r] (fn [] [a r])) ((mk 1 2 3))")[0], [1, [2, 3]])

    def test_checker(self):
        self.assertEqual(codes("(defn f [a b & m] (+ a b)) (f 1 2) (f 1 2 3 4) ((fn [x (y 1)] x) 1)"), [])
        self.assertEqual(codes("(defn f [a b & m] a) (f 1)"), ["E_ARITY"])
        self.assertEqual(codes("(defn g [a (b 1)] a) (g 1 2 3)"), ["E_ARITY"])
        self.assertEqual(codes("(defn g [a (b a)] b) (g 1)"), [])  # default sees earlier params
        self.assertEqual(codes("(defn g [(a b) (b 1)] a) (g)"), ["E_UNBOUND"])  # but not later ones
        self.assertEqual(codes("(defn f [a & m] (len m)) (f 1 2)"), [])
        self.assertEqual(codes("(defn f [a a] 1)"), ["E_SYNTAX"])

    def test_variadic_sigs(self):
        self.assertEqual(codes('(defn f [a & m] (sig [int & str] int) a) (f 1 "x" "y") (f 1)'), [])
        self.assertEqual(codes("(defn f [a & m] (sig [int & str] int) a) (f 1 2)"), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(defn f [a & m] (sig [int & str] int) (len m))"), [])
        self.assertEqual(codes("(defn f [a & m] (sig [int & str] int) (+ a m))"), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(defn f [a & m] (sig [int] int) a)"), ["E_SIG"])
        self.assertEqual(codes("(defn f [a] (sig [int & str] int) a)"), ["E_SIG"])
        self.assertEqual(codes("(defn g [a (b 1)] (sig [int int] int) (+ a b)) (g 1) (g 1 2)"), [])
        self.assertEqual(codes('(defn g [a (b 1)] (sig [int int] int) a) (g "s")'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes('(defn g [a (b "s")] (sig [int int] int) a)'), ["E_TYPE_MISMATCH"])
        self.assertEqual(codes("(defn f [a & m] (sig [int & int] int) a) (map f [1 2])"), [])

    def test_fmt_roundtrip(self):
        s = "(defn f [a (b 10) & more] (+ a b))\n"
        self.assertEqual(format_source(s), s)
        s2 = "(fn [x (y [1 2]) & r] x)\n"
        self.assertEqual(format_source(s2), s2)


if __name__ == "__main__":
    unittest.main()
