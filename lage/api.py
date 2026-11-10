"""`lage api`: list the public surface of a module (name, params, sig, effects, doc)."""
import os

from .core import STD_DIR, Form, Sym, Vec, flat, parse_program

CLAUSES = ("effects", "requires", "ensures", "sig")


def resolve(path):
    if path.startswith("std/"):
        name = path[4:]
        return os.path.join(STD_DIR, name if name.endswith(".lg") else name + ".lg")
    return path


def api_of(src):
    out = []
    for node, _, _ in parse_program(src)[0]:
        if not (isinstance(node, Form) and len(node) >= 4 and isinstance(node[0], Sym)
                and node[0].name == "defn" and isinstance(node[1], Sym) and isinstance(node[2], Vec)):
            continue
        name = node[1].name
        if name.startswith("_"):
            continue
        rest = list(node[3:])
        doc = rest.pop(0) if len(rest) > 1 and isinstance(rest[0], str) else None
        info = {"name": name, "params": [flat(p) for p in node[2]], "line": node.line}
        if doc:
            info["doc"] = doc
        effects, nreq, nens = [], 0, 0
        while rest and isinstance(rest[0], Form) and rest[0] and isinstance(rest[0][0], Sym) \
                and rest[0][0].name in CLAUSES:
            c = rest.pop(0)
            k = c[0].name
            if k == "sig" and len(c) == 3:
                info["sig"] = "%s -> %s" % (flat(c[1]), flat(c[2]))
            elif k == "effects":
                effects += [flat(s) for s in c[1:]]
            elif k == "requires":
                nreq += len(c) - 1
            elif k == "ensures":
                nens += len(c) - 1
        if effects:
            info["effects"] = effects
        if nreq:
            info["requires"] = nreq
        if nens:
            info["ensures"] = nens
        out.append(info)
    return out


def render(fns):
    lines = []
    for f in fns:
        head = "%s [%s]" % (f["name"], " ".join(f["params"]))
        if "sig" in f:
            head += " :: " + f["sig"]
        if "effects" in f:
            head += "  !" + ",".join(f["effects"])
        if "doc" in f:
            head += "  -- " + f["doc"]
        lines.append(head)
    return "\n".join(lines)
