REF = r'''lage 0.3 reference (everything an agent needs; ~1 screen)

SYNTAX   (f a b) call | [a b] list | {:k v} map | "str" | 12 3.5 | true false nil | :kw
         ; comment (top level only if you use `lage fmt`)
TRUTHY   only nil and false are falsy (0 and "" are truthy)
FORMS    (def x e)  (defn name [params] "doc"? (effects e..)? (sig [T..] R)? (requires c..)? (ensures c..)? body..)
         (fn [p..] body..)  (if c a b?)  (cond c1 e1 c2 e2 :else e)  (let [x 1 y 2] body..)
         (do ..) (and ..) (or ..)  (match r (ok v) body (err e) body)  (? r)  (test "name" expr..)
RESULTS  no exceptions. fallible builtins return (ok v) / (err e). (? r) unwraps or returns the
         err from the enclosing fn. ok err ok? err? val unwrap-or
CONTRACT (requires c) checked on entry; (ensures c) on exit with `result` bound. Failure = E_REQUIRES/E_ENSURES.
EFFECTS  fs.read fs.write net time. A fn must declare (effects ..) to use or call them AND the run must
         grant them: `lage run f.lg --allow fs.read` (prefix grants all: --allow fs, or --allow '*').
TYPES    optional, gradual, static only: (defn f [a b] (sig [int (list str)] (result int str)) ..). Types: any int
         float num str bool nil (list T) (map T) (result T E) (fn [T..] R) (or T U) (opt T); a lowercase unknown
         name is a type variable bound per call, e.g. (sig [(list t) t] t). Unannotated code is never an error
         source. Caught by `lage check`: bad arg/return types, nil leaking into int, ? in a non-result fn, match
         on non-results, wrong fn passed to map/filter/reduce. first/nth/get are typed optimistically (T, not T|nil).
MODULES  (import "lib/m.lg" :as m) then (m/name ..). Paths are relative to the importing file and must stay
         inside the entry file's directory. (import "std/list" :as l) loads the bundled stdlib instead.
         Names starting with _ are private. Module tests are not run on import.
TAILS    calls in tail position (last expr of fn/if/cond/let/do/match/and/or) run in constant depth; a fn
         with (ensures ..) is the exception. Non-tail recursion is capped at 2500 frames.
BACKEND  default `py` transpiles to Python (fast); `interp` is the reference closure interpreter (env LAGE_BACKEND
         too). Same results; differences: fuel counts user-fn calls only, effects are lexical, forms nested
         >~100 deep raise E_INTERNAL (use --backend interp).
TESTS    (test "n" (= a b)) never run by `lage run`; `lage test f.lg` runs them and reports left/right.
BUILTINS + - * / mod = != < > <= >= not | len first rest last cons append nth get assoc dissoc keys vals
         has? contains? range map filter reduce sort(xs key?) reverse concat sum min max
         str split join trim upper lower replace starts-with? ends-with? parse-int
         abs floor ceil round pow sqrt slice chars index-of
         to-json from-json type assert print read-file write-file http-get now
COMMANDS lage check FILE|- [--json]   static pass: ALL unbound names, arity, undeclared effects, syntax, imports, types
         lage run FILE|- [--json] [--allow E,..] [--fuel N] [--trace] [--no-check] [--backend py|interp]   (runs `check` first)
         lage test FILE|- [--json]   lage fmt FILE|- [--write|--check]   lage ref
OUTPUT   --json => one JSON object: {ok,value,stdout,steps,effects_used,trace?} or {ok:false,error:{code,
         message,file?,line,col,source,hint,stack}}; check failures add diagnostics:[every error]. Exit 0 ok, 1 runtime/test failure, 2 parse/usage.
ERRORS   E_PARSE E_SYNTAX E_UNBOUND E_TYPE E_ARITY E_NOT_FN E_INDEX E_DIV E_ASSERT E_REQUIRES E_ENSURES
         E_EFFECT_UNDECLARED E_EFFECT_DENIED E_FUEL E_DEPTH E_SIG E_TYPE_MISMATCH E_IMPORT E_MATCH E_TRY_TOP E_FMT_COMMENT E_RUNTIME
'''
