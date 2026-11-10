"""lage python backend: transpile lage to Python source and exec it.

The closure interpreter in core.py stays the reference backend. This module
reuses its parser, builtins, value types and error types; only evaluation
differs. Selected with LAGE_BACKEND=py|interp or `--backend` on the CLI.

How it maps lage onto Python (see also the README of the task that added it):
  * every lage binding site is a uniquely named Python local; top-level
    def/defn are Python globals (`g<mod>_<mangled>`), all modules share one exec
    namespace so fuel is one real global counter;
  * lage functions are plain Python functions (calls are CALL_FUNCTION);
  * truthiness is `is not None and is not False`, int/int arithmetic and
    comparison are inlined behind `type(x) is int`;
  * tail calls: self tail calls become `while True` loops (only when the
    function has no nested fn), all others return a `(fn, args, site)` tuple that
    call sites unwrap via `_tc`;
  * fuel: `next()` on a bounded range iterator (StopIteration -> E_FUEL),
    depth: the Python recursion limit (RecursionError -> E_DEPTH);
  * error positions/stacks cost nothing at run time: every lage call/global
    reference sits on its own physical line of the generated source that ends in
    a `#@<node-id>` marker, and escaping exceptions are translated by walking
    the traceback (see PyInterp.translate).
"""
import difflib
import itertools
import os
import re
import sys
import types
from collections import Counter

from .core import (MAXDEPTH, Builtin, EarlyReturn, E, Form, Interp, Kw, LageError, Map, Res, Sym, Vec,
                   covers, display_path, eq, flat, parse_program, show, tname, to_json, truthy, resolve_import,
                   parse_params, param_names, arity_of, arity_text, params_disp)

FT = types.FunctionType
_ID = re.compile(r"[A-Za-z_][A-Za-z_0-9]*$")
_MARK = re.compile(r"#@(P|a?\d+)$")
_DEMANGLE = re.compile(r"__|_([0-9a-f]+)_")


def mangle(name):
    return "".join(c if (c.isascii() and c.isalnum()) else ("__" if c == "_" else "_%x_" % ord(c)) for c in name)


def demangle(s):
    return _DEMANGLE.sub(lambda m: "_" if m.group(0) == "__" else chr(int(m.group(1), 16)), s)


# --------------------------------------------------------------------------
# registries shared by all compilations (ids are global)
# --------------------------------------------------------------------------
NODES = []        # id -> AST node
LINEMAP = {}      # filename -> {lineno: ("n", id) | ("a", id) | ("P",)}
CODEMETA = {}     # code object -> FnMeta
METAS = []        # id -> FnMeta
SITE_META = {}    # tail-call node id -> FnMeta of the function making the call
SYMINFO = {}      # global symbol node id -> (module id, visible local names)
ARGINFO = {}      # call node id -> [arg spec]; spec = ("c", value) | ("v", python name) | None
HEADINFO = {}     # call node id -> ("b", builtin name) | ("v", python name)


class FnMeta:
    __slots__ = ("name", "params", "effects", "outer", "lo", "hi", "disp")

    def __init__(self, name, params, effects, outer, lo=None, hi=-1, disp=None):
        self.name, self.params, self.effects, self.outer = name, params, effects, outer
        self.lo = len(params) if lo is None else lo
        self.hi = len(params) if hi == -1 else hi  # None = variadic
        self.disp = " ".join(params) if disp is None else disp

    def ctx(self):
        """The lexically enclosing *named* fn (self if named), or None at top level."""
        return self if self.effects is not None else self.outer


class PBuiltin(Builtin):
    def __call__(self, *a):
        return self.fn(*a)


_UNSET = object()  # default marker of optional params


def _tc(r):
    while True:
        r = r[0](*r[1])
        if type(r) is not tuple:
            return r


TC_CODE = _tc.__code__


def _arity_ok(code, n):
    m = CODEMETA.get(code)
    if m is None:
        return code.co_argcount == n
    return m.lo <= n and (m.hi is None or n <= m.hi)


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------
SPECIAL_TAIL = {"if": "t_if", "cond": "t_cond", "do": "t_do", "let": "t_let", "match": "t_match",
                "and": "t_and", "or": "t_or"}
SPECIAL_EX = {"def": "x_def", "defn": "x_defn", "fn": "x_fn", "if": "x_if", "let": "x_let", "do": "x_do",
              "and": "x_and", "or": "x_or", "cond": "x_cond", "match": "x_match", "?": "x_try",
              "test": "x_test", "import": "x_import"}
INLINE = {"+": "+", "-": "-", "*": "*", "<": "<", ">": ">", "<=": "<=", ">=": ">=", "=": "==", "!=": "!="}
BOOLB = {"<", ">", "<=", ">=", "=", "!=", "not", "has?", "contains?", "ok?", "err?", "starts-with?", "ends-with?"}


def simple_params(vec):
    return all(isinstance(p, Sym) and p.name != "&" for p in vec)


def kids(x):
    return x if isinstance(x, (Form, Vec, Map)) else ()


def contains_fn(forms):
    for x in forms:
        if isinstance(x, Form) and x and isinstance(x[0], Sym) and x[0].name in ("fn", "defn"):
            return True
        if contains_fn(kids(x)):
            return True
    return False


def uses_try(forms):
    for x in forms:
        if isinstance(x, Form) and x and isinstance(x[0], Sym):
            if x[0].name == "?":
                return True
            if x[0].name in ("fn", "defn"):
                continue
        if uses_try(kids(x)):
            return True
    return False


def collect_defs(forms):
    """Names defined by def/defn in the same lage Env (not under fn/let/match bodies)."""
    out = []

    def walk(x):
        if isinstance(x, Form) and x and isinstance(x[0], Sym):
            n = x[0].name
            if n in ("def", "defn"):
                if len(x) > 1 and isinstance(x[1], Sym):
                    out.append(x[1].name)
                if n == "defn":
                    return
                for e in x[2:]:
                    walk(e)
                return
            if n in ("fn", "let", "test"):
                return
            if n == "match":
                if len(x) > 1:
                    walk(x[1])
                return
        for e in kids(x):
            walk(e)
    for f in forms:
        walk(f)
    return out


def scan_binders(x, b):
    if isinstance(x, Form) and x:
        h = x[0]
        if isinstance(h, Sym):
            n = h.name
            if n == "fn" and len(x) > 1 and isinstance(x[1], Vec):
                b.update(param_names(x[1]))
            elif n == "defn" and len(x) > 2:
                if isinstance(x[1], Sym):
                    b[x[1].name] += 1
                if isinstance(x[2], Vec):
                    b.update(param_names(x[2]))
            elif n == "def" and len(x) > 1 and isinstance(x[1], Sym):
                b[x[1].name] += 1
            elif n == "let" and len(x) > 1 and isinstance(x[1], Vec):
                b.update(s.name for s in x[1][::2] if isinstance(s, Sym))
            elif n == "match":
                for pat in x[2::2]:
                    if isinstance(pat, Form) and len(pat) == 2 and isinstance(pat[1], Sym):
                        b[pat[1].name] += 1
            elif n == "ensures":
                b["result"] += 1
    if isinstance(x, (Form, Vec, Map)):
        for e in x:
            scan_binders(e, b)


# Python refuses more than 200 nested brackets (and 100 indentation levels). Any generated expression
# deeper than BUDGET is moved into a nested helper function `_hN()` (a thunk: it closes over the enclosing
# function's locals, so lage scoping is untouched); statement nesting deeper than TAIL_IND likewise.
BUDGET = 60
CHUNK = 12        # clauses of one cond/and/or chain kept inline before the rest moves into a thunk
TAIL_IND = 40
_BRK = re.compile(r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|([()\[\]{}])""")
_WALRUS = re.compile(r"\(([lg]\d+_\w*) :=")
_LREF = re.compile(r"\bl(\d+)_")
_HCALL = re.compile(r"\b(_h\d+)\(\)")


def maxdepth(code):
    d = m = 0
    for mt in _BRK.finditer(code):
        c = mt.group(1)
        if c is None:
            continue
        if c in "([{":
            d += 1
            if d > m:
                m = d
        else:
            d -= 1
    return m


def deeper_than(x, n):
    """is the AST nested more than n levels? (iterative, stops early)"""
    stack = [(x, 0)]
    while stack:
        y, d = stack.pop()
        if isinstance(y, (Form, Vec, Map)):
            if d >= n:
                return True
            stack.extend((e, d + 1) for e in y)
    return False


class Scope:
    __slots__ = ("vars", "parent", "mod")

    def __init__(self, parent=None, mod=False):
        self.vars, self.parent, self.mod = {}, parent, mod


def lookup(sc, name):
    while sc is not None:
        v = sc.vars.get(name)
        if v is not None:
            return v
        sc = sc.parent
    return None


def visible(sc):
    out = set()
    while sc is not None:
        out |= set(sc.vars)
        sc = sc.parent
    return tuple(out)


class FC:
    """Per generated python function: lines, pending hoisted defs, temp counter."""

    def __init__(self, meta):
        self.meta, self.lines, self.pre, self.ind, self.nt = meta, [], [], 1, 0
        self.globals, self.lifted = set(), []
        self.loop, self.self_name, self.self_pid, self.params = False, None, None, ()

    def tmp(self):
        self.nt += 1
        return "_t%d" % self.nt

    def emit(self, text):
        if self.pre:
            for i, t in self.pre:
                self.lines.append((self.ind + i, t))
            self.pre = []
        self.lines.append((self.ind, text))


class ModInfo:
    def __init__(self, mid):
        self.id, self.binders, self.gfun, self.defs, self.defset = mid, Counter(), {}, [], set()
        self.scope = Scope(None, True)


# --------------------------------------------------------------------------
# code generator
# --------------------------------------------------------------------------
class Gen:
    def __init__(self, it):
        self.it, self.fc, self.mod, self.pending = it, None, None, {}
        self.incall = 0  # >0 while compiling inside a call form of the current fn (mirrors interp annotate)
        self.uidn = 0
        self.hlevel, self.maxhl = {}, 0  # thunk name -> how many thunk frames deep it can nest

    def uid(self):
        self.uidn += 1
        return self.uidn

    def nid(self, node):
        NODES.append(node)
        return len(NODES) - 1

    def mk(self, nid):
        """marker text for a node that raises errors itself. Outside any call of the current fn the
        interpreter annotates only after the fn's frame is popped, so its name is left off the stack."""
        return str(nid) if self.incall else "a%d" % nid

    def declare(self, sc, name):
        v = sc.vars.get(name)
        if v is None:
            v = sc.vars[name] = "l%d_%s" % (self.uid(), mangle(name))
        return v

    def gname(self, name):
        return "g%d_%s" % (self.mod.id, mangle(name))

    def static_builtin(self, name):
        return name in self.it.bnames and name not in self.mod.binders

    def buf(self, ind, f):
        fc = self.fc
        sl, si = fc.lines, fc.ind
        fc.lines, fc.ind = [], ind
        try:
            f()
            return fc.lines
        finally:
            fc.lines, fc.ind = sl, si

    # ---- prescan (whole module, before any form runs) ----
    def prescan(self, mod, forms):
        self.mod = mod
        for node, _, _ in forms:
            scan_binders(node, mod.binders)
        for n in mod.binders:
            if n in self.it.bnames:
                self.it.ns.setdefault("g%d_%s" % (mod.id, mangle(n)), self.it.base.v[n])
        mod.gfun = {k: v for k, v in mod.gfun.items() if mod.binders[k] == 1}
        for node, _, _ in forms:
            if isinstance(node, Form) and len(node) >= 4 and isinstance(node[0], Sym) and node[0].name == "defn" \
                    and isinstance(node[1], Sym) and isinstance(node[2], Vec) \
                    and simple_params(node[2]) and mod.binders[node[1].name] == 1 \
                    and "defn" in self.it.special:
                try:
                    _, _, _, ens, body = self.fn_clauses(node[3:], True, node)
                except LageError:
                    continue
                name = node[1].name
                loop = self.is_loopable(name, body, ens)
                mod.gfun[name] = (not ens) and self.may_tc(name, len(node[2]), body[-1], loop)

    def is_loopable(self, name, body, ens):
        return bool(name) and self.mod.binders[name] == 1 and not ens and not contains_fn(body) \
            and not any(deeper_than(b, 30) for b in body)

    def may_tc(self, name, nparams, last, loop):
        sp, bn, bd = self.it.special, self.it.bnames, self.mod.binders

        def walk(x):
            if not (isinstance(x, Form) and x):
                return False
            h = x[0]
            if isinstance(h, Sym):
                n = h.name
                if n in sp:
                    if n == "if":
                        return any(walk(e) for e in x[2:4])
                    if n == "do":
                        return len(x) > 1 and walk(x[-1])
                    if n == "let":
                        return len(x) >= 3 and walk(x[-1])
                    if n in ("and", "or"):
                        return len(x) > 1 and walk(x[-1])
                    if n == "cond":
                        return any(walk(e) for e in x[2::2])
                    if n == "match":
                        return any(walk(e) for e in x[3::2])
                    return False
                if n in bn and n not in bd:
                    return False
                if loop and n == name and len(x) - 1 == nparams:
                    return False
            return True
        return walk(last)

    # ---- syntax checks shared with the interpreter (same messages) ----
    def chk_if(self, x):
        if len(x) not in (3, 4):
            raise E("E_SYNTAX", "syntax: (if cond then [else])", x, "missing else returns nil")

    def chk_let(self, x):
        if len(x) < 3 or not isinstance(x[1], Vec) or len(x[1]) % 2 or \
                not all(isinstance(s, Sym) for s in x[1][::2]):
            raise E("E_SYNTAX", "syntax: (let [name expr ...] body...)", x)

    def chk_match(self, x):
        if len(x) < 4 or len(x) % 2 != 0:
            raise E("E_SYNTAX", "syntax: (match expr (ok v) body (err e) body)", x)

    def chk_pat(self, pat, x):
        if not (isinstance(pat, Form) and len(pat) == 2 and isinstance(pat[0], Sym)
                and pat[0].name in ("ok", "err") and isinstance(pat[1], Sym)):
            raise E("E_SYNTAX", "pattern must be (ok name) or (err name)", pat if isinstance(pat, Form) else x)
        return pat[0].name == "ok", pat[1].name

    def fn_clauses(self, rest, named, node):
        rest, doc, eff, req, ens = list(rest), None, [], [], []
        if named:
            if len(rest) > 1 and isinstance(rest[0], str):
                doc = rest.pop(0)
            while rest and isinstance(rest[0], Form) and rest[0] and isinstance(rest[0][0], Sym) \
                    and rest[0][0].name in ("effects", "requires", "ensures", "sig"):
                c = rest.pop(0)
                k = c[0].name
                if k == "effects":
                    if not all(isinstance(s, Sym) for s in c[1:]):
                        raise E("E_SYNTAX", "effects takes symbols, e.g. (effects fs.read)", c)
                    eff += [s.name for s in c[1:]]
                elif k == "requires":
                    req += c[1:]
                elif k == "sig":
                    pass
                else:
                    ens += c[1:]
        if not rest:
            raise E("E_SYNTAX", "function body is empty", node)
        return eff, req, doc, ens, rest

    # ---- constants ----
    def const(self, v):
        if v is None:
            return "None"
        if v is True:
            return "True"
        if v is False:
            return "False"
        t = type(v)
        if t is int:
            return repr(v) if v >= 0 else "(%r)" % v
        if t is float and v == v and v not in (float("inf"), float("-inf")):
            return repr(v) if v >= 0 and repr(v)[0] != "-" else "(%r)" % v
        if t is str:
            return repr(v)
        return self.it.pool(v)

    # ---- expressions ----
    def ex(self, x, sc):
        if isinstance(x, Sym):
            return self.sym(x, sc)
        if isinstance(x, (Form, Vec, Map)):
            fc = self.fc
            n, u0 = len(fc.pre), self.uidn
            code = self.ex1(x, sc)
            if len(code) > 150 and maxdepth(code) > BUDGET:
                return self.hoist(code, self.take(n), u0)
            return code
        return self.const(x)

    def ex1(self, x, sc):
        if isinstance(x, Form):
            return self.form(x, sc)
        if isinstance(x, Vec):
            return "[" + ", ".join([self.ex(e, sc) for e in x]) + "]"
        return self.mapx(x, sc)

    # ---- nesting-depth hoisting ----
    def take(self, n):
        """remove and return the pending helper lines added since fc.pre had length n"""
        pre = self.fc.pre
        out = pre[n:]
        del pre[n:]
        return out

    def assigned(self, lines, u0):
        """(outer lage locals, globals) that walrus-assign inside `lines` and so need nonlocal/global in a helper"""
        nl, gl = set(), set()
        for _, t in lines:
            for m in _WALRUS.finditer(t):
                n = m.group(1)
                if n[0] == "g":
                    gl.add(n)
                elif int(n[1:n.index("_")]) <= u0:
                    nl.add(n)
        return sorted(nl), sorted(gl)

    def thunk_def(self, name, inner, u0):
        """(lines of `def name():` + inner, dead bindings making the nonlocals legal)"""
        nl, gl = self.assigned(inner, u0)
        lvl = 1 + max([self.hlevel.get(m.group(1), 0) for _, t in inner for m in _HCALL.finditer(t)] or [0])
        self.hlevel[name] = lvl
        if lvl > self.maxhl:
            self.maxhl = lvl  # python frames per lage call can reach 1 + maxhl: the recursion limit is scaled
        head = [(0, "def %s():" % name)]
        if nl:
            head.append((1, "nonlocal " + ", ".join(nl)))
        if gl:
            head.append((1, "global " + ", ".join(gl)))
        return [(0, "if 0: %s = None" % n) for n in nl], head + inner

    def hoist(self, code, new, u0):
        """Move expression `code` (with the helper lines `new` it needs) into a nested function; returns the call.
        Nested thunks that touch no lage local declared inside this subtree are lifted next to it, not into it,
        so a long chain of thunks does not nest deeper and deeper."""
        fc = self.fc
        name = "_h%d" % self.uid()
        groups, cur = [], None
        for i, t in new:
            if i == 0 or cur is None:
                cur = []
                groups.append(cur)
            cur.append((i, t))
        inner = []
        for g in groups:
            if g[0][1].startswith("if 0: ") or g[0][1].startswith("def _h") and not any(int(m.group(1)) > u0 for _, t in g
                                                        for m in _LREF.finditer(t)):
                fc.pre.extend(g)
            else:
                inner.extend((i + 1, t) for i, t in g)
        dead, lines = self.thunk_def(name, [(i, t) for i, t in inner] + [(1, "return " + code)], u0)
        fc.pre.extend(dead)
        fc.pre.extend(lines)
        return name + "()"

    def chain_wrap(self, out, acc, u0):
        """a cond/and/or chain grew long: move it (and its helper lines) into a thunk"""
        return self.hoist(out, acc, u0)

    def mapx(self, x, sc):
        if not x:
            return "{}"
        ks, vs = x[::2], x[1::2]
        if all(isinstance(k, str) for k in ks):
            return "{" + ", ".join(["%s: %s" % (self.const(k), self.ex(v, sc)) for k, v in zip(ks, vs)]) + "}"
        items = []
        for k, v in zip(ks, vs):
            items += [self.ex(k, sc), self.ex(v, sc)]
        return "_mk(  #@%s\n%s)" % (self.mk(self.nid(x)), ",\n".join(items))

    def sym(self, x, sc):
        pid = lookup(sc, x.name)
        if pid is not None:
            return pid
        if self.static_builtin(x.name):
            return "bv_" + mangle(x.name)
        return self.gsym(x, sc)

    def gsym(self, x, sc):
        i = self.nid(x)
        SYMINFO[i] = (self.mod.id, visible(sc))
        return "(\n%s  #@%s\n)" % (self.gname(x.name), self.mk(i))

    def form(self, x, sc):
        if not x:
            raise E("E_SYNTAX", "empty call ()", x, "write nil for nothing, [] for an empty list")
        h = x[0]
        if isinstance(h, Sym) and h.name in self.it.special:
            return getattr(self, SPECIAL_EX[h.name])(x, sc)
        return self.call(x, sc)

    def isbool(self, x, sc):
        if x is True or x is False:
            return True
        if isinstance(x, Form) and x and isinstance(x[0], Sym) and x[0].name in BOOLB \
                and x[0].name not in self.it.special and self.static_builtin(x[0].name):
            return True
        return False

    def truth(self, x, sc):
        c = self.ex(x, sc)
        if x is None or x is False:
            return "False"
        if not isinstance(x, (Sym, Form, Vec, Map)):
            return "True"
        if self.isbool(x, sc):
            return c
        if _ID.match(c):
            return "(%s is not None and %s is not False)" % (c, c)
        t = self.fc.tmp()
        return "((%s := %s) is not None and %s is not False)" % (t, c, t)

    def x_if(self, x, sc):
        self.chk_if(x)
        c = self.truth(x[1], sc)
        a = self.ex(x[2], sc)
        b = self.ex(x[3], sc) if len(x) == 4 else "None"
        return "(%s if %s else %s)" % (a, c, b)

    def bind(self, sc, name, v):
        if isinstance(v, Form) and v and isinstance(v[0], Sym) and v[0].name == "fn":
            self.declare(sc, name)
        code = self.ex(v, sc)
        return self.declare(sc, name), code

    def let_scope(self, x, sc):
        lsc = Scope(sc)
        for n in collect_defs(list(x[1][1::2]) + list(x[2:])):
            self.declare(lsc, n)
        return lsc

    def x_let(self, x, sc):
        self.chk_let(x)
        lsc = self.let_scope(x, sc)
        parts = []
        for s, v in zip(x[1][::2], x[1][1::2]):
            pid, code = self.bind(lsc, s.name, v)
            parts.append("(%s := %s)" % (pid, code))
        parts += [self.ex(b, lsc) for b in x[2:]]
        return "(" + ", ".join(parts) + ")[-1]"

    def x_do(self, x, sc):
        if len(x) == 1:
            return "None"
        codes = [self.ex(b, sc) for b in x[1:]]
        return codes[0] if len(codes) == 1 else "(" + ", ".join(codes) + ")[-1]"

    def x_and(self, x, sc):
        if len(x) == 1:
            return "True"
        u0 = self.uidn
        items = self.chain_items(x, sc)
        out, acc = self.chain_last(x, sc)
        n = len(items)
        for k, (cond, t, pre) in enumerate(reversed(items)):
            out, acc = "(%s if %s else %s)" % (out, cond, t or "False"), pre + acc
            if k % CHUNK == CHUNK - 1 and k < n - 1:
                out, acc = self.chain_wrap(out, acc, u0), []
        self.fc.pre.extend(acc)
        return out

    def chain_items(self, x, sc):
        """operands of and/or except the last: (cond code, temp holding the value | None, helper lines)"""
        items = []
        for b in x[1:-1]:
            n = len(self.fc.pre)
            if self.isbool(b, sc):
                items.append((self.ex(b, sc), None, self.take(n)))
            else:
                t = self.fc.tmp()
                items.append(("((%s := %s) is not None and %s is not False)" % (t, self.ex(b, sc), t), t,
                              self.take(n)))
        return items

    def chain_last(self, x, sc):
        n = len(self.fc.pre)
        out = self.ex(x[-1], sc)
        return out, self.take(n)

    def x_or(self, x, sc):
        if len(x) == 1:
            return "False"
        u0 = self.uidn
        items = self.chain_items(x, sc)
        out, acc = self.chain_last(x, sc)
        n = len(items)
        for k, (cond, t, pre) in enumerate(reversed(items)):
            out, acc = "(%s if %s else %s)" % (t or "True", cond, out), pre + acc
            if k % CHUNK == CHUNK - 1 and k < n - 1:
                out, acc = self.chain_wrap(out, acc, u0), []
        self.fc.pre.extend(acc)
        return out

    def x_cond(self, x, sc):
        if len(x) % 2 == 0:
            raise E("E_SYNTAX", "cond needs test/expr pairs", x, "end with :else expr")
        u0, fc = self.uidn, self.fc
        pairs = []
        for t, v in zip(x[1::2], x[2::2]):
            n = len(fc.pre)
            c = self.truth(t, sc)
            pairs.append((c, self.ex(v, sc), self.take(n)))
        out, acc = "None", []
        for k, (c, v, pre) in enumerate(reversed(pairs)):
            out, acc = "(%s if %s else %s)" % (v, c, out), pre + acc
            if k % CHUNK == CHUNK - 1 and k < len(pairs) - 1:
                out, acc = self.chain_wrap(out, acc, u0), []
        fc.pre.extend(acc)
        return out

    def x_match(self, x, sc):
        self.chk_match(x)
        t = self.fc.tmp()
        first = "(%s := _mres(  #@%s\n%s))" % (t, self.mk(self.nid(x)), self.ex(x[1], sc))
        clauses = []
        for pat, body in zip(x[2::2], x[3::2]):
            is_ok, var = self.chk_pat(pat, x)
            msc = Scope(sc)
            for n in collect_defs([body]):
                self.declare(msc, n)
            pid = self.declare(msc, var)
            clauses.append((is_ok, pid, self.ex(body, msc)))
        out = "_nomatch(  #@%s\n%s)" % (self.mk(self.nid(x)), t)
        for is_ok, pid, code in reversed(clauses):
            out = "(((%s := %s.val), %s)[1] if %s else %s)" % (pid, t, code, ("%s.ok" if is_ok else "not %s.ok") % t, out)
        return "(%s, %s)[1]" % (first, out)

    def x_try(self, x, sc):
        if len(x) != 2:
            raise E("E_SYNTAX", "syntax: (? expr)", x)
        return "_try(  #@%s\n%s)" % (self.mk(self.nid(x)), self.ex(x[1], sc))

    def x_test(self, x, sc):
        if len(x) < 3 or not isinstance(x[1], str):
            raise E("E_SYNTAX", 'syntax: (test "name" expr...)', x, 'example: (test "adds" (= (add 1 2) 3))')
        return "_regtest(%d, %d)" % (self.mod.id, self.nid(x))

    def x_import(self, x, sc):
        ok = len(x) in (2, 4) and isinstance(x[1], str) and (
            len(x) == 2 or (x[2] == "as" and isinstance(x[2], Kw) and isinstance(x[3], Sym)))
        if not ok:
            raise E("E_SYNTAX", 'syntax: (import "path.lg" :as alias)', x,
                    "then call alias/name; names starting with _ stay private")
        alias = x[3].name if len(x) == 4 else os.path.splitext(os.path.basename(x[1]))[0]
        return "_import(  #@%d\n%d, %r, %r, %d)" % (self.nid(x), self.mod.id, x[1], alias, self.nid(x))

    def target(self, sc, name):
        """python name a def/defn at this scope assigns to"""
        if sc.mod:
            g = self.gname(name)
            self.fc.globals.add(g)
            if name not in self.mod.defset:
                self.mod.defset.add(name)
                self.mod.defs.append(name)
            return g
        return self.declare(sc, name)

    def x_def(self, x, sc):
        if len(x) != 3 or not isinstance(x[1], Sym):
            raise E("E_SYNTAX", "syntax: (def name expr)", x)
        name = x[1].name
        if not sc.mod:
            self.declare(sc, name)
        code = self.ex(x[2], sc)
        return "(%s := _defname(%s, %r))" % (self.target(sc, name), code, name)

    def x_defn(self, x, sc):
        if len(x) < 4 or not isinstance(x[1], Sym):
            raise E("E_SYNTAX", "syntax: (defn name [params] body...)", x, "example: (defn add [a b] (+ a b))")
        name = x[1].name
        tg = self.target(sc, name)
        pyname = self.make_fn(name, x[2], x[3:], x, True, sc, None if sc.mod else tg)
        return "(%s := %s)" % (tg, pyname)

    def x_fn(self, x, sc):
        if len(x) < 3:
            raise E("E_SYNTAX", "syntax: (fn [params] body...)", x)
        return self.make_fn(None, x[1], x[2:], x, False, sc, None)

    # ---- calls ----
    def classify(self, h, sc):
        if isinstance(h, Sym) and lookup(sc, h.name) is None:
            if self.static_builtin(h.name):
                return ("sb", h.name)
            if h.name in self.mod.gfun:
                return ("sf", h.name)
        return ("dyn", None)

    def args(self, nodes, sc, temps):
        codes, specs = [], []
        for a in nodes:
            c = self.ex(a, sc)
            if not isinstance(a, (Sym, Form, Vec, Map)):
                specs.append(("c", a))
                codes.append(c)
            elif _ID.match(c):
                specs.append(("v", c))
                codes.append(c)
            elif temps and self.incall <= 30:  # very deep nesting: skip temps to stay under python's paren limit
                t = self.fc.tmp()
                codes.append("(%s := %s)" % (t, c))
                specs.append(("v", t))
            else:
                codes.append(c)
                specs.append(None)
        return codes, specs

    def operand(self, node, code):
        if type(node) is int:
            return code, None
        if _ID.match(code):
            return code, "type(%s) is int" % code
        t = self.fc.tmp()
        return t, "type(%s := %s) is int" % (t, code)

    def inline(self, name, x, sc, nid):
        a, b = self.operand(x[1], self.ex(x[1], sc)), None
        b = self.operand(x[2], self.ex(x[2], sc))
        checks = [c for c in (a[1], b[1]) if c]
        op = "%s %s %s" % (a[0], INLINE[name], b[0])
        if not checks:
            return "(%s)" % op
        cond = " & ".join("(%s)" % c for c in checks) if len(checks) > 1 else checks[0]
        return "(%s if %s else\nb_%s(  #@%d\n%s, %s)\n)" % (op, cond, mangle(name), nid, a[0], b[0])

    def head_code(self, h, sc):
        """(code, spec) for a callee expression"""
        if isinstance(h, Sym):
            pid = lookup(sc, h.name)
            if pid is not None:
                return pid, ("v", pid)
            return None, ("v", self.gname(h.name))
        c = self.ex(h, sc)
        if _ID.match(c):
            return c, ("v", c)
        t = self.fc.tmp()
        return "(%s := %s)" % (t, c), ("v", t)

    def call(self, x, sc):
        self.incall += 1
        try:
            return self._call(x, sc)
        finally:
            self.incall -= 1

    def _call(self, x, sc):
        h = x[0]
        kind = self.classify(h, sc)
        nid = self.nid(x)
        nargs = len(x) - 1
        if kind[0] == "sb":
            name = kind[1]
            if name in INLINE and nargs == 2:
                return self.inline(name, x, sc, nid)
            if name == "not" and nargs == 1:
                return "(not %s)" % self.truth(x[1], sc)
            codes, specs = self.args(x[1:], sc, True)
            HEADINFO[nid], ARGINFO[nid] = ("b", name), specs
            return "b_%s(  #@%d\n%s)" % (mangle(name), nid, ",\n".join(codes))
        hcode, hspec = self.head_code(h, sc)
        HEADINFO[nid] = hspec
        codes, specs = self.args(x[1:], sc, kind[0] != "sf")
        ARGINFO[nid] = specs
        body = ",\n".join(codes)
        if hcode is None:  # global symbol: NameError gets its own marked line
            si = self.nid(h)
            SYMINFO[si] = (self.mod.id, visible(sc))
            callstr = "(  #@%d\n%s  #@%d\n)(\n%s)" % (nid, hspec[1], si, body)
        else:
            callstr = "%s(  #@%d\n%s)" % (hcode, nid, body)
        if kind[0] == "sf" and not self.mod.gfun[kind[1]]:
            return callstr
        t = self.fc.tmp()
        return "(%s if type(%s := %s) is not tuple else\n_tc(%s)  #@%d\n)" % (t, t, callstr, t, nid)

    # ---- tail position (statements) ----
    def tail(self, x, sc):
        fc = self.fc
        if fc.ind <= TAIL_IND or fc.loop or not isinstance(x, Form):
            return self.tail1(x, sc)
        # statement nesting is about to hit python's indentation limit: compile this tail into a helper
        # function defined at function level, and `return` its result (a value or a pending tail call)
        u0, saved_pre, saved_loop = self.uidn, fc.pre, fc.loop
        fc.pre = []
        try:
            lines = self.buf(1, lambda: self.tail1(x, sc))
        finally:
            fc.pre = saved_pre
        name = "_h%d" % self.uid()
        dead, defn = self.thunk_def(name, lines, u0)
        fc.lifted.extend(dead)
        fc.lifted.extend(defn)
        fc.emit("return %s()" % name)

    def tail1(self, x, sc):
        if isinstance(x, Form) and x:
            h = x[0]
            if isinstance(h, Sym) and h.name in self.it.special:
                m = SPECIAL_TAIL.get(h.name)
                if m:
                    return getattr(self, m)(x, sc)
            else:
                return self.t_call(x, sc)
        self.fc.emit("return " + self.ex(x, sc))

    def t_if(self, x, sc):
        self.chk_if(x)
        fc = self.fc
        fc.emit("if %s:" % self.truth(x[1], sc))
        fc.ind += 1
        self.tail(x[2], sc)
        fc.ind -= 1
        if len(x) == 4:
            self.tail(x[3], sc)
        else:
            fc.emit("return None")

    def t_cond(self, x, sc):
        if len(x) % 2 == 0:
            raise E("E_SYNTAX", "cond needs test/expr pairs", x, "end with :else expr")
        fc, done = self.fc, False
        for t, v in zip(x[1::2], x[2::2]):
            if done:  # dead code: still compile it for syntax errors
                self.buf(fc.ind, lambda: (self.truth(t, sc), self.tail(v, sc)))
                continue
            c = self.truth(t, sc)
            if c == "True":
                self.tail(v, sc)
                done = True
                continue
            fc.emit("if %s:" % c)
            fc.ind += 1
            self.tail(v, sc)
            fc.ind -= 1
        if not done:
            fc.emit("return None")

    def t_do(self, x, sc):
        if len(x) == 1:
            self.fc.emit("return None")
            return
        for b in x[1:-1]:
            self.fc.emit(self.ex(b, sc))
        self.tail(x[-1], sc)

    def t_let(self, x, sc):
        self.chk_let(x)
        lsc = self.let_scope(x, sc)
        for s, v in zip(x[1][::2], x[1][1::2]):
            pid, code = self.bind(lsc, s.name, v)
            self.fc.emit("%s = %s" % (pid, code))
        for b in x[2:-1]:
            self.fc.emit(self.ex(b, lsc))
        self.tail(x[-1], lsc)

    def t_and(self, x, sc):
        fc = self.fc
        if len(x) == 1:
            fc.emit("return True")
            return
        for b in x[1:-1]:
            if self.isbool(b, sc):
                fc.emit("if not %s:" % self.ex(b, sc))
                fc.ind += 1
                fc.emit("return False")
            else:
                t = fc.tmp()
                fc.emit("%s = %s" % (t, self.ex(b, sc)))
                fc.emit("if %s is None or %s is False:" % (t, t))
                fc.ind += 1
                fc.emit("return " + t)
            fc.ind -= 1
        self.tail(x[-1], sc)

    def t_or(self, x, sc):
        fc = self.fc
        if len(x) == 1:
            fc.emit("return False")
            return
        for b in x[1:-1]:
            if self.isbool(b, sc):
                fc.emit("if %s:" % self.ex(b, sc))
                fc.ind += 1
                fc.emit("return True")
            else:
                t = fc.tmp()
                fc.emit("%s = %s" % (t, self.ex(b, sc)))
                fc.emit("if %s is not None and %s is not False:" % (t, t))
                fc.ind += 1
                fc.emit("return " + t)
            fc.ind -= 1
        self.tail(x[-1], sc)

    def t_match(self, x, sc):
        self.chk_match(x)
        fc = self.fc
        t = fc.tmp()
        fc.emit("%s = _mres(  #@%s\n%s)" % (t, self.mk(self.nid(x)), self.ex(x[1], sc)))
        for pat, body in zip(x[2::2], x[3::2]):
            is_ok, var = self.chk_pat(pat, x)
            msc = Scope(sc)
            for n in collect_defs([body]):
                self.declare(msc, n)
            pid = self.declare(msc, var)
            fc.emit(("if %s.ok:" if is_ok else "if not %s.ok:") % t)
            fc.ind += 1
            fc.emit("%s = %s.val" % (pid, t))
            self.tail(body, msc)
            fc.ind -= 1
        fc.emit("raise _nomatch(  #@%s\n%s)" % (self.mk(self.nid(x)), t))

    def t_call(self, x, sc):
        self.incall += 1
        try:
            return self._t_call(x, sc)
        finally:
            self.incall -= 1

    def _t_call(self, x, sc):
        fc, h = self.fc, x[0]
        kind = self.classify(h, sc)
        if kind[0] == "sb":
            fc.emit("return " + self.call(x, sc))
            return
        nid = self.nid(x)
        if fc.loop and isinstance(h, Sym) and h.name == fc.self_name and lookup(sc, h.name) == fc.self_pid \
                and len(x) - 1 == len(fc.params):
            codes = [self.ex(a, sc) for a in x[1:]]
            if len(codes) == 1:
                fc.emit("%s = %s" % (fc.params[0], codes[0]))
            elif codes:
                fc.emit("%s = (%s)" % (", ".join(fc.params), ",\n".join(codes)))
            fc.emit("_N()  #@%d" % nid)
            fc.emit("continue")
            return
        hcode, hspec = self.head_code(h, sc)
        if hcode is None:
            si = self.nid(h)
            SYMINFO[si] = (self.mod.id, visible(sc))
            hcode = "(\n%s  #@%d\n)" % (hspec[1], si)
        codes = [self.ex(a, sc) for a in x[1:]]
        SITE_META[nid] = fc.meta
        fc.emit("return (%s,\n(%s),\n%d)" % (hcode, ",\n".join(codes) + ("," if len(codes) == 1 else ""), nid))

    # ---- functions ----
    def make_fn(self, name, params, rest, node, named, sc, self_pid):
        req_n, opt_x, rest_n = parse_params(params, node)
        names = req_n + [n for n, _ in opt_x] + ([rest_n] if rest_n else [])
        variadic = bool(opt_x or rest_n)
        eff, req, _doc, ens, body = self.fn_clauses(rest, named, node)
        outer, saved_incall = self.fc, self.incall
        self.incall = 0
        lo, hi = arity_of(req_n, opt_x, rest_n)
        meta = FnMeta(name, names, tuple(eff) if named else None, outer.meta.ctx() if outer else None,
                      lo, hi, params_disp(req_n, opt_x, rest_n))
        mi = len(METAS)
        METAS.append(meta)
        pyname = "f%d_%s" % (self.uid(), mangle(name or "fn"))
        self.pending[pyname] = meta
        fsc = Scope(sc)
        pids = [self.declare(fsc, n) for n in names]
        for n in collect_defs(body):
            self.declare(fsc, n)
        loop = named and not variadic and self.is_loopable(name, body, ens)
        try_ = uses_try(body)
        fc = FC(meta)
        fc.loop, fc.self_name, fc.self_pid, fc.params = loop, name if loop else None, self_pid, pids
        self.fc = fc
        try:
            base = 2 if loop else 1
            bind = base + (1 if try_ else 0)

            def gen_body():
                for b in body[:-1]:
                    fc.emit(self.ex(b, fsc))
                if ens:
                    fc.emit("_res = " + self.ex(body[-1], fsc))
                else:
                    self.tail(body[-1], fsc)
            body_lines = self.buf(bind, gen_body)
            body_lines = [(bind + i, t) for i, t in fc.lifted] + body_lines
            args_t = "(%s)" % (", ".join(pids) + ("," if len(pids) == 1 else ""))

            def gen_req():
                for r in req:
                    fc.emit("if not %s:" % self.truth(r, fsc))
                    fc.ind += 1
                    fc.emit("_req_fail(  #@a%d\n%d, %d, %s)" % (self.nid(r), mi, self.nid(r), args_t))
                    fc.ind -= 1
            req_lines = self.buf(base, gen_req)

            def gen_defaults():
                # defaults run left to right; a default sees only the params before it
                dsc = Scope(sc)
                for n, pid in zip(req_n, pids):
                    dsc.vars[n] = pid
                for (n, d), pid in zip(opt_x, pids[len(req_n):]):
                    fc.emit("if %s is _U:" % pid)
                    fc.ind += 1
                    fc.emit("%s = %s" % (pid, self.ex(d, dsc)))
                    fc.ind -= 1
                    dsc.vars[n] = pid
                if rest_n:
                    fc.emit("%s = [*%s]" % (pids[-1], pids[-1]))
            def_lines = self.buf(1, gen_defaults) if variadic else []

            def gen_ens():
                esc = Scope(fsc)
                fc.emit("%s = _res" % self.declare(esc, "result"))
                for r in ens:
                    fc.emit("if not %s:" % self.truth(r, esc))
                    fc.ind += 1
                    fc.emit("_ens_fail(  #@a%d\n%d, %d, _res, %s)" % (self.nid(r), mi, self.nid(r), args_t))
                    fc.ind -= 1
            ens_lines = self.buf(1, gen_ens) if ens else []
        finally:
            self.fc, self.incall = outer, saved_incall
        sig = list(pids)
        if variadic:
            for i in range(len(req_n), len(req_n) + len(opt_x)):
                sig[i] += "=_U"
            if rest_n:
                sig[-1] = "*" + sig[-1]
        out = [(0, "def %s(%s):" % (pyname, ", ".join(sig))), (1, "_N()  #@P")]
        if meta.effects:
            out.append((1, "_eff(%d)  #@P" % mi))
        out += def_lines
        if loop:
            out.append((1, "while True:"))
        out += req_lines
        if try_:
            out.append((base, "try:"))
        out += body_lines
        if try_:
            out.append((base, "except EarlyReturn as _e:"))
            out.append((base + 1, "_res = _e.val" if ens else "return _e.val"))
        if ens:
            out += ens_lines
            out.append((1, "return _res"))
        out.append((0, "%s.__lname__ = %r" % (pyname, name))) if named else None
        outer.pre.extend(out)
        return pyname


# --------------------------------------------------------------------------
# the interpreter
# --------------------------------------------------------------------------
class PyInterp(Interp):
    def __init__(self, allow=(), fuel=5_000_000, trace=False, capture=False, root=None, entry=None):
        self._py = not trace  # --trace delegates to the closure interpreter (identical by construction)
        self._fit = None
        super().__init__(allow, fuel, trace, capture, root, entry)
        if self._py:
            self._setup()

    # steps used so far (closure backend counts every call; this one counts user-fn calls)
    @property
    def used(self):
        if not self._py:
            return self.__dict__.get("_u", 0)
        return self.fuel - self._fit.__length_hint__() if self._fit is not None else 0

    @used.setter
    def used(self, v):
        if not self._py:
            self.__dict__["_u"] = v
        elif getattr(self, "ns", None) is not None:
            self._reset_fuel()

    def _reset_fuel(self):
        self._fit = iter(range(self.fuel))
        self.ns["_N"] = self._fit.__next__

    def pool(self, v):
        k = (type(v), v if isinstance(v, (str, int, float)) else id(v))
        n = self._pool.get(k)
        if n is None:
            n = self._pool[k] = "_k%d" % len(self._pool)
            self.ns[n] = v
        return n

    # ---- setup ----
    def _setup(self):
        self.ns, self._pool, self.mods_py = {}, {}, {}
        self.gen = Gen(self)
        self._modc = itertools.count()
        self._convert_builtins()
        self.bnames = set(self.base.v)
        ns = self.ns
        for n, b in self.base.v.items():
            ns["bv_" + mangle(n)] = b
            ns["b_" + mangle(n)] = b.fn
        ns.update(_U=_UNSET, _tc=_tc, EarlyReturn=EarlyReturn, _try=self._h_try, _mres=self._h_mres, _nomatch=self._h_nomatch,
                  _mk=self._h_mk, _defname=self._h_defname, _req_fail=self._h_req_fail, _ens_fail=self._h_ens_fail,
                  _eff=self._h_eff, _trytop=self._h_trytop, _regtest=self._h_regtest, _import=self._h_import)
        self._reset_fuel()

    def _convert_builtins(self):
        s = self

        def mk(b):
            fn, eff, nm = b.fn, b.effect, b.name
            if not eff:
                return fn

            def w(*a):
                s._need(eff, "builtin " + nm, s._ctx_of(sys._getframe(1)))
                return fn(*a)
            return w
        for n, b in list(self.base.v.items()):
            self.base.v[n] = PBuiltin(b.name, mk(b), b.effect)
        apply = self.apply

        def mapf(f, xs):
            if type(f) is FT and f.__code__.co_argcount == 1:
                out = []
                for x in xs:
                    r = f(x)
                    if type(r) is tuple:
                        r = _tc(r)
                    out.append(r)
                return out
            return [apply(f, [x]) for x in xs]

        def filt(f, xs):
            if type(f) is FT and f.__code__.co_argcount == 1:
                out = []
                for x in xs:
                    r = f(x)
                    if type(r) is tuple:
                        r = _tc(r)
                    if r is not None and r is not False:
                        out.append(x)
                return out
            return [x for x in xs if truthy(apply(f, [x]))]

        def red(f, init, xs):
            acc = init
            if type(f) is FT and f.__code__.co_argcount == 2:
                for x in xs:
                    acc = f(acc, x)
                    if type(acc) is tuple:
                        acc = _tc(acc)
                return acc
            for x in xs:
                acc = apply(f, [acc, x])
            return acc

        def srt(xs, key=None):
            if key is None:
                return sorted(xs)
            pairs = [(apply(key, [x]), x) for x in xs]
            pairs.sort(key=lambda p: p[0])
            return [x for _, x in pairs]
        for n, f in (("map", mapf), ("filter", filt), ("reduce", red), ("sort", srt)):
            self.base.v[n] = PBuiltin(n, f, None)

    def apply(self, f, args, node=None):
        """Call a lage function value (used by builtins that take callbacks)."""
        if not self._py:
            return Interp.apply(self, f, args, node)
        t = type(f)
        if t is FT:
            if f.__code__.co_argcount != len(args) and not _arity_ok(f.__code__, len(args)):
                raise self._arity_err(f, len(args), node)
            r = f(*args)
            return _tc(r) if type(r) is tuple else r
        if isinstance(f, Builtin):
            try:
                return f.fn(*args)
            except (LageError, EarlyReturn):
                raise
            except Exception as ex:  # noqa: BLE001
                raise self.builtin_error(f, args, node, ex)
        raise E("E_NOT_FN", "cannot call %s: %s" % (tname(f), show(f)), node,
                "first item of a call must be a function")

    def _arity_err(self, f, nargs, node):
        meta = CODEMETA.get(f.__code__)
        params = meta.params if meta else []
        name = getattr(f, "__lname__", None)
        if meta is not None and meta.disp != " ".join(params):
            return E("E_ARITY", "%s takes %s args [%s], got %d" % (
                name or "fn", arity_text(meta.lo, meta.hi), meta.disp, nargs), node)
        return E("E_ARITY", "%s takes %d args [%s], got %d" % (name or "fn", len(params), " ".join(params), nargs), node)

    # ---- runtime helpers referenced by generated code ----
    def _h_try(self, v):
        if not isinstance(v, Res):
            raise LageError("E_TYPE", "? expects a result, got %s: %s" % (tname(v), show(v)))
        if v.ok:
            return v.val
        raise EarlyReturn(v)

    def _h_mres(self, r):
        if not isinstance(r, Res):
            raise LageError("E_TYPE", "match expects a result (ok/err), got %s: %s" % (tname(r), show(r)))
        return r

    def _h_nomatch(self, r):
        raise LageError("E_MATCH", "no clause matched %s" % show(r), hint="add the missing (ok v) or (err e) clause")

    def _h_mk(self, *kv):
        d = {}
        for i in range(0, len(kv), 2):
            k = kv[i]
            if not isinstance(k, str):
                raise LageError("E_TYPE", "map key must be keyword or string, got %s" % tname(k))
            d[k] = kv[i + 1]
        return d

    def _h_defname(self, v, name):
        if type(v) is FT and getattr(v, "__lname__", None) is None:
            v.__lname__ = name
        return v

    def _h_req_fail(self, mi, rni, args):
        meta, rn = METAS[mi], NODES[rni]
        raise E("E_REQUIRES", "precondition failed in %s: %s" % (meta.name, flat(rn)), rn,
                "called with " + show(dict(zip(meta.params, args))))

    def _h_ens_fail(self, mi, rni, res, args):
        meta, rn = METAS[mi], NODES[rni]
        raise E("E_ENSURES", "postcondition failed in %s: %s" % (meta.name, flat(rn)), rn,
                "result was %s; args %s" % (show(res), show(dict(zip(meta.params, args)))))

    def _h_trytop(self, er, nid):
        node = NODES[nid]
        return E("E_TRY_TOP", "'?' hit an err at top level: %s" % show(er.val), node,
                 "use '?' only inside a function, or (match ...) at top level")

    def _h_regtest(self, mid, nid):
        x = NODES[nid]
        if self.module_depth == 0:
            self.tests.append((x[1], list(x[2:]), x, mid))

    def _h_import(self, mid, path, alias, nid):
        exports = self.load_module(path, NODES[nid])
        for n, v in exports.items():
            self.ns["g%d_%s" % (mid, mangle(alias + "/" + n))] = v

    def _h_eff(self, mi):
        meta = METAS[mi]
        ctx = self._ctx_of(sys._getframe(2))
        for eff in meta.effects:
            self._need(eff, "fn " + meta.name, ctx)

    def _ctx_of(self, fr):
        """named fn (FnMeta) lexically enclosing the code running in frame `fr`, or None"""
        while fr is not None:
            code = fr.f_code
            if code is TC_CODE:
                m = SITE_META.get(fr.f_locals["r"][2])
                return m.ctx() if m else None
            m = CODEMETA.get(code)
            if m is not None:
                return m.ctx()
            fr = fr.f_back
        return None

    def _need(self, eff, who, ctx):
        if ctx is not None and not covers(set(ctx.effects), eff):
            raise LageError("E_EFFECT_UNDECLARED",
                            "%s needs effect '%s' but fn '%s' does not declare it" % (who, eff, ctx.name),
                            hint="add (effects %s) to (defn %s ...)" % (eff, ctx.name))
        if not covers(self.allow, eff):
            raise LageError("E_EFFECT_DENIED", "effect '%s' not granted (needed by %s)" % (eff, who),
                            hint="run with --allow %s" % eff)
        self.effects_used.add(eff)

    # ---- driver ----
    def run(self, src):
        if not self._py:
            return Interp.run(self, src)
        return self._run_module(parse_program(src)[0], self._mod(0))

    def run_forms(self, forms, env):
        if not self._py:
            return Interp.run_forms(self, forms, env)
        return self._run_module(forms, self._mod(0))

    def _mod(self, mid):
        m = self.mods_py.get(mid)
        if m is None:
            m = self.mods_py[mid] = ModInfo(mid)
        return m

    def compile_top(self, node, mod, trytop=True):
        gen, ns = self.gen, self.ns
        gen.mod = mod
        fc = FC(FnMeta(None, [], None, None))
        fc.ind = 2 if trytop else 1
        gen.fc = fc
        gen.pending = {}
        top = "_top%d" % gen.uid()
        try:
            code = gen.ex(node, mod.scope)
            fc.emit("return " + code)
        finally:
            gen.fc = None
        lines = [(0, "def %s():" % top)]
        if fc.globals:
            lines.append((1, "global " + ", ".join(sorted(fc.globals))))
        if trytop:
            nid = gen.nid(node)
            lines.append((1, "try:"))
            lines += fc.lines
            lines += [(1, "except EarlyReturn as _e:"), (2, "raise _trytop(_e, %d)  #@%d" % (nid, nid))]
        else:
            lines += fc.lines
        text = "\n".join("    " * i + t for i, t in lines) + "\n"
        fname = "<lage:%d>" % len(LINEMAP)
        LINEMAP[fname] = {n: self._marker(m.group(1)) for n, ln in enumerate(text.split("\n"), 1)
                          for m in [_MARK.search(ln)] if m}
        try:
            cobj = compile(text, fname, "exec", dont_inherit=True)
        except (SyntaxError, RecursionError, MemoryError, ValueError) as e:
            raise E("E_INTERNAL", "program too deeply nested for the python backend (%s)" % e, node,
                    "use --backend interp")
        self._register(cobj, gen.pending)
        exec(cobj, ns)
        return ns.pop(top)

    @staticmethod
    def _marker(s):
        return ("P",) if s == "P" else (("a", int(s[1:])) if s[0] == "a" else ("n", int(s)))

    def _register(self, cobj, pending):
        stack = [cobj]
        while stack:
            c = stack.pop()
            m = pending.get(c.co_name)
            if m is not None:
                CODEMETA[c] = m
            elif c.co_name.startswith("_top"):
                CODEMETA[c] = FnMeta(None, [], None, None)
            stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))

    def _run_module(self, forms, mod):
        self.gen.prescan(mod, forms)
        val = None
        for node, _, _ in forms:
            fn = self.compile_top(node, mod)
            val = self._call_top(fn)
        return val

    def _call_top(self, fn):
        old = sys.getrecursionlimit()
        depth, f = 0, sys._getframe()
        while f is not None:
            depth, f = depth + 1, f.f_back
        sys.setrecursionlimit(depth + MAXDEPTH * (1 + self.gen.maxhl) + 25)
        try:
            return fn()
        except BaseException as ex:  # noqa: BLE001
            if isinstance(ex, (KeyboardInterrupt, SystemExit)):
                raise
            sys.setrecursionlimit(old)
            err = self.translate(ex)
            if err is ex:
                raise
            raise err from None
        finally:
            sys.setrecursionlimit(old)

    def run_tests(self):
        if not self._py:
            return Interp.run_tests(self)
        results = []
        for name, exprs, node, mid in self.tests:
            self.used = 0
            r = {"name": name, "ok": True}
            mod = self._mod(mid)
            try:
                for e in exprs:
                    if isinstance(e, Form) and len(e) == 3 and isinstance(e[0], Sym) and e[0].name == "=":
                        a = self._call_top(self.compile_top(e[1], mod, False))
                        b = self._call_top(self.compile_top(e[2], mod, False))
                        if not eq(a, b):
                            r.update(ok=False, expr=flat(e), left=to_json(a), right=to_json(b))
                            break
                    else:
                        v = self._call_top(self.compile_top(e, mod, False))
                        if not truthy(v):
                            r.update(ok=False, expr=flat(e), value=to_json(v))
                            break
            except EarlyReturn as er:
                r.update(ok=False, error={"code": "E_TRY_TOP", "message": "'?' hit err: " + show(er.val)})
            except LageError as err:
                r.update(ok=False, error=err.to_dict())
            results.append(r)
        return results

    # ---- modules ----
    def load_module(self, path, node):
        if not self._py:
            return Interp.load_module(self, path, node)
        p = self.resolve_module(path, node)
        if p in self.modules:
            return self.modules[p]
        if p in self.loading:
            chain = [display_path(q, self.root) for q in self.loading + [p]]
            raise E("E_IMPORT", "import cycle: " + " -> ".join(chain), node)
        try:
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as e:
            import json
            raise E("E_IMPORT", "cannot read module %s: %s" % (json.dumps(path), e.strerror), node)
        mid = next(self._modc) + 1
        mod, saved = self._mod(mid), self.cur_dir
        self.loading.append(p)
        self.module_depth += 1
        self.cur_dir = os.path.dirname(p)
        try:
            self._run_module(parse_program(src)[0], mod)
        except LageError as e:
            if e.file is None:
                e.file, e.src = display_path(p, self.root), src
            raise
        finally:
            self.loading.pop()
            self.module_depth -= 1
            self.cur_dir = saved
        exports = {}
        for n in mod.defs:
            k = "g%d_%s" % (mid, mangle(n))
            if k in self.ns and not n.startswith("_") and "/" not in n:
                exports[n] = self.ns[k]
        self.modules[p] = exports
        return exports

    # ---- exception translation (zero cost until something goes wrong) ----
    def translate(self, ex):
        """Map any exception escaping generated code to a positioned LageError (or return ex itself)."""
        if isinstance(ex, EarlyReturn):
            return ex
        ents, tb = [], ex.__traceback__
        while tb is not None:
            ents.append((tb.tb_frame, tb.tb_lineno))
            tb = tb.tb_next
        gens = [i for i, (f, _) in enumerate(ents) if f.f_code.co_filename in LINEMAP]

        def marker(i):
            f, ln = ents[i]
            return LINEMAP[f.f_code.co_filename].get(ln)

        node_id = None
        if gens:
            m = marker(gens[-1])
            node_id = m[1] if m and m[0] != "P" else None
        stack = []
        for i in reversed(gens):
            m, meta = marker(i), CODEMETA.get(ents[i][0].f_code)
            if m and m[0] in ("P", "a"):
                continue
            if meta is not None and meta.effects is not None:
                stack.append(meta.name)

        hop = ents and ents[-1][0].f_code is TC_CODE
        node = None
        if hop:
            r = ents[-1][0].f_locals.get("r")
            if type(r) is tuple:
                node = NODES[r[2]]
                meta = SITE_META.get(r[2])
                # a non-lage callee is applied inside the caller's frame in the interpreter
                if type(r[0]) is not FT and meta is not None and meta.effects is not None:
                    stack.insert(0, meta.name)
        elif gens:
            node = self._locate(ents, gens, marker)

        if isinstance(ex, LageError):
            err = ex
        elif isinstance(ex, RecursionError):
            err = E("E_DEPTH", "call depth exceeded %d" % MAXDEPTH, node,
                    "unbounded non-tail recursion? use a tail call, or reduce/map/range")
            stack = stack[:MAXDEPTH]
        elif isinstance(ex, StopIteration):
            err = E("E_FUEL", "fuel exhausted after %d steps" % self.fuel, node,
                    "possible infinite loop; raise --fuel if intended")
        elif isinstance(ex, NameError) and not hop:
            err = self._unbound(ex, ents, gens, marker)
        else:
            err = self._call_error(ex, ents, gens, node_id, hop)
        if err.line is None and node is not None:
            err.line, err.col = getattr(node, "line", None), getattr(node, "col", None)
        if err.stack is None:
            err.stack = stack
        return err

    def _locate(self, ents, gens, marker):
        j = gens[-1]
        m = marker(j)
        if m is None:
            return None
        if m[0] != "P":
            return NODES[m[1]]
        k = j - 1  # raised in a callee prologue: blame the call site
        while k >= 0:
            kf, kl = ents[k]
            if kf.f_code is TC_CODE:
                r = kf.f_locals.get("r")
                return NODES[r[2]] if type(r) is tuple else None
            if kf.f_code.co_filename in LINEMAP:
                mm = marker(k)
                return NODES[mm[1]] if mm and mm[0] != "P" else None
            k -= 1
        return None

    def _unbound(self, ex, ents, gens, marker):
        m = marker(gens[-1]) if gens else None
        node = NODES[m[1]] if m and m[0] != "P" else None
        if isinstance(node, Sym):
            name = node.name
            mid, locs = SYMINFO.get(m[1], (0, ()))
        else:
            raw = getattr(ex, "name", "") or ""
            name, mid, locs = demangle(re.sub(r"^(g\d+|l\d+)_", "", raw)), 0, ()
        pre = "g%d_" % mid
        cands = set(self.base.v) | set(self.special) | set(locs) | {
            demangle(k[len(pre):]) for k in self.ns if k.startswith(pre)}
        near = difflib.get_close_matches(name, list(cands), 3, 0.6)
        return E("E_UNBOUND", "unbound symbol '%s'" % name, node, ("did you mean: " + ", ".join(near)) if near else None)

    def _spec_val(self, frame, spec):
        if spec is None:
            return None, False
        if spec[0] == "c":
            return spec[1], True
        n = spec[1]
        if n in frame.f_locals:
            return frame.f_locals[n], True
        if n in frame.f_globals:
            return frame.f_globals[n], True
        return None, False

    def _call_error(self, ex, ents, gens, node_id, hop):
        """TypeError/other python exception raised by a builtin, an arity mismatch or a non-callable."""
        direct = hop
        if hop:
            r = ents[-1][0].f_locals.get("r")
            callee, args, node = r[0], list(r[1]), NODES[r[2]]
        else:
            if not gens:
                return LageError("E_RUNTIME", "internal error: %s: %s" % (type(ex).__name__, ex))
            gf = ents[gens[-1]][0]
            direct = ents[-1][0] is gf
            node = NODES[node_id] if node_id is not None else None
            hs = HEADINFO.get(node_id)
            callee, args = None, None
            if hs is not None:
                if hs[0] == "b":
                    callee = self.base.v[hs[1]]
                else:
                    callee, ok = self._spec_val(gf, hs)
                    if not ok:
                        callee = None
                vals = [self._spec_val(gf, s) for s in ARGINFO.get(node_id, ())]
                args = [v for v, ok in vals] if all(ok for _, ok in vals) else None
        if callee is None and not hop:
            return LageError("E_RUNTIME", "%s: %s" % (type(ex).__name__, ex))
        nargs = len(node) - 1 if isinstance(node, Form) else (len(args) if args else 0)
        if type(callee) is FT:
            if isinstance(ex, TypeError) and direct and not _arity_ok(callee.__code__, nargs):
                return self._arity_err(callee, nargs, node)
            return LageError("E_RUNTIME", "%s: %s" % (type(ex).__name__, ex))
        if isinstance(callee, Builtin):
            f = callee
            if args is None:
                args = []
                err = self.builtin_error(f, args, node, ex)
                if isinstance(ex, TypeError):
                    err.hint = None
                return err
            return self.builtin_error(f, args, node, ex)
        if isinstance(ex, TypeError) and direct:
            return E("E_NOT_FN", "cannot call %s: %s" % (tname(callee), show(callee)), node,
                     "first item of a call must be a function")
        return LageError("E_RUNTIME", "%s: %s" % (type(ex).__name__, ex))
