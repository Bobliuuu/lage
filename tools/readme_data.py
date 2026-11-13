#!/usr/bin/env python3
"""Run the real checks and benchmark, and write results/readme_data.json (the source of every README/video number).

    python3 tools/readme_data.py       # from the repo root; runs bench/bench.py, the unit tests, and lage on the examples
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
PY = sys.executable


def run(*a):
    return subprocess.run([PY, *a], capture_output=True, text=True, cwd=ROOT)


out = {}

# benchmark (bench/bench.py is deterministic in its checks: it aborts if interp and py disagree)
r = run("bench/bench.py", "--reps", "3")
assert r.returncode == 0, r.stdout + r.stderr
rows = []
for line in r.stdout.splitlines()[1:]:
    m = re.match(r"(.+?)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)x$", line.strip())
    if m:
        rows.append({"case": m.group(1).strip(), "interp_s": float(m.group(2)), "py_s": float(m.group(3)),
                     "speedup": round(float(m.group(2)) / float(m.group(3)), 1)})
out["bench"] = rows
out["speedup_max"] = max(x["speedup"] for x in rows)
out["speedup_min"] = min(x["speedup"] for x in rows)
os.makedirs("results", exist_ok=True)
json.dump(rows, open("results/bench.json", "w"), indent=1)

# unit tests
r = run("-m", "unittest", "discover", "-s", "tests")
m = re.search(r"Ran (\d+) tests", r.stderr)
out["tests"] = int(m.group(1))
out["tests_ok"] = r.returncode == 0 and "OK" in r.stderr
assert out["tests_ok"], r.stderr[-500:]

# multi-error check pass on a deliberately broken program
r = run("-m", "lage", "check", "examples/broken.lg", "--json")
d = json.loads(r.stdout)
out["broken_diagnostics"] = len(d["diagnostics"])
out["broken_codes"] = sorted({x["code"] for x in d["diagnostics"]})
out["broken_exit"] = r.returncode
out["hint_example"] = next(x["hint"] for x in d["diagnostics"] if x.get("hint"))

# contracts + inline tests
r = run("-m", "lage", "test", "examples/hello.lg", "--json")
d = json.loads(r.stdout)
out["hello_passed"], out["hello_failed"] = d["passed"], d["failed"]
r = run("-m", "lage", "run", "examples/hello.lg", "--json")
d = json.loads(r.stdout)
out["hello_steps"] = d["steps"]

# stdlib surface
mods, fns = [], 0
for fn in sorted(os.listdir("lage/std")):
    if fn.endswith(".lg"):
        a = json.loads(run("-m", "lage", "api", "std/" + fn[:-3], "--json").stdout)
        mods.append({"module": "std/" + fn[:-3], "functions": len(a["functions"])})
        fns += len(a["functions"])
out["std_modules"], out["std_functions"], out["std_rows"] = len(mods), fns, mods

out["src_lines"] = sum(len(open("lage/" + f).read().splitlines()) for f in os.listdir("lage") if f.endswith(".py"))
out["stdlib_only"] = "dependencies" not in open("pyproject.toml").read()
json.dump(out, open("results/readme_data.json", "w"), indent=1)
print("wrote results/readme_data.json")
