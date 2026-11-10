"""Gradual static types for lage.

Annotate a function with a `sig` clause:

    (defn add [a b] (sig [int int] int) (+ a b))
    (defn first-or [xs d] (sig [(list t) t] t) ...)        ; lowercase unknown names are type variables

Types: any int float num str bool nil | (list T) (map T) (result T E) (fn [T..] R) (or T..) (opt T)
`any` and unannotated code are never an error source (gradual). Sigs are checked statically only.
Optimistic where idiomatic: `first`/`nth`/`get` return the element type, not (or T nil).
"""
from .core import Form, Kw, LageError, Map, Sym, Vec, param_names, parse_params, parse_program

ANY, INT, FLOAT, NUM, STR, BOOL, NIL = (("any",), ("int",), ("float",), ("num",), ("str",), ("bool",), ("nil",))
BASES = {"any": ANY, "int": INT, "float": FLOAT, "num": NUM, "str": STR, "bool": BOOL, "nil": NIL}


def show_type(t):
    k = t[0]
    if k in BASES:
        return k
    if k == "var":
        return t[1]
    if k in ("list", "map"):
        return "(%s %s)" % (k, show_type(t[1]))
    if k == "result":
        return "(result %s %s)" % (show_type(t[1]), show_type(t[2]))
    if k == "fn":
        ps = [show_type(p) for p in t[1]]
        if len(t) > 3 and t[3] is not None:
            ps += ["&", show_type(t[3])]
        return "(fn [%s] %s)" % (" ".join(ps), show_type(t[2]))
    if k == "or":
        return "(or %s)" % " ".join(show_type(m) for m in t[1])
    return str(t)


# ---- parsing type expressions ----
class TypeSyntax(Exception):
    def __init__(self, msg, node=None):
        super().__init__(msg)
        self.node = node


def parse_type(x):
    if x is None:
        return NIL
    if isinstance(x, Sym):
        return BASES.get(x.name) or ("var", x.name)
    if isinstance(x, Form) and x and isinstance(x[0], Sym):
        h, a = x[0].name, x[1:]
        if h in ("list", "map") and len(a) == 1:
            return (h, parse_type(a[0]))
        if h == "opt" and len(a) == 1:
            return ("or", (parse_type(a[0]), NIL))
        if h == "result" and len(a) == 2:
            return ("result", parse_type(a[0]), parse_type(a[1]))
        if h == "or" and a:
            return ("or", tuple(parse_type(m) for m in a))
        if h == "fn" and len(a) == 2 and isinstance(a[0], Vec):
            ps = [p for p in a[0]]
            rest = None
            if len(ps) >= 2 and isinstance(ps[-2], Sym) and ps[-2].name == "&":
                rest = parse_type(ps[-1])
                ps = ps[:-2]
            return ("fn", tuple(parse_type(p) for p in ps), parse_type(a[1]), rest)
    raise TypeSyntax("bad type %r" % _src(x), x)


def _src(x):
    from .core import flat
    return flat(x) if isinstance(x, (Form, Vec, Map, Sym)) else str(x)


def parse_sig(c):
    """(sig [T..] R) / (sig [T.. & T] R) -> ('fn', params, ret, rest type | None)"""
    if len(c) != 3 or not isinstance(c[1], Vec):
        raise TypeSyntax("syntax: (sig [param-types] return-type)", c)
    ps, rest = list(c[1]), None
    for i, p in enumerate(ps):
        if isinstance(p, Sym) and p.name == "&":
            if i != len(ps) - 2:
                raise TypeSyntax("& in a sig must be followed by exactly one type, e.g. (sig [int & str] int)", c)
            rest = parse_type(ps[-1])
            ps = ps[:i]
            break
    return ("fn", tuple(parse_type(p) for p in ps), parse_type(c[2]), rest)


def fn_lo(t):
    """minimum arg count of a fn type (a 5th slot, set for fns with optional params)"""
    return t[4] if len(t) > 4 else len(t[1])


def _fn(src):
    t = parse_type(parse_program(src)[0][0][0])
    return t


BUILTIN_SIGS = {name: _fn(sig) for name, sig in {
    "=": "(fn [any any] bool)", "!=": "(fn [any any] bool)", "not": "(fn [any] bool)",
    "<": "(fn [(or num str) (or num str)] bool)", ">": "(fn [(or num str) (or num str)] bool)",
    "<=": "(fn [(or num str) (or num str)] bool)", ">=": "(fn [(or num str) (or num str)] bool)",
    "len": "(fn [(or str (list any) (map any))] int)",
    "first": "(fn [(list a)] a)", "last": "(fn [(list a)] a)", "rest": "(fn [(list a)] (list a))",
    "cons": "(fn [a (list a)] (list a))", "append": "(fn [(list a) a] (list a))",
    "nth": "(fn [(list a) int] a)",
    "assoc": "(fn [(map a) & any] (map a))", "dissoc": "(fn [(map a) & str] (map a))",
    "keys": "(fn [(map any)] (list str))", "vals": "(fn [(map a)] (list a))",
    "has?": "(fn [(map any) str] bool)", "contains?": "(fn [any any] bool)",
    "range": "(fn [int & int] (list int))",
    "map": "(fn [(fn [a] b) (list a)] (list b))",
    "filter": "(fn [(fn [a] any) (list a)] (list a))",
    "reduce": "(fn [(fn [b a] b) b (list a)] b)",
    "sort": "(fn [(list a) & any] (list a))", "reverse": "(fn [(list a)] (list a))",
    "min": "(fn [(list a)] a)", "max": "(fn [(list a)] a)",
    "str": "(fn [& any] str)", "split": "(fn [str str] (list str))", "join": "(fn [str (list str)] str)",
    "trim": "(fn [str] str)", "upper": "(fn [str] str)", "lower": "(fn [str] str)",
    "replace": "(fn [str str str] str)", "starts-with?": "(fn [str str] bool)",
    "ends-with?": "(fn [str str] bool)",
    "parse-int": "(fn [str] (result int str))", "from-json": "(fn [str] (result any str))",
    "to-json": "(fn [any] str)",
    "ok": "(fn [a] (result a any))", "err": "(fn [e] (result any e))",
    "ok?": "(fn [(result any any)] bool)", "err?": "(fn [(result any any)] bool)",
    "val": "(fn [(result a any)] a)", "unwrap-or": "(fn [(result a any) a] a)",
    "type": "(fn [any] str)", "assert": "(fn [any & any] nil)", "print": "(fn [& any] nil)",
    "read-file": "(fn [str] (result str str))", "write-file": "(fn [str str] (result int str))",
    "http-get": "(fn [str] (result str str))", "now": "(fn [] float)",
    "floor": "(fn [num] int)", "ceil": "(fn [num] int)", "round": "(fn [num] int)",
    "pow": "(fn [num num] num)", "sqrt": "(fn [num] float)",
    "chars": "(fn [str] (list str))", "index-of": "(fn [any any] int)",
}.items()}


# ---- relations ----
def bind_of(v, s):
    return s.get(v[1])


def compat(e, a, s):
    """May a value of type `a` be used where `e` is expected? Binds type variables in `s`."""
    if e[0] == "any" or a[0] == "any":
        return True
    if e[0] == "var":
        b = s.get(e[1])
        if b is None:
            s[e[1]] = a
            return True
        return compat(b, a, s) or compat(a, b, s)
    if a[0] == "var":
        b = s.get(a[1])
        return True if b is None else compat(e, b, s)
    if a[0] == "or":
        return all(compat(e, m, s) for m in a[1])
    if e[0] == "or":
        for m in e[1]:
            s2 = dict(s)
            if compat(m, a, s2):
                s.update(s2)
                return True
        return False
    ek, ak = e[0], a[0]
    if ek == "num":
        return ak in ("int", "float", "num")
    if ek == "float":
        return ak in ("int", "float", "num")
    if ek in ("int", "str", "bool", "nil"):
        return ek == ak
    if ek in ("list", "map"):
        return ak == ek and compat(e[1], a[1], s)
    if ek == "result":
        return ak == "result" and compat(e[1], a[1], s) and compat(e[2], a[2], s)
    if ek == "fn":
        if ak != "fn":
            return False
        if len(e[1]) != len(a[1]) or e[3] is not None or a[3] is not None or fn_lo(a) != len(a[1]):
            ne = len(e[1])  # variadic / optional: the actual fn must accept exactly the expected arg count
            if e[3] is not None and a[3] is None:
                return False
            if not (fn_lo(a) <= ne and (ne <= len(a[1]) or a[3] is not None)):
                return False
            ps = [a[1][i] if i < len(a[1]) else a[3] for i in range(ne)]
        else:
            ps = a[1]
        if not all(compat(ap, ep, s) for ap, ep in zip(ps, e[1])):
            return False
        return compat(e[2], a[2], s)
    return False


def subst(t, s):
    k = t[0]
    if k == "var":
        b = s.get(t[1])
        return ANY if b is None else subst(b, {kk: vv for kk, vv in s.items() if kk != t[1]})
    if k in ("list", "map"):
        return (k, subst(t[1], s))
    if k == "result":
        return ("result", subst(t[1], s), subst(t[2], s))
    if k == "fn":
        return ("fn", tuple(subst(p, s) for p in t[1]), subst(t[2], s), t[3] and subst(t[3], s)) + t[4:]
    if k == "or":
        return ("or", tuple(subst(m, s) for m in t[1]))
    return t


def skolem(t):
    """Inside a generic function's own body, its type variables are opaque: treat as any."""
    k = t[0]
    if k == "var":
        return ANY
    if k in ("list", "map"):
        return (k, skolem(t[1]))
    if k == "result":
        return ("result", skolem(t[1]), skolem(t[2]))
    if k == "fn":
        return ("fn", tuple(skolem(p) for p in t[1]), skolem(t[2]), t[3] and skolem(t[3])) + t[4:]
    if k == "or":
        return ("or", tuple(skolem(m) for m in t[1]))
    return t


def join(a, b):
    if a == b:
        return a
    if a[0] == "any" or b[0] == "any":
        return ANY
    if {a[0], b[0]} <= {"int", "float", "num"}:
        return NUM
    if a[0] == b[0] == "result":
        return ("result", join(a[1], b[1]), join(a[2], b[2]))
    if a[0] == b[0] and a[0] in ("list", "map"):
        return (a[0], join(a[1], b[1]))
    ms = []
    for t in (a, b):
        for m in (t[1] if t[0] == "or" else (t,)):
            if m not in ms:
                ms.append(m)
    return ("or", tuple(ms)) if len(ms) <= 3 else ANY


def join_all(ts):
    out = ts[0]
    for t in ts[1:]:
        out = join(out, t)
    return out


class TypeChecker:
    def __init__(self, imports=None):
        self.out = []
        self.top = dict(imports or {})

    def diag(self, code, msg, node, hint=None):
        self.out.append((code, msg, node, hint))

    # ---- module driver ----
    def run(self, forms):
        for f in forms:
            if isinstance(f, Form) and len(f) >= 3 and isinstance(f[0], Sym) and f[0].name == "defn" \
                    and isinstance(f[1], Sym) and isinstance(f[2], Vec):
                self.top[f[1].name] = self.declared(f, report=False)
        for f in forms:
            self.infer(f, dict(self.top), None, top=True)
        return {n: t for n, t in self.top.items() if t[0] == "fn"}

    def declared(self, f, report=True):
        """Declared fn type of a defn form (from its sig), or any-typed with correct arity."""
        try:
            preq, popt, prest = parse_params(f[2], f)
        except LageError:
            preq, popt, prest = param_names(f[2]), [], None
        n = len(preq) + len(popt)
        dflt = ("fn", (ANY,) * n, ANY, ANY if prest else None) + ((len(preq),) if popt else ())
        for c in f[3:]:
            if isinstance(c, Form) and c and isinstance(c[0], Sym) and c[0].name == "sig":
                try:
                    t = parse_sig(c)
                except TypeSyntax as e:
                    if report:
                        self.diag("E_SIG", str(e), e.node if hasattr(e.node, "line") else c,
                                  "types: any int float num str bool nil (list T) (map T) (result T E) (fn [T] R) (or T U) (opt T)")
                    break
                if len(t[1]) != n or (t[3] is None) != (prest is None):
                    if report:
                        self.diag("E_SIG", "sig lists %d param types%s but fn %s takes %d%s" % (
                            len(t[1]), " + & rest" if t[3] is not None else "", f[1].name, n,
                            " + & rest" if prest else ""), c,
                                  "list the rest element type after &: (sig [int & str] int)" if prest else None)
                    break
                return t + ((len(preq),) if popt else ())
        return dflt

    # ---- inference ----
    def infer(self, x, env, ret, top=False):
        if isinstance(x, bool):
            return BOOL
        if x is None:
            return NIL
        if isinstance(x, int):
            return INT
        if isinstance(x, float):
            return FLOAT
        if isinstance(x, str):
            return STR
        if isinstance(x, Sym):
            return env.get(x.name) or self.top.get(x.name) or BUILTIN_SIGS.get(x.name) or ANY
        if isinstance(x, Vec):
            ts = [self.infer(e, env, ret) for e in x]
            return ("list", join_all(ts) if ts else ANY)
        if isinstance(x, Map):
            ts = [self.infer(v, env, ret) for k, v in zip(x[::2], x[1::2])]
            for k in x[::2]:
                self.infer(k, env, ret)
            return ("map", join_all(ts) if ts else ANY)
        if not isinstance(x, Form) or not x:
            return ANY
        h = x[0]
        if isinstance(h, Sym) and h.name not in env:
            m = getattr(self, "s_" + h.name.replace("?", "_try").replace("-", "_"), None)
            if h.name in ("def", "defn", "fn", "if", "let", "do", "and", "or", "cond", "match", "?", "test", "import") and m:
                return m(x, env, ret, top)
        r = self.builtin_special(x, env, ret)
        return r if r is not None else self.call(x, env, ret)

    def body(self, xs, env, ret):
        t = NIL
        for b in xs:
            t = self.infer(b, env, ret)
        return t

    def s_if(self, x, env, ret, top):
        if len(x) not in (3, 4):
            return ANY
        self.infer(x[1], env, ret)
        a = self.infer(x[2], env, ret)
        b = self.infer(x[3], env, ret) if len(x) == 4 else NIL
        return join(a, b)

    def s_cond(self, x, env, ret, top):
        ts, has_else = [], False
        for t, v in zip(x[1::2], x[2::2]):
            self.infer(t, env, ret)
            ts.append(self.infer(v, env, ret))
            if isinstance(t, Kw) or t is True:
                has_else = True
        if not has_else:
            ts.append(NIL)
        return join_all(ts) if ts else NIL

    def s_let(self, x, env, ret, top):
        e = dict(env)
        if len(x) < 3 or not isinstance(x[1], Vec):
            return ANY
        for s, v in zip(x[1][::2], x[1][1::2]):
            if isinstance(s, Sym):
                e[s.name] = self.infer(v, e, ret)
        return self.body(x[2:], e, ret)

    def s_do(self, x, env, ret, top):
        return self.body(x[1:], env, ret)

    def s_and(self, x, env, ret, top):
        for e in x[1:]:
            self.infer(e, env, ret)
        return ANY

    def s_or(self, x, env, ret, top):
        ts = [self.infer(e, env, ret) for e in x[1:]]
        return join_all(ts) if ts else BOOL

    def s_import(self, x, env, ret, top):
        return NIL

    def s_test(self, x, env, ret, top):
        for e in x[2:]:
            self.infer(e, dict(self.top), None)
        return NIL

    def s_def(self, x, env, ret, top):
        if len(x) != 3 or not isinstance(x[1], Sym):
            return ANY
        t = self.infer(x[2], env, ret)
        env[x[1].name] = t
        if top:
            self.top[x[1].name] = t
        return t

    def s_match(self, x, env, ret, top):
        if len(x) < 4:
            return ANY
        rt = self.infer(x[1], env, ret)
        if rt[0] not in ("result", "any", "var"):
            self.diag("E_TYPE_MISMATCH", "match expects a result, got %s" % show_type(rt), x)
        ts = []
        for pat, body in zip(x[2::2], x[3::2]):
            if not (isinstance(pat, Form) and len(pat) == 2 and isinstance(pat[1], Sym)):
                continue
            e = dict(env)
            vt = ANY
            if rt[0] == "result":
                vt = rt[1] if pat[0].name == "ok" else rt[2]
            e[pat[1].name] = vt
            ts.append(self.infer(body, e, ret))
        return join_all(ts) if ts else ANY

    def s__try(self, x, env, ret, top):
        if len(x) != 2:
            return ANY
        rt = self.infer(x[1], env, ret)
        if rt[0] not in ("result", "any", "var"):
            self.diag("E_TYPE_MISMATCH", "? expects a result, got %s" % show_type(rt), x)
            return ANY
        if ret is not None and ret[0] not in ("result", "any", "var"):
            self.diag("E_TYPE_MISMATCH", "? returns the err early, but this fn is declared to return %s" % show_type(ret), x,
                      "declare the return type as (result T E)")
        return rt[1] if rt[0] == "result" else ANY

    def s_fn(self, x, env, ret, top, expect=None):
        if len(x) < 3 or not isinstance(x[1], Vec):
            return ANY
        e = dict(env)
        ps = []
        try:
            preq, popt, prest = parse_params(x[1], x)
        except LageError:
            return ANY
        for i, n in enumerate(preq + [n for n, _ in popt]):
            t = expect[i] if expect is not None and i < len(expect) else ANY
            e[n] = t
            ps.append(t)
        for n, d in popt:
            self.infer(d, e, ret)
        if prest:
            e[prest] = ("list", ANY)
        rt = self.body(x[2:], e, ret)
        return ("fn", tuple(ps), rt, ANY if prest else None) + ((len(preq),) if popt else ())

    def s_defn(self, x, env, ret, top):
        if len(x) < 4 or not isinstance(x[1], Sym) or not isinstance(x[2], Vec):
            return ANY
        t = self.declared(x)
        if top:
            self.top[x[1].name] = t
        env[x[1].name] = t
        sk = skolem(t)
        e = dict(env)
        try:
            preq, popt, prest = parse_params(x[2], x)
        except LageError:
            return t
        for n, pt in zip(preq + [n for n, _ in popt], sk[1]):
            e[n] = pt
        if prest:
            e[prest] = ("list", sk[3] if sk[3] is not None else ANY)
        for (n, d), pt in zip(popt, sk[1][len(preq):]):
            dt = self.infer(d, e, sk[2])
            if not compat(pt, dt, {}):
                self.diag("E_TYPE_MISMATCH", "default of %s is %s but its param type is %s" % (
                    n, show_type(dt), show_type(pt)), d if hasattr(d, "line") and d.line else x)
        body = list(x[3:])
        if len(body) > 1 and isinstance(body[0], str):
            body = body[1:]
        reqs, enss = [], []
        while body and isinstance(body[0], Form) and body[0] and isinstance(body[0][0], Sym) \
                and body[0][0].name in ("effects", "requires", "ensures", "sig"):
            c = body.pop(0)
            if c[0].name == "requires":
                reqs += c[1:]
            elif c[0].name == "ensures":
                enss += c[1:]
        for r in reqs:
            self.infer(r, e, sk[2])
        for r in enss:
            self.infer(r, dict(e, result=sk[2]), sk[2])
        last = self.body(body, e, sk[2])
        declared_ret = sk[2]
        if declared_ret[0] != "any" and body and not compat(declared_ret, last, {}):
            node = body[-1] if hasattr(body[-1], "line") else x
            self.diag("E_TYPE_MISMATCH", "fn %s is declared to return %s but its body returns %s" % (
                x[1].name, show_type(declared_ret), show_type(last)), node)
        return t

    def call(self, x, env, ret):
        h = x[0]
        if not isinstance(h, (Sym, Form)):  # literal head: reported as E_NOT_FN by the scope checker
            for a in x[1:]:
                self.infer(a, env, ret)
            return ANY
        name = h.name if isinstance(h, Sym) else None
        ft = self.infer(h, env, ret) if not isinstance(h, Sym) else (
            env.get(name) or self.top.get(name) or BUILTIN_SIGS.get(name) or ANY)
        args = x[1:]
        if ft[0] != "fn":
            ts = [self.infer(a, env, ret) for a in args]
            if ft[0] not in ("any", "var"):
                self.diag("E_TYPE_MISMATCH", "cannot call a value of type %s" % show_type(ft), x)
            return ANY
        params, rtype, rest = ft[1], ft[2], ft[3] if len(ft) > 3 else None
        n = len(args)
        if n < fn_lo(ft) or (n > len(params) and rest is None):
            for a in args:
                self.infer(a, env, ret)
            return subst(rtype, {})  # arity is reported by the scope checker
        expected = [params[i] if i < len(params) else rest for i in range(n)]
        s, at = {}, [None] * n
        # pass 1: args not expected to be functions bind the type variables; pass 2: function-typed
        # args (lambdas and named fns) are then checked against the bound element types
        for i, a in enumerate(args):
            if expected[i][0] != "fn":
                at[i] = self.infer(a, env, ret)
                self.check_arg(name, i, expected[i], at[i], s, a, x)
        for i, a in enumerate(args):
            if at[i] is None:
                exp = expected[i]
                if isinstance(a, Form) and a and isinstance(a[0], Sym) and a[0].name == "fn":
                    hint = tuple(subst(p, s) for p in exp[1])
                    at[i] = self.s_fn(a, env, ret, False, expect=hint)
                else:
                    at[i] = self.infer(a, env, ret)
                self.check_arg(name, i, exp, at[i], s, a, x)
        return subst(rtype, s)

    def check_arg(self, name, i, exp, act, s, a, call):
        if not compat(exp, act, s):
            node = a if hasattr(a, "line") and a.line else call
            self.diag("E_TYPE_MISMATCH", "argument %d of %s expects %s, got %s" % (
                i + 1, name or "call", show_type(subst(exp, s)), show_type(act)), node)

    def builtin_special(self, x, env, ret):
        """Builtins whose result type depends on argument types. Returns None if not applicable."""
        h = x[0]
        if not isinstance(h, Sym) or h.name in env or h.name in self.top or h.name not in SPECIAL_CALLS:
            return None
        name = h.name
        at = [self.infer(a, env, ret) for a in x[1:]]
        if name in ("+", "-", "*", "/", "mod"):
            for i, t in enumerate(at):
                if not compat(NUM, t, {}):
                    node = x[i + 1] if hasattr(x[i + 1], "line") and x[i + 1].line else x
                    self.diag("E_TYPE_MISMATCH", "argument %d of %s expects num, got %s" % (i + 1, name, show_type(t)),
                              node, "use (str ...) or (concat ...) to join strings" if t[0] == "str" else None)
                    return ANY
            if any(t[0] in ("any", "var") for t in at):
                return ANY
            if name == "/":
                return ANY  # exact int division gives int, otherwise float
            if all(t[0] == "int" for t in at):
                return INT
            return FLOAT if any(t[0] == "float" for t in at) else NUM
        first = at[0] if at else ANY
        if name == "abs":
            return first if first[0] in ("int", "float") else NUM
        if name in ("min", "max"):
            return first[1] if first[0] == "list" else ANY
        if name == "get":
            return first[1] if first[0] in ("list", "map") else ANY
        if name == "concat":
            return first if first[0] in ("str", "list") else ANY
        if name == "slice":
            return first
        if name == "sum":
            return first[1] if first[0] == "list" and first[1][0] in ("int", "float") else NUM
        return ANY


SPECIAL_CALLS = {"+", "-", "*", "/", "mod", "abs", "min", "max", "get", "concat", "slice", "sum"}


def typecheck(forms, imports=None):
    """forms: top-level AST nodes. Returns (diagnostics [(code,msg,node,hint)], exported fn types)."""
    tc = TypeChecker(imports)
    types = tc.run(forms)
    return tc.out, types
