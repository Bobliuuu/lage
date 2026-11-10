import glob
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = sorted(glob.glob(os.path.join(ROOT, "lage", "std", "*.lg")))


def lage(*a, stdin=None):
    return subprocess.run([sys.executable, "-m", "lage", *a], cwd=ROOT, input=stdin,
                          capture_output=True, text=True)


class Std(unittest.TestCase):
    def test_modules_present(self):
        names = {os.path.basename(f)[:-3] for f in FILES}
        self.assertTrue({"list", "str", "map", "math", "result", "json"} <= names)

    def test_every_module(self):
        for f in FILES:
            rel = os.path.relpath(f, ROOT)
            with self.subTest(module=rel, step="check"):
                r = lage("check", rel)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with self.subTest(module=rel, step="test"):
                r = lage("test", rel)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with self.subTest(module=rel, step="fmt"):
                r = lage("fmt", rel, "--check")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_import_from_user_code(self):
        src = '(import "std/list" :as l) (import "std/json" :as j) (j/pretty (l/take 2 [1 2 3]))'
        r = lage("run", "-", stdin=src)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unwrap_err_raises_assert(self):
        r = lage("run", "-", "--json", stdin='(import "std/result" :as r) (r/unwrap (err "boom"))')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("E_ASSERT", r.stdout)


IMPORTS = ('(import "std/list" :as l) (import "std/str" :as s) (import "std/map" :as m) '
           '(import "std/math" :as n) (import "std/result" :as r) (import "std/json" :as j) ')


def check_codes(body):
    from lage.check import check
    path = os.path.join(ROOT, "tmp_user_prog.lg")
    return [d["code"] for d in check(IMPORTS + body, root=ROOT, path=path)]


class StdTypes(unittest.TestCase):
    def test_correct_use_is_clean(self):
        body = """
        (defn f [xs] (sig [(list int)] num)
          (+ (n/clamp (n/gcd 12 18) 0 10) (len (l/take 2 xs)) (l/sum-by (fn [x] (* x 2)) xs)))
        (defn g [name] (sig [str] str) (s/pad-left (s/capitalize name) 8 "."))
        (defn h [d] (sig [(map int)] (map int)) (m/merge d (m/map-vals (fn [v] (+ v 1)) d)))
        (defn k [rs] (sig [(list (result int str))] (result (list int) str)) (r/collect rs))
        (defn q [x] (sig [any] str) (j/pretty x))
        (defn z [xs ys] (sig [(list int) (list str)] (list (list any))) (l/zip xs ys))
        (f [1 2 3]) (g "bob") (h {:a 1}) (k [(ok 1)]) (q [1 2]) (z [1] ["a"])
        (l/group-by (fn [x] (mod x 2)) [1 2 3]) (r/map-ok (fn [x] (+ x 1)) (ok 1))
        (j/get-path {:a [1]} ["a" 0]) (n/mean [1 2 3]) (l/chunk 2 [1 2 3])
        """
        self.assertEqual(check_codes(body), [])

    def test_miscalls_are_type_mismatches(self):
        bad = [
            '(l/take "x" [1 2])',
            '(l/enumerate 5)',
            '(l/sum-by (fn [x] (+ x 1)) ["a" "b"])',
            '(l/find (fn [x] (+ x 1)) ["a"])',
            '(s/pad-left 5 3 "0")',
            '(s/words [1 2])',
            '(n/gcd "a" 4)',
            '(n/mean 3)',
            '(m/merge [1] {:a 1})',
            '(r/map-ok (fn [s] (upper s)) (ok 1))',
            '(r/collect [1 2])',
            '(defn u [x] (sig [int] int) (+ 1 (s/lines "a")))',
            '(defn u [] (sig [] int) (s/count-substr "a" "a") (s/lines "a"))',
            '(+ 1 (s/truncate "abc" 2))',
            '(l/chunk "2" [1])',
        ]
        for src in bad:
            with self.subTest(src=src):
                self.assertIn("E_TYPE_MISMATCH", check_codes(src))

    def test_every_public_defn_has_sig(self):
        from lage.core import parse_program
        total = 0
        for f in FILES:
            with open(f) as fh:
                forms = parse_program(fh.read())[0]
            for form, _line, _col in forms:
                if (len(form) > 2 and getattr(form[0], "name", None) == "defn"
                        and not form[1].name.startswith("_")):
                    total += 1
                    has = any(isinstance(c, list) and c and getattr(c[0], "name", None) == "sig"
                              for c in form[3:])
                    with self.subTest(fn=os.path.basename(f) + ":" + form[1].name):
                        self.assertTrue(has, "missing (sig ...)")
        self.assertGreater(total, 60)


if __name__ == "__main__":
    unittest.main()
