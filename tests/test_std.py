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


if __name__ == "__main__":
    unittest.main()
