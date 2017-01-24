import itertools
import sys

from cozy.target_syntax import *
from cozy.typecheck import INT, BOOL
from cozy.syntax_tools import subst, replace, pprint, free_vars, BottomUpExplorer, BottomUpRewriter, equal, fresh_var, alpha_equivalent, all_exps, implies, mk_lambda
from cozy.common import Visitor, fresh_name, typechecked, unique, pick_to_sum, cross_product, OrderedDefaultDict, nested_dict
from cozy.solver import satisfy, satisfiable, valid
from cozy.evaluation import eval, mkval
from cozy.cost_model import CostModel

class Cache(object):
    def __init__(self, items=None):
        self.data = nested_dict(3, list) # data[type_tag][type][size] is list of exprs
        self.size = 0
        if items:
            for (e, size) in items:
                self.add(e, size)
    def tag(self, t):
        return type(t)
    def is_tag(self, t):
        return isinstance(t, type)
    def add(self, e, size):
        self.data[self.tag(e.type)][e.type][size].append(e)
        self.size += 1
    def evict(self, e, size):
        try:
            self.data[self.tag(e.type)][e.type][size].remove(e)
            self.size -= 1
        except ValueError:
            # this happens if e is not in the list, which is fine
            pass
    def find(self, type=None, size=None):
        type_tag = None
        if type is not None:
            if self.is_tag(type):
                type_tag = type
                type = None
            else:
                type_tag = self.tag(type)
        res = []
        for x in (self.data.values() if type_tag is None else [self.data.get(type_tag, {})]):
            for y in (x.values() if type is None else [x.get(type, {})]):
                for z in (y.values() if size is None else [y.get(size, [])]):
                    res += z
        return res
    def types(self):
        for d in self.data.values():
            yield from d.keys()
    def __iter__(self):
        for x in self.data.values():
            for y in x.values():
                for (size, es) in y.items():
                    for e in es:
                        yield (e, size)
    def __len__(self):
        return self.size
    def random_sample(self, n):
        import random
        es = [ e for (e, size) in self ]
        return random.sample(es, min(n, len(es)))

class ExpBuilder(object):
    def build(self, cache, size):
        raise NotImplementedError()
    def with_roots(self, new_roots):
        raise NotImplementedError()

def values_of_type(value, value_type, desired_type):
    # see evaluation.mkval for info on the structure of values
    if value_type == desired_type:
        yield value
    elif isinstance(value_type, TSet) or isinstance(value_type, TBag):
        for x in value:
            yield from values_of_type(x, value_type.t, desired_type)
    else:
        # I think this is OK since all values for bound vars are pulled from
        # bags or other collections.
        pass

def _instantiate_examples(examples, vars, binder):
    for e in examples:
        found = 0
        if binder.id in e:
            yield e
            found += 1
        for v in vars:
            for possible_value in unique(values_of_type(e[v.id], v.type, binder.type)):
                # print("possible value for {}: {}".format(pprint(binder.type), repr(possible_value)))
                e2 = dict(e)
                e2[binder.id] = possible_value
                yield e2
                found += 1
            # print("got {} ways to instantiate {}".format(found, binder.id))
        if not found:
            e2 = dict(e)
            e2[binder.id] = mkval(binder.type)
            yield e2

def instantiate_examples(examples, vars : {EVar}, binders : [EVar]):
    for v in binders:
        examples = list(_instantiate_examples(examples, vars, v))
    return examples

def fingerprint(e, examples):
    return (e.type,) + tuple(eval(e, ex) for ex in examples)

def make_constant_of_type(t):
    class V(Visitor):
        def visit_TInt(self, t):
            return ENum(0).with_type(t)
        def visit_TBool(self, t):
            return EBool(False).with_type(t)
        def visit_TBag(self, t):
            return EEmptyList().with_type(t)
        def visit_Type(self, t):
            raise NotImplementedError(t)
    return V().visit(t)

class StopException(Exception):
    pass

class Learner(object):
    def __init__(self, target, examples, cost_model, builder, stop_callback):
        self.stop_callback = stop_callback
        self.cost_model = cost_model
        self.builder = builder
        self.seen = { } # fingerprint:(cost, e, size) map
        self.reset(examples, update_watched_exps=False)
        self.watch(target)

    def reset(self, examples, update_watched_exps=True):
        self.cache = Cache()
        self.current_size = 0
        self.examples = examples
        self.seen.clear()
        self.builder_iter = ()
        self.last_progress = 0
        if update_watched_exps:
            self.update_watched_exps()

    def watch(self, new_target):
        new_roots = []
        for e in all_exps(new_target):
            if e in new_roots:
                continue
            if not isinstance(e, ELambda):
                try:
                    self._fingerprint(e)
                    new_roots.append(e)
                except Exception:
                    pass

        self.builder = self.builder.with_roots(new_roots)

        self.target = new_target
        self.update_watched_exps()
        if self.cost_model.is_monotonic():
            seen = list(self.seen.items())
            n = 0
            for (fp, (cost, e, size)) in seen:
                if cost > self.cost_ceiling:
                    self.cache.evict(e, size)
                    del self.seen[fp]
                    n += 1
            if n:
                print("evicted {} elements".format(n))

    def update_watched_exps(self):
        e = self.target
        self.cost_ceiling = self.cost_model.cost(e)
        # print(" --< cost ceiling is now {}".format(self.cost_ceiling))
        self.watched_exps = {}
        for e in all_exps(self.target):
            if isinstance(e, ELambda):
                continue
            try:
                fp = self._fingerprint(e)
                cost = self.cost_model.cost(e)
                prev = self.watched_exps.get(fp)
                if prev is None or prev[1] < cost:
                    self.watched_exps[fp] = (e, cost)
            except Exception:
                print("WARNING: unable to watch expression {}".format(pprint(e)))
                continue
        # for (fp, (e, cost)) in self.watched_exps.items():
        #     print("WATCHING {} (fp={}, cost={})".format(pprint(e), hash(fp), cost))

    def _fingerprint(self, e):
        return fingerprint(e, self.examples)

    def _on_exp(self, e, fate, *args):
        return
        # if (isinstance(e, EMapGet) or
        #         isinstance(e, EFilter) or
        #         (isinstance(e, EBinOp) and e.op == "==" and (isinstance(e.e1, EVar) or isinstance(e.e2, EVar))) or
        #         (isinstance(e, EBinOp) and e.op == ">=" and (isinstance(e.e1, EVar) or isinstance(e.e2, EVar)))):
        # if isinstance(e, EBinOp) and e.op == "+" and isinstance(e.type, TBag):
        # if hasattr(e, "_tag") and e._tag:
        # if isinstance(e, EFilter):
        if fate in ("better", "new"):
            print(" ---> [{}] {}; {}".format(fate, pprint(e), ", ".join(pprint(e) for e in args)))

    def forget_most_recent(self):
        (e, size, fp) = self.most_recent
        self.cache.evict(e, size)
        if self.overwritten is None:
            del self.seen[fp]
        else:
            self.seen[fp] = self.overwritten
        self.most_recent = self.overwritten = None

    def next(self):
        while True:
            for e in self.builder_iter:
                if self.stop_callback():
                    raise StopException()

                cost = self.cost_model.cost(e)

                if self.cost_model.is_monotonic() and cost > self.cost_ceiling:
                    self._on_exp(e, "too expensive", cost, self.cost_ceiling)
                    continue

                fp = self._fingerprint(e)
                prev = self.seen.get(fp)

                if prev is None:
                    self.overwritten = None
                    self.most_recent = (e, self.current_size, fp)
                    self.seen[fp] = (cost, e, self.current_size)
                    self.cache.add(e, size=self.current_size)
                    self.last_progress = self.current_size
                    self._on_exp(e, "new")
                else:
                    prev_cost, prev_exp, prev_size = prev
                    if cost < prev_cost:
                        self.overwritten = prev
                        self.most_recent = (e, self.current_size, fp)
                        # print("cost ceiling lowered for {}: {} --> {}".format(fp, prev_cost, cost))
                        self.cache.evict(prev_exp, prev_size)
                        self.cache.add(e, size=self.current_size)
                        self.seen[fp] = (cost, e, self.current_size)
                        self.last_progress = self.current_size
                        self._on_exp(e, "better", prev_exp)
                    else:
                        self._on_exp(e, "worse", prev_exp)
                        continue

                watched = self.watched_exps.get(fp)
                if watched is not None:
                    watched_e, watched_cost = watched
                    if cost < watched_cost or (cost == watched_cost and e != watched_e):
                        print("Found potential improvement [{}] for [{}]".format(pprint(e), pprint(watched_e)))
                        return (watched_e, e)

            if self.last_progress < (self.current_size+1) // 2:
                raise StopException("hit termination condition")

            self.current_size += 1
            self.builder_iter = self.builder.build(self.cache, self.current_size)
            print("minor iteration {}, |cache|={}".format(self.current_size, len(self.cache)))

@typechecked
def fixup_binders(e : Exp, binders_to_use : [EVar]) -> Exp:
    class V(BottomUpRewriter):
        def visit_ELambda(self, e):
            body = self.visit(e.body)
            if e.arg in binders_to_use:
                return ELambda(e.arg, body)
            if not any(b.type == e.arg.type for b in binders_to_use):
                # print("WARNING: I am assuming that subexpressions of [{}] never appear in isolation".format(pprint(e)))
                return ELambda(e.arg, body)
            fvs = free_vars(body)
            legal_repls = [ b for b in binders_to_use if b not in fvs and b.type == e.arg.type ]
            if not legal_repls:
                raise Exception("No legal binder to use for {}".format(e))
            b = legal_repls[0]
            return ELambda(b, subst(body, { e.arg.id : b }))
    return V().visit(e)

class FixedBuilder(ExpBuilder):
    def __init__(self, wrapped_builder, binders_to_use, assumptions : Exp):
        self.wrapped_builder = wrapped_builder
        self.binders_to_use = binders_to_use
        self.assumptions = assumptions
    def build(self, cache, size):
        for e in self.wrapped_builder.build(cache, size):
            try:
                e = fixup_binders(e, self.binders_to_use)
            except Exception:
                continue
                print("WARNING: skipping built expression {}".format(pprint(e)), file=sys.stderr)

            # experimental criterion: bags of handles must have distinct values
            if isinstance(e.type, TBag) and isinstance(e.type.t, THandle):
                if not valid(implies(self.assumptions, EUnaryOp("unique", e).with_type(BOOL))):
                    # print("rejecting non-unique {}".format(pprint(e)))
                    continue

            # all sets must have distinct values
            if isinstance(e.type, TSet):
                if not valid(implies(self.assumptions, EUnaryOp("unique", e).with_type(BOOL))):
                    raise Exception("insanity: values of {} are not distinct".format(e))

            # experimental criterion: "the" must be a 0- or 1-sized collection
            if isinstance(e, EUnaryOp) and e.op == "the":
                len = EUnaryOp("sum", EMap(e.e, mk_lambda(e.type, lambda x: ENum(1).with_type(INT))).with_type(TBag(INT))).with_type(INT)
                if not valid(implies(self.assumptions, EBinOp(len, "<=", ENum(1).with_type(INT)))):
                    # print("rejecting illegal application of 'the': {}".format(pprint(e)))
                    continue
                if not satisfiable(EAll([self.assumptions, equal(len, ENum(0).with_type(INT))])):
                    # print("rejecting illegal application of 'the': {}".format(pprint(e)))
                    continue
                if not satisfiable(EAll([self.assumptions, equal(len, ENum(1).with_type(INT))])):
                    # print("rejecting illegal application of 'the': {}".format(pprint(e)))
                    continue

            # filters must *do* something
            # This prevents degenerate cases where the synthesizer uses filter
            # expressions to artificially lower the estimated cardinality of a
            # collection.
            if isinstance(e, EFilter):
                if not satisfiable(EAll([self.assumptions, ENot(equal(e, e.e))])):
                    continue
                    print("rejecting stupid filter {}".format(pprint(e)), file=sys.stderr)

            yield e
    def with_roots(self, roots):
        return FixedBuilder(self.wrapped_builder.with_roots(roots), self.binders_to_use, self.assumptions)

def truncate(s):
    if len(s) > 60:
        return s[:60] + "..."
    return s

@typechecked
def improve(
        target : Exp,
        assumptions : Exp,
        binders : [EVar],
        cost_model : CostModel,
        builder : ExpBuilder,
        stop_callback):

    target = fixup_binders(target, binders)
    builder = FixedBuilder(builder, binders, assumptions)

    vars = list(free_vars(target) | free_vars(assumptions))
    examples = []
    learner = Learner(target, instantiate_examples(examples, set(vars), binders), cost_model, builder, stop_callback)
    try:
        while True:
            # 1. find any potential improvement to any sub-exp of target
            old_e, new_e = learner.next()

            # 2. substitute-in the improvement
            new_target = replace(target, old_e, new_e)

            if (free_vars(new_target) - set(vars)):
                print("oops, candidate {} has weird free vars".format(pprint(new_target)))
                learner.forget_most_recent()
                continue

            # 3. check
            formula = EAll([assumptions, ENot(equal(target, new_target))])
            counterexample = satisfy(formula, vars=vars)
            if counterexample is not None:
                # a. if incorrect: add example, reset the learner
                examples.append(counterexample)
                print("new example: {}".format(truncate(repr(counterexample))))
                print("restarting with {} examples".format(len(examples)))
                instantiated_examples = instantiate_examples(examples, set(vars), binders)
                print("    ({} examples post-instantiation)".format(len(instantiated_examples)))
                learner.reset(instantiated_examples)
            else:
                # b. if correct: yield it, watch the new target, goto 2
                old_cost = cost_model.cost(target)
                new_cost = cost_model.cost(new_target)
                if new_cost > old_cost:
                    print("whoops: {} ----> {}".format(target, new_target))
                    from cozy.rep_inference import infer_rep, pprint_reps
                    for x in [old_e, new_e, target, new_target]:
                        pprint_reps(infer_rep(cost_model.state_vars, x))
                    # import pdb
                    # pdb.set_trace()
                    assert False
                    # learner.forget_most_recent()
                    # continue
                if new_cost == old_cost:
                    continue
                print("found improvement: {} -----> {}".format(pprint(old_e), pprint(new_e)))
                print("cost: {} -----> {}".format(old_cost, new_cost))
                learner.reset(instantiate_examples(examples, set(vars), binders), update_watched_exps=False)
                learner.watch(new_target)
                target = new_target
                yield new_target
    except KeyboardInterrupt:
        for e in learner.cache.random_sample(50):
            print(pprint(e))
        raise
