import unittest

from cozy.target_syntax import *
from cozy.syntax_tools import mk_lambda, pprint, fresh_var, free_vars, alpha_equivalent
from cozy.typecheck import retypecheck
from cozy.solver import satisfy, valid
from cozy.evaluation import eval, construct_value
from cozy.synthesis.acceleration import optimized_in

class SemanticsTests(unittest.TestCase):

    """
    Tests for a few equivalences we expect to be true.
    """

    def assert_same(self, e1, e2):
        assert e1.type == e2.type, "{} | {}".format(pprint(e1.type), pprint(e2.type))
        def dbg(model):
            print("model: {!r}".format(model))
            r1 = eval(e1, model)
            r2 = eval(e2, model)
            print("e1: {}".format(pprint(e1)))
            print(" ---> {!r}".format(r1))
            print("e2: {}".format(pprint(e2)))
            print(" ---> {!r}".format(r2))
        assert satisfy(ENot(EBinOp(e1, "===", e2).with_type(BOOL)), model_callback=dbg) is None

    def test_distinct_mapkeys(self):
        xs = EVar("xs").with_type(INT_BAG)
        x = EVar("x").with_type(INT)
        e1 = EUnaryOp(UOp.Distinct, xs)
        e2 = EMapKeys(EMakeMap2(xs, ELambda(x, T)))
        assert retypecheck(e1)
        assert retypecheck(e2)
        self.assert_same(e1, e2)

    def test_mapget_of_makemap1(self):
        t = THandle("T", INT)
        xs = EVar("xs").with_type(TBag(t))
        x = EVar("x").with_type(t)
        y = EVar("y").with_type(t)
        mt = TTuple((INT, INT))
        e1 = EMapGet(
            EMakeMap2(xs, ELambda(x,
                ETuple((EGetField(x, "val").with_type(INT), EGetField(y, "val").with_type(INT))).with_type(mt)
                )).with_type(TMap(t, mt)),
            y).with_type(mt)
        e2 = EUnaryOp(UOp.The,
            EMap(
                EFilter(e1.map.e,
                    mk_lambda(e1.map.value.arg.type, lambda foo: EEq(foo, e1.key))).with_type(e1.map.e.type),
                e1.map.value).with_type(e1.map.e.type)).with_type(e1.map.e.type.t)
        assert retypecheck(e1)
        assert retypecheck(e2)
        self.assert_same(e1, e2)

    def test_mapget_of_makemap2(self):
        t = THandle("T", INT)
        xs = EVar("xs").with_type(TBag(t))
        x = EVar("x").with_type(t)
        y = EVar("y").with_type(t)
        mt = TTuple((INT, INT))
        e1 = EMapGet(
            EMakeMap2(xs, ELambda(x,
                ETuple((EGetField(x, "val").with_type(INT), EGetField(y, "val").with_type(INT))).with_type(mt)
                )).with_type(TMap(t, mt)),
            y).with_type(mt)
        e2 = ECond(
            EIn(e1.key, e1.map.e),
            e1.map.value.apply_to(EUnaryOp(UOp.The, EFilter(e1.map.e, mk_lambda(e1.map.value.arg.type, lambda foo: EEq(foo, e1.key))).with_type(e1.map.e.type)).with_type(e1.map.e.type.t)),
            construct_value(e1.type)).with_type(e1.type)
        self.assert_same(e1, e2)

    def test_map_eq(self):
        k = TNative("V")
        v = TBag(THandle("H", k))
        t = TMap(k, v)
        m1 = EVar("m1").with_type(t)
        m2 = EVar("m1").with_type(t)

        e = EImplies(EEq(m1, m2), EEq(EMapKeys(m1), EMapKeys(m2)))
        assert retypecheck(e)
        assert valid(e, collection_depth=3)

        k = EVar("k").with_type(t.k)
        e = EImplies(EEq(m1, m2), EEq(EMapGet(m1, k), EMapGet(m2, k)))
        assert retypecheck(e)
        assert valid(e, collection_depth=3)

    def test_argmin(self):
        xs = EVar("xs").with_type(INT_BAG)
        ys = EVar("ys").with_type(INT_BAG)
        id = mk_lambda(INT, lambda x: x)
        e1 = EArgMin(EBinOp(xs, "+", ys), id)
        e2 = ECond(EUnaryOp(UOp.Empty, xs), EArgMin(ys, id),
             ECond(EUnaryOp(UOp.Empty, ys), EArgMin(xs, id),
                EArgMin(EBinOp(
                    ESingleton(EArgMin(xs, id)),
                    "+",
                    ESingleton(EArgMin(ys, id))), id)))
        assert retypecheck(e1)
        assert retypecheck(e2)
        self.assert_same(e1, e2)

    def test_haskey(self):
        m = EVar("m").with_type(TMap(INT, INT))
        k = EVar("k").with_type(INT)
        e1 = EHasKey(m, k).with_type(BOOL)
        e2 = EIn(k, EMapKeys(m).with_type(TSet(INT)))
        self.assert_same(e1, e2)

    def test_edeepin(self):
        ht = THandle("H", INT)
        hb = EVar("hb").with_type(TBag(ht))
        h = fresh_var(ht, omit=free_vars(hb))
        arg = fresh_var(ht, omit=free_vars(h)|free_vars(hb))
        f1 = EDeepIn(h, hb)
        f2 = EUnaryOp(UOp.Any, EMap(hb, ELambda(arg, EBinOp(arg, "===", h).with_type(BOOL))).with_type(BOOL_BAG)).with_type(BOOL)
        self.assert_same(f1, f2)

    def test_subsub(self):
        xs = EVar("xs").with_type(INT_BAG)
        i = EVar("i").with_type(INT)
        e1 = EBinOp(
            EUnaryOp(UOp.Distinct, xs), "-",
            EBinOp(
                xs, "-",
                ESingleton(i)))
        assert retypecheck(e1)
        m = EMakeMap2(e1.e1,
            mk_lambda(INT, lambda x:
                EUnaryOp(UOp.Length, EFilter(xs,
                    mk_lambda(INT, lambda y:
                        EEq(x, y)))).with_type(INT))).with_type(TMap(INT, INT))
        count = EMapGet(m, i).with_type(INT)
        e2 = ECond(
            EEq(count, ONE),
            ESingleton(i).with_type(INT_BAG),
            EEmptyList().with_type(INT_BAG)).with_type(INT_BAG)
        assert retypecheck(e2)
        self.assert_same(e1, e2)

    def test_optimized_in1(self):
        xs = EVar("xs").with_type(INT_BAG)
        i = EVar("i").with_type(INT)
        j = EVar("j").with_type(INT)
        e1 = EIn(i, EBinOp(EStateVar(xs), "-", ESingleton(j)))
        assert retypecheck(e1)
        e2 = optimized_in(i, e1.e2)
        assert not alpha_equivalent(e1, e2)
        self.assert_same(e1, e2)

    def test_optimized_in2(self):
        xs = EVar("xs").with_type(INT_BAG)
        ys = EVar("ys").with_type(INT_BAG)
        i = EVar("i").with_type(INT)
        e1 = EIn(i, EBinOp(xs, "-", ys))
        assert retypecheck(e1)
        e2 = optimized_in(i, e1.e2)
        assert not alpha_equivalent(e1, e2)
        self.assert_same(e1, e2)

    def test_distribute_filter_over_subtract(self):
        xs = EVar("xs").with_type(INT_BAG)
        ys = EVar("ys").with_type(INT_BAG)
        x = EVar("x").with_type(INT)
        e1 = EFilter(EBinOp(xs, "-", ys), ELambda(x, ECall("f", (x,)).with_type(BOOL)))
        assert retypecheck(e1)
        e2 = EBinOp(EFilter(xs, e1.p), "-", EFilter(ys, e1.p))
        assert retypecheck(e2)
        self.assert_same(e1, e2)

    def test_distribute_the_over_map(self):
        xs = EVar("xs").with_type(INT_BAG)
        x = EVar("x").with_type(INT)
        e1 = EUnaryOp(UOp.The, EMap(xs, ELambda(x, ECall("f", (x,)).with_type(INT))))
        assert retypecheck(e1)
        e2 = ECond(
            EUnaryOp(UOp.Exists, xs),
            e1.e.f.apply_to(EUnaryOp(UOp.The, xs)),
            EUnaryOp(UOp.The, EEmptyList().with_type(xs.type)))
        assert retypecheck(e2)
        self.assert_same(e1, e2)
