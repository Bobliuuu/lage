"""lage: a small programming language designed for LLM agents.

Design goals (see README):
  * one unambiguous, whitespace-insensitive syntax (S-expressions)
  * effects are declared and capability-gated (fs.read, fs.write, net, time)
  * contracts (requires/ensures) and tests live inline with the code
  * every failure is a structured diagnostic (code, line, col, hint, stack)
  * bounded execution (fuel), deterministic output, canonical formatter
"""
import difflib
import json
import os
import re
import sys
import urllib.request

MAXDEPTH = 2500


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------
class LageError(Exception):
    def __init__(self, code, message, line=None, col=None, hint=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.col = col
        self.hint = hint
        self.stack = None
        self.file = None
        self.src = None

    def to_dict(self, src=None):
        d = {"code": self.code, "message": self.message}
        if self.file:
            d["file"] = self.file
            src = self.src
        if self.line:
            d["line"] = self.line
            d["col"] = self.col
            if src:
                lines = src.splitlines()
                if 1 <= self.line <= len(lines):
                    d["source"] = lines[self.line - 1]
        if self.hint:
            d["hint"] = self.hint
        if self.stack:
            d["stack"] = compress_stack(self.stack)
        return d


def compress_stack(names, cap=12):
    """Innermost-first call stack; collapse runs ('f x2498') and cap length."""
    out = []
    for n in names:
        if out and out[-1][0] == n:
            out[-1][1] += 1
        else:
            out.append([n, 1])
    items = ["%s x%d" % (n, c) if c > 1 else n for n, c in out]
    return items if len(items) <= cap else items[:cap] + ["... %d more" % (len(items) - cap)]


def E(code, msg, node=None, hint=None):
    return LageError(code, msg, getattr(node, "line", None), getattr(node, "col", None), hint)


# --------------------------------------------------------------------------
# data types
# --------------------------------------------------------------------------
class Sym:
    __slots__ = ("name", "line", "col")

    def __init__(self, name, line=None, col=None):
        self.name, self.line, self.col = name, line, col


class Kw(str):
    """Keyword :name. Equal to the plain string 'name' so maps round-trip JSON."""


class Form(list):
    pass


class Vec(list):
    pass


class Map(list):
    pass


class Res:
    __slots__ = ("ok", "val")

    def __init__(self, ok, val):
        self.ok, self.val = ok, val


class Fn:
    def __init__(self, name, params, body, env, effects, requires, ensures, doc):
        self.name, self.params, self.body, self.env = name, params, body, env
        self.effects, self.requires, self.ensures, self.doc = effects, requires, ensures, doc


class Builtin:
    def __init__(self, name, fn, effect=None):
        self.name, self.fn, self.effect = name, fn, effect


class Frame:
    def __init__(self, fn):
        self.fn = fn


class EarlyReturn(Exception):
    def __init__(self, val):
        self.val = val


def truthy(v):
    return v is not None and v is not False


def tname(v):
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, Kw):
        return "kw"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "map"
    if isinstance(v, Res):
        return "result"
    if isinstance(v, (Fn, Builtin)):
        return "fn"
    return type(v).__name__


def eq(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, Res) or isinstance(b, Res):
        return isinstance(a, Res) and isinstance(b, Res) and a.ok == b.ok and eq(a.val, b.val)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(eq(a[k], b[k]) for k in a)
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return False
    return a == b


_KEY = re.compile(r"[A-Za-z_][\w\-?!.]*$")


def show(v):
    """Render a value in lage syntax."""
    if v is None:
        return "nil"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, Kw):
        return ":" + v
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + " ".join(show(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + " ".join((":" + k if _KEY.match(k) else json.dumps(k)) + " " + show(x)
                              for k, x in v.items()) + "}"
    if isinstance(v, Res):
        return "(%s %s)" % ("ok" if v.ok else "err", show(v.val))
    if isinstance(v, Fn):
        return "<fn %s>" % (v.name or "anonymous")
    if isinstance(v, Builtin):
        return "<builtin %s>" % v.name
    return str(v)


def display(v):
    return v if isinstance(v, str) else show(v)


def to_json(v):
    if isinstance(v, Res):
        return {"ok": to_json(v.val)} if v.ok else {"err": to_json(v.val)}
    if isinstance(v, (Fn, Builtin)):
        return show(v)
    if isinstance(v, list):
        return [to_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_json(x) for k, x in v.items()}
    if isinstance(v, Kw):
        return str(v)
    return v


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
TOK = re.compile(
    r'''\s+|(?P<cm>;[^\n]*)|(?P<str>"(?:\\.|[^"\\\n])*")|(?P<open>[(\[{])|(?P<close>[)\]}])|(?P<atom>[^\s()\[\]{}";]+)''')
INT = re.compile(r"[+-]?\d+")
FLT = re.compile(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?")
OPEN = {"(": ")", "[": "]", "{": "}"}


def lc(src, off):
    return src.count("\n", 0, off) + 1, off - (src.rfind("\n", 0, off) + 1) + 1


class Parser:
    def __init__(self, src):
        self.src, self.toks, self.comments, self.i = src, [], [], 0
        pos = 0
        while pos < len(src):
            m = TOK.match(src, pos)
            if not m:
                l, c = lc(src, pos)
                raise LageError("E_PARSE", "unterminated string or illegal character", l, c,
                                'strings are single-line; close with " and use \\n for newlines')
            k = m.lastgroup
            if k == "cm":
                self.comments.append((m.start(), m.group()))
            elif k:
                self.toks.append((k, m.group(), m.start(), m.end()))
            pos = m.end()

    def read(self):
        k, t, a, _ = self.toks[self.i]
        self.i += 1
        l, c = lc(self.src, a)
        if k == "open":
            close, items = OPEN[t], []
            while True:
                if self.i >= len(self.toks):
                    raise LageError("E_PARSE", "unclosed '%s'" % t, l, c, "add '%s'" % close)
                kk, tt, aa, bb = self.toks[self.i]
                if kk == "close":
                    if tt != close:
                        ll, cc = lc(self.src, aa)
                        raise LageError("E_PARSE", "expected '%s' but found '%s'" % (close, tt), ll, cc,
                                        "'%s' opened at %d:%d" % (t, l, c))
                    self.i += 1
                    break
                items.append(self.read())
            node = {"(": Form, "[": Vec, "{": Map}[t](items)
            node.line, node.col = l, c
            if t == "{" and len(items) % 2:
                raise LageError("E_PARSE", "map literal needs an even number of items", l, c,
                                "write {:key value :key value}")
            return node
        if k == "close":
            raise LageError("E_PARSE", "unexpected '%s'" % t, l, c, "remove it")
        if k == "str":
            try:
                return json.loads(t)
            except ValueError:
                raise LageError("E_PARSE", "bad string escape", l, c, 'valid escapes: \\n \\t \\" \\\\ \\uXXXX')
        if t == "true":
            return True
        if t == "false":
            return False
        if t == "nil":
            return None
        if INT.fullmatch(t):
            return int(t)
        if FLT.fullmatch(t):
            return float(t)
        if t[0] == ":":
            if len(t) == 1:
                raise LageError("E_PARSE", "empty keyword", l, c, "write :name")
            return Kw(t[1:])
        return Sym(t, l, c)


def parse_program(src):
    """Return ([(node, start_off, end_off)], comments)."""
    p = Parser(src)
    out = []
    while p.i < len(p.toks):
        start = p.toks[p.i][2]
        node = p.read()
        out.append((node, start, p.toks[p.i - 1][3]))
    return out, p.comments


# --------------------------------------------------------------------------
# canonical formatter
# --------------------------------------------------------------------------
HEAD_ARGS = {"defn": 2, "fn": 1, "let": 1, "def": 1, "test": 1, "if": 1, "match": 1}


def flat(x):
    if isinstance(x, Form):
        return "(" + " ".join(flat(e) for e in x) + ")"
    if isinstance(x, Vec):
        return "[" + " ".join(flat(e) for e in x) + "]"
    if isinstance(x, Map):
        return "{" + " ".join(flat(e) for e in x) + "}"
    if isinstance(x, Sym):
        return x.name
    return show(x)


def fmt_node(x, indent=0, width=80):
    one = flat(x)
    if len(one) + indent <= width or not isinstance(x, (Form, Vec, Map)) or not x:
        return one
    sp = "\n" + " " * (indent + 2)
    if isinstance(x, Form):
        n = HEAD_ARGS.get(x[0].name, 0) if isinstance(x[0], Sym) else 0
        head = "(" + " ".join(flat(e) for e in x[:1 + n])
        return head + "".join(sp + fmt_node(e, indent + 2, width) for e in x[1 + n:]) + ")"
    if isinstance(x, Vec):
        return "[" + ("\n" + " " * (indent + 1)).join(fmt_node(e, indent + 1, width) for e in x) + "]"
    lines = []
    for k, v in zip(x[::2], x[1::2]):
        ks = flat(k)
        lines.append(ks + " " + fmt_node(v, indent + 1 + len(ks) + 1, width))
    return "{" + ("\n" + " " * (indent + 1)).join(lines) + "}"


def format_source(src):
    forms, comments = parse_program(src)
    out, prev_end = [], 0
    for node, start, end in forms:
        if any(start < off < end for off, _ in comments):
            raise LageError("E_FMT_COMMENT", "comment inside a form would be lost by formatting",
                            *lc(src, next(o for o, _ in comments if start < o < end)),
                            "move the comment above the top-level form")
        lead = [c.rstrip() for off, c in comments if prev_end <= off < start]
        out.append("\n".join(lead + [fmt_node(node)]))
        prev_end = end
    tail = [c.rstrip() for off, c in comments if off >= prev_end]
    if tail:
        out.append("\n".join(tail))
    return "\n\n".join(out) + "\n" if out else ""


# --------------------------------------------------------------------------
# interpreter
# --------------------------------------------------------------------------
class Env:
    def __init__(self, parent=None):
        self.v, self.p = {}, parent

    def get(self, name):
        e = self
        while e:
            if name in e.v:
                return e.v[name]
            e = e.p
        raise KeyError(name)

    def names(self):
        out, e = set(), self
        while e:
            out |= set(e.v)
            e = e.p
        return out


def covers(granted, eff):
    return "*" in granted or eff in granted or eff.split(".")[0] in granted


def num(x, who):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise E("E_TYPE", "%s expects a number, got %s: %s" % (who, tname(x), show(x)),
                hint="use (str ...) or (concat ...) for strings" if isinstance(x, str) else None)
    return x


class TailCall:
    __slots__ = ("f", "args", "node")

    def __init__(self, f, args, node):
        self.f, self.args, self.node = f, args, node


class Interp:
    def __init__(self, allow=(), fuel=5_000_000, trace=False, capture=False, root=None, entry=None):
        self.base = Env()
        self.glob = Env(self.base)
        self.allow, self.fuel, self.used = set(allow), fuel, 0
        self.frames, self.depth = [], 0
        self.tests, self.out = [], []
        self.tracing, self.trace = trace, []
        self.capture = capture
        self.effects_used = set()
        self.root = os.path.realpath(root or os.getcwd())
        self.cur_dir = self.root
        self.modules, self.module_depth = {}, 0
        self.loading = [os.path.realpath(entry)] if entry else []
        self.special = {
            "def": self.c_def, "defn": self.c_defn, "fn": self.c_fn, "if": self.c_if,
            "let": self.c_let, "do": self.c_do, "and": self.c_and, "or": self.c_or,
            "cond": self.c_cond, "match": self.c_match, "?": self.c_try, "test": self.c_test,
            "import": self.c_import,
        }
        self._install()

    # ---- driver ----
    def run_forms(self, forms, env):
        val = None
        for node, _, _ in forms:
            try:
                val = self.comp(node)(env)
            except EarlyReturn as er:
                raise E("E_TRY_TOP", "'?' hit an err at top level: %s" % show(er.val), node,
                        "use '?' only inside a function, or (match ...) at top level")
        return val

    def run(self, src):
        return self.run_forms(parse_program(src)[0], self.glob)

    def eval(self, x, env):
        return self.comp(x)(env)

    def run_tests(self):
        results = []
        for name, exprs, node, env in self.tests:
            self.used = 0
            r = {"name": name, "ok": True}
            try:
                for e in exprs:
                    if isinstance(e, Form) and len(e) == 3 and isinstance(e[0], Sym) and e[0].name == "=":
                        a, b = self.eval(e[1], env), self.eval(e[2], env)
                        if not eq(a, b):
                            r.update(ok=False, expr=flat(e), left=to_json(a), right=to_json(b))
                            break
                    else:
                        v = self.eval(e, env)
                        if not truthy(v):
                            r.update(ok=False, expr=flat(e), value=to_json(v))
                            break
            except EarlyReturn as er:
                r.update(ok=False, error={"code": "E_TRY_TOP", "message": "'?' hit err: " + show(er.val)})
            except LageError as err:
                r.update(ok=False, error=err.to_dict())
            results.append(r)
        return results

    def emit(self, text):
        if self.capture:
            self.out.append(text)
        else:
            sys.stdout.write(text)

    # ---- compiler: AST -> python closures ----
    def comp(self, x, tail=False):
        if isinstance(x, Sym):
            return self.c_sym(x)
        if isinstance(x, Form):
            return self.c_form(x, tail)
        if isinstance(x, Vec):
            cs = [self.comp(e) for e in x]
            return lambda env: [c(env) for c in cs]
        if isinstance(x, Map):
            kvs = [(self.comp(k), self.comp(v)) for k, v in zip(x[::2], x[1::2])]

            def mk(env):
                d = {}
                for kc, vc in kvs:
                    k = kc(env)
                    if not isinstance(k, str):
                        raise E("E_TYPE", "map key must be keyword or string, got %s" % tname(k), x)
                    d[k] = vc(env)
                return d
            return mk
        return lambda env, v=x: v

    def c_sym(self, x):
        name = x.name

        def look(env):
            e = env
            while e is not None:
                v = e.v
                if name in v:
                    return v[name]
                e = e.p
            cands = list(env.names() | set(self.special))
            m = difflib.get_close_matches(name, cands, 3, 0.6)
            raise E("E_UNBOUND", "unbound symbol '%s'" % name, x,
                    ("did you mean: " + ", ".join(m)) if m else None)
        return look

    def annotate(self, e, x):
        if e.line is None:
            e.line, e.col = getattr(x, "line", None), getattr(x, "col", None)
        if e.stack is None:
            e.stack = [fr.fn.name for fr in reversed(self.frames)]

    def c_form(self, x, tail):
        if not x:
            raise E("E_SYNTAX", "empty call ()", x, "write nil for nothing, [] for an empty list")
        h = x[0]
        if isinstance(h, Sym) and h.name in self.special:
            return self.special[h.name](x, tail)
        hc = self.comp(h)
        acs = [self.comp(a) for a in x[1:]]

        def call(env):
            self.used += 1
            if self.used > self.fuel:
                e = E("E_FUEL", "fuel exhausted after %d steps" % self.fuel, x,
                      "possible infinite loop; raise --fuel if intended")
                self.annotate(e, x)
                raise e
            try:
                f = hc(env)
                args = [c(env) for c in acs]
                tf = type(f)
                if tf is Builtin and f.effect is None:
                    try:
                        return f.fn(*args)
                    except (LageError, EarlyReturn):
                        raise
                    except Exception as ex:  # noqa: BLE001
                        raise self.builtin_error(f, args, x, ex)
                if tail and tf is Fn:
                    return TailCall(f, args, x)
                return self.apply(f, args, x)
            except LageError as e:
                self.annotate(e, x)
                raise
        return call

    def need(self, eff, who, node):
        if self.frames:
            fr = self.frames[-1].fn
            if not covers(set(fr.effects), eff):
                raise E("E_EFFECT_UNDECLARED",
                        "%s needs effect '%s' but fn '%s' does not declare it" % (who, eff, fr.name), node,
                        "add (effects %s) to (defn %s ...)" % (eff, fr.name))
        if not covers(self.allow, eff):
            raise E("E_EFFECT_DENIED", "effect '%s' not granted (needed by %s)" % (eff, who), node,
                    "run with --allow %s" % eff)
        self.effects_used.add(eff)

    def builtin_error(self, f, args, node, ex):
        if isinstance(ex, RecursionError):
            return E("E_DEPTH", "recursion too deep", node)
        if isinstance(ex, TypeError):
            return E("E_TYPE", "%s: %s" % (f.name, ex), node, "args given: " + " ".join(tname(a) for a in args))
        return E("E_RUNTIME", "%s: %s" % (f.name, ex), node)

    def apply(self, f, args, node=None):
        if type(f) is Builtin:
            if f.effect:
                self.need(f.effect, "builtin " + f.name, node)
            try:
                return f.fn(*args)
            except (LageError, EarlyReturn):
                raise
            except Exception as ex:  # noqa: BLE001
                raise self.builtin_error(f, args, node, ex)
        if self.depth >= MAXDEPTH:
            raise E("E_DEPTH", "call depth exceeded %d" % MAXDEPTH, node,
                    "unbounded non-tail recursion? use a tail call, or reduce/map/range")
        self.depth += 1
        pushed, chain = False, []
        try:
            while True:
                if type(f) is not Fn:
                    raise E("E_NOT_FN", "cannot call %s: %s" % (tname(f), show(f)), node,
                            "first item of a call must be a function")
                if len(args) != len(f.params):
                    raise E("E_ARITY", "%s takes %d args [%s], got %d" % (
                        f.name or "fn", len(f.params), " ".join(f.params), len(args)), node)
                named = f.effects is not None
                if named:
                    for eff in f.effects:
                        self.need(eff, "fn " + f.name, node)
                    if pushed:
                        self.frames.pop()
                    self.frames.append(Frame(f))
                    pushed = True
                env = Env(f.env)
                env.v.update(zip(f.params, args))
                for rn, rc in f.requires:
                    if not truthy(rc(env)):
                        raise E("E_REQUIRES", "precondition failed in %s: %s" % (f.name, flat(rn)), rn,
                                "called with " + show(dict(zip(f.params, args))))
                try:
                    res = None
                    for b in f.body:
                        res = b(env)
                except EarlyReturn as er:
                    res = er.val
                if type(res) is TailCall:
                    if self.tracing:
                        chain.append((f, args))
                    f, args, node = res.f, res.args, res.node
                    continue
                if f.ensures:
                    env2 = Env(env)
                    env2.v["result"] = res
                    for rn, rc in f.ensures:
                        if not truthy(rc(env2)):
                            raise E("E_ENSURES", "postcondition failed in %s: %s" % (f.name, flat(rn)), rn,
                                    "result was %s; args %s" % (show(res), show(dict(zip(f.params, args)))))
                if self.tracing:
                    for cf, ca in chain + [(f, args)]:
                        if cf.effects is not None and len(self.trace) < 2000:
                            self.trace.append({"fn": cf.name, "args": to_json(ca), "ret": to_json(res)})
                return res
        finally:
            self.depth -= 1
            if pushed:
                self.frames.pop()

    # ---- special forms (compile-time) ----
    def make_fn(self, name, params, rest, node, named):
        if not isinstance(params, Vec) or not all(isinstance(p, Sym) for p in params):
            raise E("E_SYNTAX", "params must be a vector of symbols, e.g. [a b]", node)
        names = [p.name for p in params]
        if len(set(names)) != len(names):
            raise E("E_SYNTAX", "duplicate parameter name", node)
        rest, doc, eff, req, ens = list(rest), None, [], [], []
        if named:
            if len(rest) > 1 and isinstance(rest[0], str):
                doc = rest.pop(0)
            while rest and isinstance(rest[0], Form) and rest[0] and isinstance(rest[0][0], Sym) \
                    and rest[0][0].name in ("effects", "requires", "ensures"):
                c = rest.pop(0)
                k = c[0].name
                if k == "effects":
                    if not all(isinstance(s, Sym) for s in c[1:]):
                        raise E("E_SYNTAX", "effects takes symbols, e.g. (effects fs.read)", c)
                    eff += [s.name for s in c[1:]]
                elif k == "requires":
                    req += c[1:]
                else:
                    ens += c[1:]
        if not rest:
            raise E("E_SYNTAX", "function body is empty", node)
        body = [self.comp(b) for b in rest[:-1]] + [self.comp(rest[-1], tail=not ens)]
        reqs = [(r, self.comp(r)) for r in req]
        enss = [(r, self.comp(r)) for r in ens]
        effects = tuple(eff) if named else None
        return lambda env: Fn(name, names, body, env, effects, reqs, enss, doc)

    def c_defn(self, x, tail):
        if len(x) < 4 or not isinstance(x[1], Sym):
            raise E("E_SYNTAX", "syntax: (defn name [params] body...)", x, "example: (defn add [a b] (+ a b))")
        mk, name = self.make_fn(x[1].name, x[2], x[3:], x, True), x[1].name

        def run(env):
            fn = env.v[name] = mk(env)
            return fn
        return run

    def c_fn(self, x, tail):
        if len(x) < 3:
            raise E("E_SYNTAX", "syntax: (fn [params] body...)", x)
        return self.make_fn(None, x[1], x[2:], x, False)

    def c_def(self, x, tail):
        if len(x) != 3 or not isinstance(x[1], Sym):
            raise E("E_SYNTAX", "syntax: (def name expr)", x)
        name, vc = x[1].name, self.comp(x[2])

        def run(env):
            v = vc(env)
            if isinstance(v, Fn) and v.name is None:
                v.name = name
            env.v[name] = v
            return v
        return run

    def c_if(self, x, tail):
        if len(x) not in (3, 4):
            raise E("E_SYNTAX", "syntax: (if cond then [else])", x, "missing else returns nil")
        c, a = self.comp(x[1]), self.comp(x[2], tail)
        b = self.comp(x[3], tail) if len(x) == 4 else (lambda env: None)
        return lambda env: a(env) if truthy(c(env)) else b(env)

    def c_let(self, x, tail):
        if len(x) < 3 or not isinstance(x[1], Vec) or len(x[1]) % 2 or \
                not all(isinstance(s, Sym) for s in x[1][::2]):
            raise E("E_SYNTAX", "syntax: (let [name expr ...] body...)", x)
        binds = [(s.name, self.comp(v)) for s, v in zip(x[1][::2], x[1][1::2])]
        body = [self.comp(b) for b in x[2:-1]] + [self.comp(x[-1], tail)]

        def run(env):
            e = Env(env)
            for n, c in binds:
                e.v[n] = c(e)
            res = None
            for b in body:
                res = b(e)
            return res
        return run

    def c_do(self, x, tail):
        if len(x) == 1:
            return lambda env: None
        body = [self.comp(b) for b in x[1:-1]] + [self.comp(x[-1], tail)]

        def run(env):
            res = None
            for b in body:
                res = b(env)
            return res
        return run

    def c_and(self, x, tail):
        if len(x) == 1:
            return lambda env: True
        cs = [self.comp(b) for b in x[1:-1]] + [self.comp(x[-1], tail)]

        def run(env):
            v = True
            for c in cs:
                v = c(env)
                if not truthy(v):
                    return v
            return v
        return run

    def c_or(self, x, tail):
        if len(x) == 1:
            return lambda env: False
        cs = [self.comp(b) for b in x[1:-1]] + [self.comp(x[-1], tail)]

        def run(env):
            v = False
            for c in cs:
                v = c(env)
                if truthy(v):
                    return v
            return v
        return run

    def c_cond(self, x, tail):
        if len(x) % 2 == 0:
            raise E("E_SYNTAX", "cond needs test/expr pairs", x, "end with :else expr")
        pairs = [(self.comp(t), self.comp(v, tail)) for t, v in zip(x[1::2], x[2::2])]

        def run(env):
            for t, v in pairs:
                if truthy(t(env)):
                    return v(env)
            return None
        return run

    def c_match(self, x, tail):
        if len(x) < 4 or len(x) % 2 != 0:
            raise E("E_SYNTAX", "syntax: (match expr (ok v) body (err e) body)", x)
        ec, clauses = self.comp(x[1]), []
        for pat, body in zip(x[2::2], x[3::2]):
            if not (isinstance(pat, Form) and len(pat) == 2 and isinstance(pat[0], Sym)
                    and pat[0].name in ("ok", "err") and isinstance(pat[1], Sym)):
                raise E("E_SYNTAX", "pattern must be (ok name) or (err name)", pat if isinstance(pat, Form) else x)
            clauses.append((pat[0].name == "ok", pat[1].name, self.comp(body, tail)))

        def run(env):
            r = ec(env)
            if not isinstance(r, Res):
                raise E("E_TYPE", "match expects a result (ok/err), got %s: %s" % (tname(r), show(r)), x)
            for is_ok, var, body in clauses:
                if is_ok == r.ok:
                    e = Env(env)
                    e.v[var] = r.val
                    return body(e)
            raise E("E_MATCH", "no clause matched %s" % show(r), x, "add the missing (ok v) or (err e) clause")
        return run

    def c_try(self, x, tail):
        if len(x) != 2:
            raise E("E_SYNTAX", "syntax: (? expr)", x)
        ec = self.comp(x[1])

        def run(env):
            v = ec(env)
            if not isinstance(v, Res):
                raise E("E_TYPE", "? expects a result, got %s: %s" % (tname(v), show(v)), x)
            if v.ok:
                return v.val
            raise EarlyReturn(v)
        return run

    def c_test(self, x, tail):
        if len(x) < 3 or not isinstance(x[1], str):
            raise E("E_SYNTAX", 'syntax: (test "name" expr...)', x, 'example: (test "adds" (= (add 1 2) 3))')

        def run(env):
            if self.module_depth == 0:
                self.tests.append((x[1], list(x[2:]), x, env))
            return None
        return run

    def c_import(self, x, tail):
        ok = len(x) in (2, 4) and isinstance(x[1], str) and (
            len(x) == 2 or (x[2] == "as" and isinstance(x[2], Kw) and isinstance(x[3], Sym)))
        if not ok:
            raise E("E_SYNTAX", 'syntax: (import "path.lg" :as alias)', x,
                    "then call alias/name; names starting with _ stay private")
        path = x[1]
        alias = x[3].name if len(x) == 4 else os.path.splitext(os.path.basename(path))[0]

        def run(env):
            menv = self.load_module(path, x)
            for n, v in menv.v.items():
                if not n.startswith("_") and "/" not in n:
                    env.v[alias + "/" + n] = v
            return None
        return run

    def resolve_module(self, path, node):
        p = os.path.realpath(os.path.join(self.cur_dir, path))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise E("E_IMPORT", "import %s is outside the project root" % json.dumps(path), node,
                    "imports must stay inside the directory of the entry file")
        return p

    def load_module(self, path, node):
        p = self.resolve_module(path, node)
        if p in self.modules:
            return self.modules[p]
        if p in self.loading:
            chain = [os.path.relpath(q, self.root) for q in self.loading + [p]]
            raise E("E_IMPORT", "import cycle: " + " -> ".join(chain), node)
        try:
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as e:
            raise E("E_IMPORT", "cannot read module %s: %s" % (json.dumps(path), e.strerror), node)
        menv, saved = Env(self.base), self.cur_dir
        self.loading.append(p)
        self.module_depth += 1
        self.cur_dir = os.path.dirname(p)
        try:
            self.run_forms(parse_program(src)[0], menv)
        except LageError as e:
            if e.file is None:
                e.file, e.src = os.path.relpath(p, self.root), src
            raise
        finally:
            self.loading.pop()
            self.module_depth -= 1
            self.cur_dir = saved
        self.modules[p] = menv
        return menv


    # ---- builtins ----
    def _install(self):
        s = self

        def reg(name, fn, effect=None):
            s.base.v[name] = Builtin(name, fn, effect)

        def add(*a):
            if len(a) == 2 and type(a[0]) is int and type(a[1]) is int:
                return a[0] + a[1]
            if a and all(isinstance(v, str) for v in a):
                raise E("E_TYPE", "+ expects numbers, got str", hint="use (concat ...) or (str ...)")
            return sum(num(v, "+") for v in a)

        def sub(a, *r):
            if len(r) == 1 and type(a) is int and type(r[0]) is int:
                return a - r[0]
            num(a, "-")
            return -a if not r else a - sum(num(v, "-") for v in r)

        def mul(*a):
            if len(a) == 2 and type(a[0]) is int and type(a[1]) is int:
                return a[0] * a[1]
            p = 1
            for v in a:
                p *= num(v, "*")
            return p

        def div(a, b):
            num(a, "/"), num(b, "/")
            if b == 0:
                raise E("E_DIV", "division by zero")
            if isinstance(a, int) and isinstance(b, int) and a % b == 0:
                return a // b
            return a / b

        def mod(a, b):
            num(a, "mod"), num(b, "mod")
            if b == 0:
                raise E("E_DIV", "modulo by zero")
            return a % b

        def cmp(name, op):
            def f(a, b):
                if type(a) is int and type(b) is int:
                    return op(a, b)
                if isinstance(a, bool) or isinstance(b, bool) or type(a) is not type(b) and not (
                        isinstance(a, (int, float)) and isinstance(b, (int, float))):
                    raise E("E_TYPE", "%s cannot compare %s with %s" % (name, tname(a), tname(b)))
                return op(a, b)
            return f

        reg("+", add), reg("-", sub), reg("*", mul), reg("/", div), reg("mod", mod)
        reg("=", lambda a, b: eq(a, b)), reg("!=", lambda a, b: not eq(a, b))
        reg("<", cmp("<", lambda a, b: a < b)), reg(">", cmp(">", lambda a, b: a > b))
        reg("<=", cmp("<=", lambda a, b: a <= b)), reg(">=", cmp(">=", lambda a, b: a >= b))
        reg("not", lambda a: not truthy(a))

        # collections
        def nth(xs, i):
            if not isinstance(xs, list) or isinstance(i, bool) or not isinstance(i, int):
                raise E("E_TYPE", "nth expects (list int)")
            if not 0 <= i < len(xs):
                raise E("E_INDEX", "index %d out of range for list of length %d" % (i, len(xs)),
                        hint="use (get xs i default) for a safe lookup")
            return xs[i]

        def get(c, k, d=None):
            if isinstance(c, dict):
                return c.get(k, d)
            if isinstance(c, list) and isinstance(k, int) and not isinstance(k, bool):
                return c[k] if 0 <= k < len(c) else d
            raise E("E_TYPE", "get expects a map or list, got %s" % tname(c))

        def assoc(m, *kv):
            if not isinstance(m, dict) or len(kv) % 2:
                raise E("E_TYPE", "assoc expects (map key value ...)")
            r = dict(m)
            r.update(zip(kv[::2], kv[1::2]))
            return r

        def dissoc(m, *ks):
            return {k: v for k, v in m.items() if k not in ks}

        def rng(*a):
            return list(range(*a))

        def mapf(f, xs):
            return [s.apply(f, [x]) for x in xs]

        def filt(f, xs):
            return [x for x in xs if truthy(s.apply(f, [x]))]

        def red(f, init, xs):
            acc = init
            for x in xs:
                acc = s.apply(f, [acc, x])
            return acc

        def srt(xs, key=None):
            if key is None:
                return sorted(xs)
            return [x for _, x in sorted(((s.apply(key, [x]), x) for x in xs), key=lambda p: p[0])]

        def concat(*a):
            if all(isinstance(v, str) for v in a):
                return "".join(a)
            if all(isinstance(v, list) for v in a):
                return [x for v in a for x in v]
            raise E("E_TYPE", "concat expects all strings or all lists")

        def contains(c, x):
            if isinstance(c, dict):
                return x in c
            return any(eq(x, y) for y in c) if isinstance(c, list) else x in c

        reg("len", len), reg("first", lambda xs: xs[0] if xs else None)
        reg("rest", lambda xs: xs[1:]), reg("last", lambda xs: xs[-1] if xs else None)
        reg("cons", lambda x, xs: [x] + xs), reg("append", lambda xs, x: xs + [x])
        reg("nth", nth), reg("get", get), reg("assoc", assoc), reg("dissoc", dissoc)
        reg("keys", lambda m: list(m.keys())), reg("vals", lambda m: list(m.values()))
        reg("has?", lambda m, k: k in m), reg("contains?", contains)
        reg("range", rng), reg("map", mapf), reg("filter", filt), reg("reduce", red)
        reg("sort", srt), reg("reverse", lambda xs: xs[::-1]), reg("concat", concat)
        reg("sum", lambda xs: sum(num(v, "sum") for v in xs))
        reg("min", lambda xs: min(xs)), reg("max", lambda xs: max(xs))

        # strings
        reg("str", lambda *a: "".join(display(v) for v in a))
        reg("split", lambda t, sep: t.split(sep)), reg("join", lambda sep, xs: sep.join(xs))
        reg("trim", lambda t: t.strip()), reg("upper", lambda t: t.upper()), reg("lower", lambda t: t.lower())
        reg("replace", lambda t, a, b: t.replace(a, b))
        reg("starts-with?", lambda t, p: t.startswith(p)), reg("ends-with?", lambda t, p: t.endswith(p))

        def parse_int(t):
            try:
                return Res(True, int(t))
            except (ValueError, TypeError):
                return Res(False, "not an integer: " + show(t))

        def from_json(t):
            try:
                return Res(True, json.loads(t))
            except (ValueError, TypeError) as e:
                return Res(False, "invalid json: %s" % e)

        reg("parse-int", parse_int), reg("from-json", from_json)
        reg("to-json", lambda v: json.dumps(to_json(v), ensure_ascii=False, separators=(",", ":")))

        # results / misc
        reg("ok", lambda v: Res(True, v)), reg("err", lambda v: Res(False, v))
        reg("ok?", lambda r: r.ok), reg("err?", lambda r: not r.ok)
        reg("val", lambda r: r.val)
        reg("unwrap-or", lambda r, d: r.val if r.ok else d)
        reg("type", tname)

        def assert_(c, msg="assertion failed"):
            if not truthy(c):
                raise E("E_ASSERT", display(msg))
            return None

        reg("assert", assert_)

        def pr(*a):
            s.emit(" ".join(display(v) for v in a) + "\n")

        reg("print", pr)

        # effects
        def read_file(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    return Res(True, fh.read())
            except OSError as e:
                return Res(False, e.strerror or str(e))

        def write_file(p, t):
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(t)
                return Res(True, len(t))
            except OSError as e:
                return Res(False, e.strerror or str(e))

        def http_get(u):
            try:
                with urllib.request.urlopen(u, timeout=15) as r:  # noqa: S310
                    return Res(True, r.read().decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001
                return Res(False, str(e))

        def now():
            import time
            return time.time()

        reg("read-file", read_file, "fs.read"), reg("write-file", write_file, "fs.write")
        reg("http-get", http_get, "net"), reg("now", now, "time")
