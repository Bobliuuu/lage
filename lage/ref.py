REF = r'''lage 0.1 reference (everything an agent needs; ~1 screen)

SYNTAX   (f a b) call | [a b] list | {:k v} map | "str" | 12 3.5 | true false nil | :kw
         ; comment (top level only if you use `lage fmt`)
TRUTHY   only nil and false are falsy (0 and "" are truthy)
FORMS    (def x e)  (defn name [params] "doc"? (effects e..)? (requires c..)? (ensures c..)? body..)
         (fn [p..] body..)  (if c a b?)  (cond c1 e1 c2 e2 :else e)  (let [x 1 y 2] body..)
         (do ..) (and ..) (or ..)  (match r (ok v) body (err e) body)  (? r)  (test "name" expr..)
RESULTS  no exceptions. fallible builtins return (ok v) / (err e). (? r) unwraps or returns the
         err from the enclosing fn. ok err ok? err? val unwrap-or
CONTRACT (requires c) checked on entry; (ensures c) on exit with `result` bound. Failure = E_REQUIRES/E_ENSURES.
EFFECTS  fs.read fs.write net time. A fn must declare (effects ..) to use or call them AND the run must
         grant them: `lage run f.lg --allow fs.read` (prefix grants all: --allow fs, or --allow '*').
TESTS    (test "n" (= a b)) never run by `lage run`; `lage test f.lg` runs them and reports left/right.
BUILTINS + - * / mod = != < > <= >= not | len first rest last cons append nth get assoc dissoc keys vals
         has? contains? range map filter reduce sort(xs key?) reverse concat sum min max
         str split join trim upper lower replace starts-with? ends-with? parse-int
         to-json from-json type assert print read-file write-file http-get now
COMMANDS lage run FILE|- [--json] [--allow E,..] [--fuel N] [--trace]   lage test FILE|- [--json]
         lage fmt FILE|- [--write|--check]   lage ref
OUTPUT   --json => one JSON object: {ok,value,stdout,steps,effects_used,trace?} or {ok:false,error:{code,
         message,line,col,source,hint,stack}}. Exit 0 ok, 1 runtime/test failure, 2 parse/usage.
ERRORS   E_PARSE E_SYNTAX E_UNBOUND E_TYPE E_ARITY E_NOT_FN E_INDEX E_DIV E_ASSERT E_REQUIRES E_ENSURES
         E_EFFECT_UNDECLARED E_EFFECT_DENIED E_FUEL E_DEPTH E_MATCH E_TRY_TOP E_FMT_COMMENT E_RUNTIME
'''
