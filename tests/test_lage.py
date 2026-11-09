import json
import os
import subprocess
import sys
import unittest

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

    def test_tests_report_left_right(self):
        it = Interp(capture=True)
        it.run('(defn f [x] x) (test "ok" (= (f 1) 1)) (test "bad" (= (f 1) 2))')
        res = it.run_tests()
        self.assertTrue(res[0]["ok"])
        self.assertEqual((res[1]["left"], res[1]["right"]), (1, 2))

    def test_deterministic_maps(self):
        self.assertEqual(run("(keys {:b 1 :a 2})")[0], ["b", "a"])


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
        for n in os.listdir(os.path.join(ROOT, "examples")):
            s = open(os.path.join(ROOT, "examples", n)).read()
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
        d = json.loads(self.sh("run", "-", "--json", stdin="(defn f [x] (f x)) (f 1)").stdout)
        self.assertEqual(d["error"]["code"], "E_DEPTH")
        self.assertEqual(d["error"]["stack"], ["f x2500"])

    def test_ref(self):
        self.assertIn("lage 0.1", self.sh("ref").stdout)


if __name__ == "__main__":
    unittest.main()
