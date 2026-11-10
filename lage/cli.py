import argparse
import json
import os
import sys
import threading

from .api import api_of, render, resolve
from .check import check
from .core import Interp, LageError, format_source, show, to_json
from .ref import REF


def _read(path):
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def _root(a):
    return os.path.dirname(os.path.abspath(a.file)) if a.file != "-" else os.getcwd()


def _allow(a):
    return [x for x in (a.allow or "").split(",") if x]


def _precheck(a, src):
    """Run the static checker; print diagnostics and return an exit code, or None if clean."""
    if getattr(a, "no_check", False):
        return None
    try:
        diags = check(src, _root(a), a.file if a.file != "-" else None)
    except RecursionError:
        return None
    errs = [d for d in diags if d["severity"] == "error"]
    if not errs:
        return None
    if a.json:
        _emit({"ok": False, "error": errs[0], "diagnostics": errs, "stdout": "", "steps": 0})
    else:
        for d in errs:
            _print_diag(d)
    return 2 if any(d["code"] == "E_PARSE" for d in errs) else 1


def _print_diag(d):
    where = "%s%s" % (d["file"] + ":" if "file" in d else "", "%s:%s" % (d["line"], d["col"]) if "line" in d else "")
    sys.stderr.write("%s%s: %s\n" % (d["code"], " at " + where if where else "", d["message"]))
    if d.get("hint"):
        sys.stderr.write("  hint: %s\n" % d["hint"])


def cmd_check(a):
    src = _read(a.file)
    diags = check(src, _root(a), a.file if a.file != "-" else None)
    bad = [d for d in diags if d["severity"] == "error"]
    if a.json:
        _emit({"ok": not bad, "diagnostics": diags})
    else:
        for d in diags:
            _print_diag(d)
        print("ok" if not bad else "%d problem(s)" % len(bad))
    return 2 if any(d["code"] == "E_PARSE" for d in bad) else (1 if bad else 0)


def cmd_run(a):
    src = _read(a.file)
    rc = _precheck(a, src)
    if rc is not None:
        return rc
    it = Interp(_allow(a), a.fuel, a.trace, capture=a.json, root=_root(a), entry=a.file if a.file != "-" else None)
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
    rc = _precheck(a, src)
    if rc is not None:
        return rc
    it = Interp(_allow(a), a.fuel, capture=True, root=_root(a), entry=a.file if a.file != "-" else None)
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


def cmd_api(a):
    try:
        src = _read(resolve(a.file))
        fns = api_of(src)
    except OSError as e:
        sys.stderr.write("cannot read %s: %s\n" % (a.file, e.strerror))
        return 2
    except LageError as e:
        sys.stderr.write("%s: %s\n" % (e.code, e.message))
        return 2
    if a.json:
        _emit({"ok": True, "module": a.file, "functions": fns})
    else:
        print(render(fns))
    return 0


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
        s.add_argument("--no-check", action="store_true", help="skip the static pre-check")
        s.add_argument("--backend", choices=("py", "interp"), help="evaluator (default: $LAGE_BACKEND or built-in default)")
        if name == "run":
            s.add_argument("--trace", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("check")
    s.add_argument("file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("api")
    s.add_argument("file", help="path or std/NAME")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_api)
    s = sub.add_parser("fmt")
    s.add_argument("file")
    s.add_argument("--write", action="store_true")
    s.add_argument("--check", action="store_true")
    s.set_defaults(fn=cmd_fmt)
    s = sub.add_parser("ref")
    s.set_defaults(fn=lambda a: print(REF, end="") or 0)
    a = p.parse_args(argv)
    if getattr(a, "backend", None):
        os.environ["LAGE_BACKEND"] = a.backend
    sys.setrecursionlimit(200000)
    threading.stack_size(256 * 1024 * 1024)
    box = []
    t = threading.Thread(target=lambda: box.append(a.fn(a)))
    t.start()
    t.join()
    return box[0] if box else 1
