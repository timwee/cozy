from collections import OrderedDict
from enum import Enum

from cozy.common import OrderedSet, partition
from cozy.target_syntax import *
from cozy.syntax_tools import pprint, fresh_var, free_vars, free_funcs, break_sum, all_exps, alpha_equivalent
from cozy.contexts import Context
from cozy.typecheck import is_collection, is_numeric
from cozy.pools import Pool, RUNTIME_POOL, STATE_POOL
from cozy.solver import satisfy, ModelCachingSolver
from cozy.evaluation import eval, eval_bulk
from cozy.structures import extension_handler
from cozy.logging import task, event

class Order(Enum):
    EQUAL     = 0
    LT        = 1
    GT        = 2
    AMBIGUOUS = 3

def composite_order(*funcs):
    for f in funcs:
        o = f()
        if o != Order.EQUAL:
            return o
    return Order.EQUAL

def order_objects(x, y):
    if x < y: return Order.LT
    if y < x: return Order.GT
    return Order.EQUAL

class CostModel(object):

    def __init__(self, assumptions : Exp = T, examples=(), funcs=()):
        self.solver = ModelCachingSolver(vars=(), funcs=funcs, examples=examples, assumptions=assumptions)
        self.assumptions = assumptions
        # self.examples = list(examples)
        self.funcs = OrderedDict(funcs)

    @property
    def examples(self):
        return tuple(self.solver.examples)

    def _compare(self, e1 : Exp, e2 : Exp, context : Context):
        e1_constant = not free_vars(e1) and not free_funcs(e1)
        e2_constant = not free_vars(e2) and not free_funcs(e2)
        if e1_constant and e2_constant:
            e1v = eval(e1, {})
            e2v = eval(e2, {})
            event("comparison obvious on constants: {} vs {}".format(e1v, e2v))
            return order_objects(e1v, e2v)
        if alpha_equivalent(e1, e2):
            event("shortcutting comparison of identical terms")
            return Order.EQUAL

        a = EAll(context.path_conditions())
        always_le = self.solver.valid(EImplies(a, ELe(e1, e2)))
        always_ge = self.solver.valid(EImplies(a, EGe(e1, e2)))

        if always_le and always_ge:
            return Order.EQUAL
        if always_le:
            return Order.LT
        if always_ge:
            return Order.GT
        return Order.AMBIGUOUS

    def compare(self, e1 : Exp, e2 : Exp, context : Context, pool : Pool):
        with task("compare costs", context=context):
            if pool == RUNTIME_POOL:
                return composite_order(
                    lambda: self._compare(asymptotic_runtime(e1), asymptotic_runtime(e2), context),
                    lambda: self._compare(max_storage_size(e1), max_storage_size(e2), context),
                    lambda: self._compare(rt(e1), rt(e2), context),
                    lambda: order_objects(e1.size(), e2.size()))
            else:
                return composite_order(
                    lambda: self._compare(storage_size(e1), storage_size(e2), context),
                    lambda: order_objects(e1.size(), e2.size()))

def card(e):
    assert is_collection(e.type)
    return ELen(e)

TWO   = ENum(2).with_type(INT)
FOUR  = ENum(4).with_type(INT)
EIGHT = ENum(8).with_type(INT)
TWENTY = ENum(20).with_type(INT)
def storage_size(e):
    h = extension_handler(type(e.type))
    if h is not None:
        return h.storage_size(e, k=storage_size)

    if e.type == BOOL:
        return ONE
    elif is_numeric(e.type) or isinstance(e.type, THandle):
        return FOUR
    elif isinstance(e.type, TEnum):
        return TWO
    elif isinstance(e.type, TNative):
        return FOUR
    elif isinstance(e.type, TString):
        return TWENTY
    elif isinstance(e.type, TTuple):
        return ESum([storage_size(ETupleGet(e, n).with_type(t)) for (n, t) in enumerate(e.type.ts)])
    elif isinstance(e.type, TRecord):
        return ESum([storage_size(EGetField(e, f).with_type(t)) for (f, t) in e.type.fields])
    elif is_collection(e.type):
        v = fresh_var(e.type.t, omit=free_vars(e))
        return ESum([
            FOUR,
            EUnaryOp(UOp.Sum, EMap(e, ELambda(v, storage_size(v))).with_type(INT_BAG)).with_type(INT)])
    elif isinstance(e.type, TMap):
        k = fresh_var(e.type.k, omit=free_vars(e))
        return ESum([
            FOUR,
            EUnaryOp(UOp.Sum, EMap(
                EMapKeys(e).with_type(TBag(e.type.k)),
                ELambda(k, ESum([
                    storage_size(k),
                    storage_size(EMapGet(e, k).with_type(e.type.v))]))).with_type(INT_BAG)).with_type(INT)])
    else:
        raise NotImplementedError(e.type)

def max_of(*es):
    x = EVar("x").with_type(INT)
    parts = ESum([ESingleton(e).with_type(INT_BAG) for e in es], base_case=EEmptyList().with_type(INT_BAG))
    if isinstance(parts, EEmptyList):
        return ZERO
    if isinstance(parts, ESingleton):
        return parts.e
    return EArgMax(parts, ELambda(x, x)).with_type(INT)

def max_storage_size(e):
    sizes = OrderedSet()
    for x in all_exps(e):
        if isinstance(x, EStateVar):
            sizes.add(storage_size(x.e))
    return max_of(*sizes)

def hash_cost(e):
    return storage_size(e)

def comparison_cost(e1, e2):
    return ESum([storage_size(e1), storage_size(e2)])

# def iterates_over_bag(e):
#     if isinstance(e, EFilter) or isinstance(e, EMap) or isinstance(e, EFlatMap) or isinstance(e, EArgMin) or isinstance(e, EArgMax) or isinstance(e, EMakeMap2):
#         return e.e
#     if isinstance(e, EUnaryOp) and is_collection(e.e.type):
#         return e.e
#     if isinstance(e, EBinOp) and e.op == BOp.In:
#         return e.e2
#     return None

def wc_card(e):
    assert is_collection(e.type)
    while isinstance(e, EFilter) or isinstance(e, EMap) or isinstance(e, EFlatMap) or isinstance(e, EArgMin) or isinstance(e, EArgMax) or isinstance(e, EMakeMap2) or isinstance(e, EStateVar) or (isinstance(e, EUnaryOp) and e.op == UOp.Distinct):
        e = e.e
    if isinstance(e, EBinOp) and e.op == "-":
        return wc_card(e.e1)
    if isinstance(e, EBinOp) and e.op == "+":
        return EBinOp(wc_card(e.e1), "+", wc_card(e.e2)).with_type(INT)
    if isinstance(e, ECond):
        return max_of(wc_card(e.then_branch), wc_card(e.else_branch))
    if isinstance(e, EVar):
        return ENum(EXTREME_COST).with_type(INT)
    return card(e)

# These require walking over the entire collection.
# Some others (e.g. "exists" or "empty") just look at the first item.
LINEAR_TIME_UOPS = {
    UOp.Sum, UOp.Length,
    UOp.Distinct, UOp.AreUnique,
    UOp.All, UOp.Any,
    UOp.Reversed }

def asymptotic_runtime(e):
    terms = [ONE]
    stk = [e]
    while stk:
        e = stk.pop()
        if isinstance(e, tuple) or isinstance(e, list):
            stk.extend(e)
            continue
        if not isinstance(e, Exp):
            continue
        if isinstance(e, ELambda):
            e = e.body
        if isinstance(e, EFilter):
            terms.append(EBinOp(wc_card(e.e), "*", asymptotic_runtime(e.p)).with_type(INT))
        if isinstance(e, EMap) or isinstance(e, EFlatMap) or isinstance(e, EArgMin) or isinstance(e, EArgMax):
            terms.append(EBinOp(wc_card(e.e), "*", asymptotic_runtime(e.f)).with_type(INT))
        if isinstance(e, EMakeMap2):
            terms.append(EBinOp(wc_card(e.e), "*", asymptotic_runtime(e.value)).with_type(INT))
        if isinstance(e, EBinOp) and e.op == BOp.In:
            terms.append(wc_card(e.e2))
        if isinstance(e, EBinOp) and e.op == "-" and is_collection(e.type):
            terms.append(ENum(EXTREME_COST).with_type(INT))
            terms.append(EBinOp(wc_card(e.e1), "*", wc_card(e.e2)).with_type(INT))
        if isinstance(e, EUnaryOp) and e.op in LINEAR_TIME_UOPS:
            terms.append(wc_card(e.e))
        if isinstance(e, EStateVar):
            continue
        stk.extend(e.children())
    return max_of(*terms)

# Some kinds of expressions have a massive penalty associated with them if they
# appear at runtime.
EXTREME_COST = 1000
def rt(e, account_for_constant_factors=True):
    constant = 0
    terms = []
    stk = [e]
    while stk:
        e = stk.pop()
        if isinstance(e, tuple) or isinstance(e, list):
            stk.extend(e)
            continue
        if isinstance(e, str) or isinstance(e, int):
            continue
        if isinstance(e, ELambda):
            continue
        if isinstance(e, EBinOp) and e.op == BOp.In:
            v = fresh_var(e.e1.type, omit=free_vars(e.e1))
            stk.append(e.e1)
            stk.append(EUnaryOp(UOp.Any, EMap(e.e2, ELambda(v, EEq(v, e.e1))).with_type(BOOL_BAG)).with_type(BOOL))
            continue
        if isinstance(e, EBinOp) and e.op == BOp.And:
            stk.append(e.e1)
            terms.append(ECond(e.e1, rt(e.e2), ZERO).with_type(INT))
            continue
        if isinstance(e, EBinOp) and e.op == BOp.Or:
            stk.append(e.e1)
            terms.append(ECond(e.e1, ZERO, rt(e.e2)).with_type(INT))
            continue
        if isinstance(e, ECond):
            stk.append(e.cond)
            terms.append(ECond(e.cond,
                rt(e.then_branch),
                rt(e.else_branch)).with_type(INT))
            continue

        constant += 1
        if isinstance(e, EStateVar):
            continue
        if isinstance(e, Exp):
            stk.extend(e.children())

        if isinstance(e, EFilter):
            # constant += EXTREME_COST
            terms.append(EUnaryOp(UOp.Sum, EMap(e.e, ELambda(e.p.arg, rt(e.p.body))).with_type(INT_BAG)).with_type(INT))
        elif isinstance(e, EMap) or isinstance(e, EFlatMap) or isinstance(e, EArgMin) or isinstance(e, EArgMax):
            # constant += EXTREME_COST
            terms.append(EUnaryOp(UOp.Sum, EMap(e.e, ELambda(e.f.arg, rt(e.f.body))).with_type(INT_BAG)).with_type(INT))
        elif isinstance(e, EMakeMap2):
            constant += EXTREME_COST
            terms.append(EUnaryOp(UOp.Sum, EMap(e.e, ELambda(e.value.arg, rt(e.value.body))).with_type(INT_BAG)).with_type(INT))
        elif isinstance(e, EBinOp) and e.op == "-" and is_collection(e.type):
            constant += EXTREME_COST
            terms.append(card(e.e1))
            terms.append(card(e.e2))
        elif isinstance(e, EBinOp) and e.op in ("==", "!=", ">", "<", ">=", "<="):
            terms.append(comparison_cost(e.e1, e.e2))
        elif isinstance(e, EUnaryOp) and e.op in LINEAR_TIME_UOPS:
            terms.append(card(e.e))
        elif isinstance(e, EMapGet):
            terms.append(hash_cost(e.key))
            terms.append(comparison_cost(e.key, e.key))

    terms.append(ENum(constant).with_type(INT))
    if not account_for_constant_factors:
        terms = [t for t in terms if not isinstance(t, ENum)]
    return ESum(terms)

def ESum(es, base_case=ZERO):
    es = [e for x in es for e in break_sum(x) if e != base_case]
    if not es:
        return base_case
    nums, nonnums = partition(es, lambda e: isinstance(e, ENum))
    es = nonnums
    if nums:
        es.append(ENum(sum(n.val for n in nums)).with_type(base_case.type))
    return build_balanced_tree(base_case.type, "+", es)

def debug_comparison(cm : CostModel, e1 : Exp, e2 : Exp, context : Context):
    print("-" * 20)
    print("Comparing")
    print("  e1 = {}".format(pprint(e1)))
    print("  e2 = {}".format(pprint(e2)))
    print("-" * 20 + " {} examples...".format(len(cm.examples)))
    for f in asymptotic_runtime, max_storage_size, rt:
        for ename, e in [("e1", e1), ("e2", e2)]:
            print("{f}({e}) = {res}".format(f=f.__name__, e=ename, res=pprint(f(e))))
    for x in cm.examples:
        print("-" * 20)
        print(x)
        print("asympto(e1) = {}".format(eval_bulk(asymptotic_runtime(e1), [x], use_default_values_for_undefined_vars=True)[0]))
        print("asympto(e2) = {}".format(eval_bulk(asymptotic_runtime(e2), [x], use_default_values_for_undefined_vars=True)[0]))
        print("storage(e1) = {}".format(eval_bulk(max_storage_size(e1), [x], use_default_values_for_undefined_vars=True)[0]))
        print("storage(e2) = {}".format(eval_bulk(max_storage_size(e2), [x], use_default_values_for_undefined_vars=True)[0]))
        print("runtime(e1) = {}".format(eval_bulk(rt(e1), [x], use_default_values_for_undefined_vars=True)[0]))
        print("runtime(e2) = {}".format(eval_bulk(rt(e2), [x], use_default_values_for_undefined_vars=True)[0]))
    print("-" * 20)
