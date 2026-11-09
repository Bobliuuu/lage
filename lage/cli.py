import argparse
import json
import sys
import threading

from .core import Interp, LageError, format_source, show, to_json
from .ref import REF


def _read(path):
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def _allow(a):
    return [x for x in (a.allow or "").split(",") if x]


def cmd_run(a):
    src = _read(a.file)
    it = Interp(_allow(a), a.fuel, a.trace, capture=a.json)
    try:
        val = it.run(src)
    except LageError as e:
        if a.json:
            _emit({"ok": False, "error": e.to_dict(src), "stdout": "".join(it.out), "steps": it.used})
        else:
            sys.stderr.write("%s%s: %s\n" % (e.code, " at %s:%s" % (e.line, e.col) if e.line else "", e.message))
            if e.hint:
                sys.stderr.write("  hint: %s\n" % e.hint)
            if e.stack:
                sys.stderr.write("  stack: %s\n" % " <- ".join(e.stack))
        return 2 if e.code in ("E_PARSE",) else 1
    if a.json:
        out = {"ok": True, "value": to_json(val), "stdout": "".join(it.out), "steps": it.used,
               "effects_used": sorted(it.effects_used)}
        if a.trace:
            out["trace"] = it.trace
        _emit(out)
    elif val is not None:
        print(show(val))
    return 0


def cmd_test(a):
    src = _read(a.file)
    it = Interp(_allow(a), a.fuel, capture=True)
    try:
        it.run(src)
    except LageError as e:
        if a.json:
            _emit({"ok": False, "error": e.to_dict(src)})
        else:
            sys.stderr.write("%s: %s\n" % (e.code, e.message))
        return 2 if e.code == "E_PARSE" else 1
    res = it.run_tests()
    bad = [r for r in res if not r["ok"]]
    if a.json:
        _emit({"ok": not bad, "passed": len(res) - len(bad), "failed": len(bad), "results": res})
    else:
        for r in res:
            print(("PASS " if r["ok"] else "FAIL ") + r["name"])
            if not r["ok"]:
                print("     " + json.dumps({k: v for k, v in r.items() if k not in ("name", "ok")}))
        print("%d passed, %d failed" % (len(res) - len(bad), len(bad)))
    return 1 if bad else 0


def cmd_fmt(a):
    src = _read(a.file)
    try:
        out = format_source(src)
    except LageError as e:
        sys.stderr.write("%s: %s\n" % (e.code, e.message))
        return 2
    if a.check:
        return 0 if out == src else 1
    if a.write and a.file != "-":
        open(a.file, "w", encoding="utf-8").write(out)
    else:
        sys.stdout.write(out)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="lage", description="lage: a language for agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("test", cmd_test)):
        s = sub.add_parser(name)
        s.add_argument("file")
        s.add_argument("--json", action="store_true")
        s.add_argument("--allow", default="")
        s.add_argument("--fuel", type=int, default=5_000_000)
        if name == "run":
            s.add_argument("--trace", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("fmt")
    s.add_argument("file")
    s.add_argument("--write", action="store_true")
    s.add_argument("--check", action="store_true")
    s.set_defaults(fn=cmd_fmt)
    s = sub.add_parser("ref")
    s.set_defaults(fn=lambda a: print(REF, end="") or 0)
    a = p.parse_args(argv)
    sys.setrecursionlimit(200000)
    threading.stack_size(256 * 1024 * 1024)
    box = []
    t = threading.Thread(target=lambda: box.append(a.fn(a)))
    t.start()
    t.join()
    return box[0] if box else 1
