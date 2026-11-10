"""Static checker: reports ALL problems in a program (and its imports) before it runs.

Catches: parse errors, malformed special forms, unbound symbols (with suggestions),
wrong argument counts, calls to non-functions, and undeclared effects.
"""
import difflib
import inspect
import os

from .core import (Form, Interp, Kw, LageError, Map, STD_DIR, Sym, Vec, display_path, parse_program,
                   resolve_import)

SPECIAL = ("def", "defn", "fn", "if", "let", "do", "and", "or", "cond", "match", "?", "test", "import")


def _arity(fn):
    """(min, max) positional args of a python callable; max None = variadic."""
    try:
        ps = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return None
    lo = sum(1 for p in ps if p.kind == p.POSITIONAL_OR_KEYWORD and p.default is p.empty)
    if any(p.kind == p.VAR_POSITIONAL for p in ps):
        return lo, None
    return lo, len(ps)


class Module:
    def __init__(self, path, src):
        self.path, self.src = path, src
        self.lines = src.splitlines()
        self.exports = {}   # name -> info dict (kind, arity, effects)
        self.diags = []


class Checker:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        proto = Interp(root=self.root)
        self.builtins = {n: b for n, b in proto.base.v.items()}
        self.mods = {}
        self.loading = []

    # ---- diagnostics ----
    def diag(self, mod, code, msg, node=None, hint=None, severity="error"):
        d = {"severity": severity, "code": code, "message": msg}
        if mod.path:
            d["file"] = display_path(mod.path, self.root)
        line = getattr(node, "line", None)
        if line:
            d["line"], d["col"] = line, node.col
            if 1 <= line <= len(mod.lines):
                d["source"] = mod.lines[line - 1]
        if hint:
            d["hint"] = hint
        mod.diags.append(d)

    # ---- entry points ----
    def check_source(self, src, path=None):
        mod = Module(os.path.realpath(path) if path else None, src)
        if mod.path:
            self.loading.append(mod.path)
        try:
            self._check(mod)
        finally:
            if mod.path:
                self.loading.pop()
        return self._collect(mod)

    def _collect(self, mod, seen=None):
        seen = seen if seen is not None else set()
        out = []
        for d in mod.diags:
            key = (d.get("file"), d.get("line"), d.get("col"), d["code"], d["message"])
            if key not in seen:
                seen.add(key)
                out.append(d)
        return out

    def load(self, importer, path, node):
        try:
            p = resolve_import(os.path.dirname(importer.path or self.root + "/x"), self.root, path)
        except ValueError as ve:
            self.diag(importer, "E_IMPORT", ve.args[0][0], node, ve.args[0][1])
            return None
        if p in self.loading:
            self.diag(importer, "E_IMPORT", "import cycle: " + " -> ".join(
                os.path.relpath(q, self.root) for q in self.loading + [p]), node)
            return None
        if p in self.mods:
            return self.mods[p]
        try:
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as e:
            self.diag(importer, "E_IMPORT", "cannot read module %r: %s" % (path, e.strerror), node)
            return None
        mod = Module(p, src)
        self.mods[p] = mod
        self.loading.append(p)
        try:
            self._check(mod)
        finally:
            self.loading.pop()
        importer.diags.extend(mod.diags)
        return mod

    # ---- per-module ----
    def _check(self, mod):
        try:
            forms = [f for f, _, _ in parse_program(mod.src)[0]]
        except LageError as e:
            d = {"severity": "error", "code": e.code, "message": e.message}
            if mod.path:
                d["file"] = display_path(mod.path, self.root)
            if e.line:
                d["line"], d["col"] = e.line, e.col
                if 1 <= e.line <= len(mod.lines):
                    d["source"] = mod.lines[e.line - 1]
            if e.hint:
                d["hint"] = e.hint
            mod.diags.append(d)
            return
        syntax_ok = []
        for f in forms:
            try:
                Interp().comp(f)
                syntax_ok.append(True)
            except LageError as e:
                node = f
                self.diag(mod, e.code, e.message, _Pos(e.line, e.col) if e.line else node, e.hint)
                syntax_ok.append(False)
        # pass 1: top-level names (visible inside function bodies regardless of order)
        top = {}
        for f, ok in zip(forms, syntax_ok):
            if ok and isinstance(f, Form) and f and isinstance(f[0], Sym):
                h = f[0].name
                if h == "defn":
                    top[f[1].name] = self._fn_info(f[2], f[3:], True)
                elif h == "def":
                    v = f[2]
                    top[f[1].name] = self._fn_info(v[1], v[2:], False) if (
                        isinstance(v, Form) and v and isinstance(v[0], Sym) and v[0].name == "fn") \
                        else {"kind": "val"}
                elif h == "import":
                    alias = f[3].name if len(f) == 4 else os.path.splitext(os.path.basename(f[1]))[0]
                    m = self.load(mod, f[1], f)
                    if m:
                        for n, info in m.exports.items():
                            top[alias + "/" + n] = info
        mod.exports = {n: i for n, i in top.items() if not n.startswith("_") and "/" not in n}
        # pass 2: walk with sequential top-level visibility
        defined = set()
        for f, ok in zip(forms, syntax_ok):
            if not ok:
                continue
            if isinstance(f, Form) and f and isinstance(f[0], Sym) and f[0].name == "import":
                alias = f[3].name if len(f) == 4 else os.path.splitext(os.path.basename(f[1]))[0]
                defined |= {n for n in top if n.startswith(alias + "/")}
                continue
            ctx = _Ctx(mod, top, defined, in_fn=False, effects=None)
            self.walk(f, set(), ctx)
            if isinstance(f, Form) and f and isinstance(f[0], Sym) and f[0].name in ("def", "defn"):
                defined.add(f[1].name)

    def _fn_info(self, params, rest, named):
        eff = ()
        if named:
            rest = list(rest)
            if len(rest) > 1 and isinstance(rest[0], str):
                rest = rest[1:]
            for c in rest:
                if isinstance(c, Form) and c and isinstance(c[0], Sym) and c[0].name in ("effects", "requires", "ensures", "sig"):
                    if c[0].name == "effects":
                        eff += tuple(s.name for s in c[1:] if isinstance(s, Sym))
                else:
                    break
        return {"kind": "fn", "arity": len(params), "effects": eff if named else None}

    # ---- expression walk ----
    def visible(self, name, scope, ctx):
        return (name in scope or name in self.builtins or name in ctx.defined
                or (ctx.in_fn and name in ctx.top))

    def walk(self, x, scope, ctx):
        mod = ctx.mod
        if isinstance(x, Sym):
            if not self.visible(x.name, scope, ctx):
                cands = list(scope | set(self.builtins) | set(ctx.top if ctx.in_fn else ctx.defined))
                m = difflib.get_close_matches(x.name, cands, 3, 0.6)
                self.diag(mod, "E_UNBOUND", "unbound symbol '%s'" % x.name, x,
                          ("did you mean: " + ", ".join(m)) if m else (
                              "defined later at top level; move it above" if x.name in ctx.top else None))
            return
        if isinstance(x, (Vec, Map)):
            for e in x:
                self.walk(e, scope, ctx)
            return
        if not isinstance(x, Form) or not x:
            return
        h = x[0]
        if isinstance(h, Sym) and h.name in SPECIAL and not self.visible_local(h.name, scope):
            return self.walk_special(x, scope, ctx)
        if isinstance(h, Sym):
            self.walk(h, scope, ctx)
            if h.name not in scope:
                self.check_call(x, ctx)
        elif isinstance(h, Form):
            self.walk(h, scope, ctx)
        else:
            self.diag(mod, "E_NOT_FN", "cannot call a literal", x, "first item of a call must be a function")
        for a in x[1:]:
            self.walk(a, scope, ctx)

    def visible_local(self, name, scope):
        return name in scope

    def check_call(self, x, ctx):
        name = x[0].name
        n = len(x) - 1
        info = ctx.top.get(name) if (name in ctx.defined or ctx.in_fn) else None
        if info is None and name in self.builtins:
            b = self.builtins[name]
            ar = _arity(b.fn)
            if ar and (n < ar[0] or (ar[1] is not None and n > ar[1])):
                want = str(ar[0]) if ar[0] == ar[1] else ("%d+" % ar[0] if ar[1] is None else "%d-%d" % ar)
                self.diag(ctx.mod, "E_ARITY", "%s takes %s args, got %d" % (name, want, n), x)
            if b.effect:
                self.need(ctx, b.effect, "builtin " + name, x)
            return
        if info and info["kind"] == "fn":
            if n != info["arity"]:
                self.diag(ctx.mod, "E_ARITY", "%s takes %d args, got %d" % (name, info["arity"], n), x)
            for eff in info["effects"] or ():
                self.need(ctx, eff, "fn " + name, x)

    def need(self, ctx, eff, who, node):
        if ctx.effects is None:
            return
        if not (eff in ctx.effects or eff.split(".")[0] in ctx.effects or "*" in ctx.effects):
            self.diag(ctx.mod, "E_EFFECT_UNDECLARED",
                      "%s needs effect '%s' but fn '%s' does not declare it" % (who, eff, ctx.fname), node,
                      "add (effects %s) to (defn %s ...)" % (eff, ctx.fname))

    def walk_special(self, x, scope, ctx):
        h = x[0].name
        mod = ctx.mod
        if h in ("defn", "fn"):
            named = h == "defn"
            params = x[2] if named else x[1]
            rest = list(x[3:] if named else x[2:])
            sc = set(scope) | {p.name for p in params}
            if named:
                sc.add(x[1].name)
            effects, ens, req, body = (), [], [], rest
            if named:
                if len(body) > 1 and isinstance(body[0], str):
                    body = body[1:]
                while body and isinstance(body[0], Form) and body[0] and isinstance(body[0][0], Sym) \
                        and body[0][0].name in ("effects", "requires", "ensures", "sig"):
                    c = body[0]
                    body = body[1:]
                    if c[0].name == "effects":
                        effects += tuple(s.name for s in c[1:])
                    elif c[0].name == "requires":
                        req += c[1:]
                    elif c[0].name == "sig":
                        pass
                    else:
                        ens += c[1:]
            inner = _Ctx(mod, ctx.top, ctx.defined, True, effects if named else ctx.effects,
                         x[1].name if named else ctx.fname)
            if named:
                inner.top = ctx.top
            for r in req:
                self.walk(r, sc, inner)
            for r in ens:
                self.walk(r, sc | {"result"}, inner)
            for b in body:
                self.walk(b, sc, inner)
            return
        if h == "def":
            scope.add(x[1].name)
            self.walk(x[2], scope, ctx)
            return
        if h == "let":
            sc = set(scope)
            for s, v in zip(x[1][::2], x[1][1::2]):
                self.walk(v, sc, ctx)
                sc.add(s.name)
            for b in x[2:]:
                self.walk(b, sc, ctx)
            return
        if h == "match":
            self.walk(x[1], scope, ctx)
            for pat, body in zip(x[2::2], x[3::2]):
                self.walk(body, scope | {pat[1].name}, ctx)
            return
        if h == "test":
            tctx = _Ctx(mod, ctx.top, ctx.defined, True, None, ctx.fname)
            for e in x[2:]:
                self.walk(e, set(scope), tctx)
            return
        if h == "import":
            return
        for e in x[1:]:
            self.walk(e, scope, ctx)


class _Pos:
    def __init__(self, line, col):
        self.line, self.col = line, col


class _Ctx:
    def __init__(self, mod, top, defined, in_fn, effects, fname=None):
        self.mod, self.top, self.defined = mod, top, defined
        self.in_fn, self.effects, self.fname = in_fn, effects, fname


def check(src, root=None, path=None):
    """Return a list of diagnostic dicts (empty = clean)."""
    return Checker(root or (os.path.dirname(os.path.abspath(path)) if path else os.getcwd())).check_source(src, path)
