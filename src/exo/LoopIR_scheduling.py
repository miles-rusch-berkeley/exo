import re
from collections import ChainMap

from .LoopIR import (
    LoopIR,
    LoopIR_Rewrite,
    Alpha_Rename,
    LoopIR_Do,
    SubstArgs,
    T,
    get_reads_of_expr,
    get_reads_of_stmts,
    get_writes_of_stmts,
    is_const_zero,
)
from .LoopIR_dataflow import LoopIR_Dependencies
from .new_eff import (
    SchedulingError,
    Check_ReorderStmts,
    Check_ReorderLoops,
    Check_FissionLoop,
    Check_DeleteConfigWrite,
    Check_ExtendEqv,
    Check_ExprEqvInContext,
    Check_BufferRW,
    Check_BufferReduceOnly,
    Check_Bounds,
    Check_IsDeadAfter,
    Check_IsIdempotent,
    Check_IsPositiveExpr,
    Check_Aliasing,
)
from .range_analysis import IndexRangeAnalysis
from .prelude import *
from .proc_eqv import get_strictest_eqv_proc
import exo.internal_cursors as ic
import exo.API as api
from .pattern_match import match_pattern


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Wrapper for LoopIR_Rewrite for scheduling directives which takes procedure cursor
# and returns Procedure object


class Cursor_Rewrite(LoopIR_Rewrite):
    def __init__(self, proc):
        self.provenance = proc
        self.orig_proc = proc._root()
        self.proc = self.apply_proc(self.orig_proc)

    def result(self, mod_config=None):
        return api.Procedure(
            self.proc, _provenance_eq_Procedure=self.provenance, _mod_config=mod_config
        )

    def map_proc(self, pc):
        p = pc._node
        new_args = self._map_list(self.map_fnarg, p.args)
        new_preds = self.map_exprs(p.preds)
        new_body = self.map_stmts(pc.body())

        if any((new_args is not None, new_preds is not None, new_body is not None)):
            new_preds = new_preds or p.preds
            new_preds = [
                p for p in new_preds if not (isinstance(p, LoopIR.Const) and p.val)
            ]
            return p.update(
                args=new_args or p.args, preds=new_preds, body=new_body or p.body
            )

        return None

    def apply_stmts(self, old):
        if (new := self.map_stmts(old)) is not None:
            return new
        return [o._node for o in old]

    def apply_s(self, old):
        if (new := self.map_s(old)) is not None:
            return new
        return [old._node]

    def map_stmts(self, stmts):
        new_stmts = []
        needs_update = False

        for s in stmts:
            s2 = self.map_s(s)
            if s2 is None:
                new_stmts.append(s._node)
            else:
                needs_update = True
                if isinstance(s2, list):
                    new_stmts.extend(s2)
                else:
                    new_stmts.append(s2)

        if not needs_update:
            return None

        return new_stmts

    def map_s(self, sc):
        s = sc._node
        if isinstance(s, (LoopIR.Assign, LoopIR.Reduce)):
            new_type = self.map_t(s.type)
            new_idx = self.map_exprs(s.idx)
            new_rhs = self.map_e(s.rhs)
            if any((new_type, new_idx is not None, new_rhs)):
                return [
                    s.update(
                        type=new_type or s.type,
                        idx=new_idx or s.idx,
                        rhs=new_rhs or s.rhs,
                    )
                ]
        elif isinstance(s, (LoopIR.WriteConfig, LoopIR.WindowStmt)):
            new_rhs = self.map_e(s.rhs)
            if new_rhs:
                return [s.update(rhs=new_rhs or s.rhs)]
        elif isinstance(s, LoopIR.If):
            new_cond = self.map_e(s.cond)
            new_body = self.map_stmts(sc.body())
            new_orelse = self.map_stmts(sc.orelse())
            if any((new_cond, new_body is not None, new_orelse is not None)):
                return [
                    s.update(
                        cond=new_cond or s.cond,
                        body=new_body or s.body,
                        orelse=new_orelse or s.orelse,
                    )
                ]
        elif isinstance(s, LoopIR.Seq):
            new_lo = self.map_e(s.lo)
            new_hi = self.map_e(s.hi)
            new_body = self.map_stmts(sc.body())
            if any((new_lo, new_hi, new_body is not None)):
                return [
                    s.update(
                        lo=new_lo or s.lo, hi=new_hi or s.hi, body=new_body or s.body
                    )
                ]
        elif isinstance(s, LoopIR.Call):
            new_args = self.map_exprs(s.args)
            if new_args is not None:
                return [s.update(args=new_args or s.args)]
        elif isinstance(s, LoopIR.Alloc):
            new_type = self.map_t(s.type)
            if new_type:
                return [s.update(type=new_type or s.type)]
        elif isinstance(s, LoopIR.Pass):
            return None
        else:
            raise NotImplementedError(f"bad case {type(s)}")
        return None


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Finding Names


def name_plus_count(namestr):
    results = re.search(r"^([a-zA-Z_]\w*)\s*(\#\s*([0-9]+))?$", namestr)
    if not results:
        raise TypeError(
            "expected name pattern of the form\n"
            "  ident (# integer)?\n"
            "where ident is the name of a variable "
            "and (e.g.) '#2' may optionally be attached to mean "
            "'the second occurence of that identifier"
        )

    name = results[1]
    count = int(results[3]) if results[3] else None
    return name, count


def iter_name_to_pattern(namestr):
    name, count = name_plus_count(namestr)
    if count is not None:
        count = f" #{count}"
    else:
        count = ""

    pattern = f"for {name} in _: _{count}"
    return pattern


def nested_iter_names_to_pattern(namestr, inner):
    name, count = name_plus_count(namestr)
    if count is not None:
        count = f" #{count}"
    else:
        count = ""
    assert is_valid_name(inner)

    pattern = f"for {name} in _:\n  for {inner} in _: _{count}"
    return pattern


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Cursor form scheduling directive helpers


def _compose(f, g):
    return lambda x: f(g(x))


def _replace_helper(c, c_repl, only_replace_attrs):
    if only_replace_attrs:
        ir, fwd_s = c.get_root(), lambda x: x
        assert isinstance(c_repl, dict)
        for attr in c_repl.keys():
            if attr in ["body", "orelse", "idx"]:
                ir, fwd_attr = fwd_s(c)._child_block(attr)._replace(c_repl[attr])
            else:
                ir, fwd_attr = fwd_s(c)._child_node(attr)._replace(c_repl[attr])
            fwd_s = _compose(fwd_attr, fwd_s)
        return ir, fwd_s
    else:
        if (
            isinstance(c, ic.Block)
            and not isinstance(c_repl, list)
            or isinstance(c, ic.Node)
            and c.get_index() is not None
        ):
            c_repl = [c_repl]
        return c._replace(c_repl)


# TODO KQ: make ones for read and write that consider ID
def _replace_pats(ir, fwd, c, pat, repl, only_replace_attrs=True):
    # TODO: consider the implications of composing O(n) forwarding functions.
    #   will we need a special data structure? A chunkier operation for
    #   multi-way replacement?
    cur_fwd = lambda x: x
    c = fwd(c)
    for rd in match_pattern(c, pat):
        rd = cur_fwd(rd)
        if not (c_repl := repl(rd)):
            continue
        ir, fwd_rd = _replace_helper(rd, c_repl, only_replace_attrs)
        cur_fwd = _compose(fwd_rd, cur_fwd)
    return ir, _compose(cur_fwd, fwd)


def _replace_pats_stmts(ir, fwd, c, pat, repl, only_replace_attrs=True):
    cur_fwd = lambda x: x
    c = fwd(c)
    for block in match_pattern(c, pat):
        # needed because match_pattern on stmts return blocks
        assert len(block) == 1
        s = cur_fwd(block[0])
        if not (c_repl := repl(s)):
            continue
        ir, fwd_s = _replace_helper(s, c_repl, only_replace_attrs)
        cur_fwd = _compose(fwd_s, cur_fwd)
    return ir, _compose(cur_fwd, fwd)


def get_rest_of_block(c):
    return c.as_block().expand(delta_lo=0)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Scheduling directives


# Take a conservative approach and allow stmt reordering only when they are
# writing to different buffers
# TODO: Do effectcheck's check_commutes-ish thing using SMT here
def DoReorderStmt(f_cursor, s_cursor):
    if f_cursor.next() != s_cursor:
        raise SchedulingError(
            "expected the second statement to be directly after the first"
        )
    Check_ReorderStmts(f_cursor.get_root(), f_cursor._node, s_cursor._node)
    ir, fwd = s_cursor._move(f_cursor.before())
    return ir, fwd


def DoPartitionLoop(stmt, partition_by):
    s = stmt._node

    assert isinstance(s, LoopIR.Seq)

    part_by = LoopIR.Const(partition_by, T.int, s.srcinfo)
    new_hi = LoopIR.BinOp("-", s.hi, part_by, T.int, s.srcinfo)
    try:
        Check_IsPositiveExpr(
            stmt.get_root(),
            [s],
            LoopIR.BinOp(
                "+", new_hi, LoopIR.Const(1, T.int, s.srcinfo), T.int, s.srcinfo
            ),
        )
    except SchedulingError:
        raise SchedulingError(
            f"expected the new loop bound {new_hi} to be always non-negative"
        )

    loop1 = Alpha_Rename([s.update(hi=part_by)]).result()[0]

    # all uses of the loop iteration in the second body need
    # to be offset by the partition value
    iter2 = s.iter.copy()
    iter2_node = LoopIR.Read(iter2, [], T.index, s.srcinfo)
    iter_off = LoopIR.BinOp("+", iter2_node, part_by, T.index, s.srcinfo)
    env = {s.iter: iter_off}

    body2 = SubstArgs(s.body, env).result()
    zero = LoopIR.Const(0, T.index, s.srcinfo)
    loop2 = Alpha_Rename(
        [s.update(iter=iter2, lo=zero, hi=new_hi, body=body2)]
    ).result()[0]

    ir, fwd = stmt._replace([loop1, loop2])
    return ir, fwd


def DoProductLoop(outer_loop_c, new_name):
    body = outer_loop_c.body()
    outer_loop = outer_loop_c._node

    if len(body) != 1 or not isinstance(body[0]._node, LoopIR.Seq):
        raise SchedulingError(
            f"expected loop directly inside of {body[0]._node.iter} loop"
        )

    inner_loop_c = body[0]
    inner_loop = inner_loop_c._node
    inner_hi = inner_loop.hi

    if not isinstance(inner_hi, LoopIR.Const):
        raise SchedulingError(
            f"expected the inner loop to have a constant bound, " f"got {inner_hi}."
        )

    if not (is_const_zero(inner_loop.lo) and is_const_zero(outer_loop.lo)):
        raise SchedulingError(
            f"expected the inner and outer loops to have a constant lower bound of 0, "
            f"got {inner_loop.lo} and {outer_loop.lo}."
        )

    # Only spend a name once the other parameters are validated
    new_var = Sym(new_name)

    # Construct replacement expressions
    srcinfo = inner_hi.srcinfo
    var = LoopIR.Read(new_var, [], T.index, srcinfo)
    outer_expr = LoopIR.BinOp("/", var, inner_hi, T.index, srcinfo)
    inner_expr = LoopIR.BinOp("%", var, inner_hi, T.index, srcinfo)

    # TODO: indexes are inside expression blocks... need a more
    #   uniform way to treat this.
    mk_outer_expr = lambda _: outer_expr
    mk_inner_expr = lambda _: inner_expr

    # Initial state of editing transaction
    ir, fwd = outer_loop_c.get_root(), lambda x: x

    # Replace inner reads to loop variables
    for c in inner_loop_c.body():
        ir, fwd = _replace_pats(
            ir, fwd, c, f"{outer_loop.iter}", mk_outer_expr, only_replace_attrs=False
        )
        ir, fwd = _replace_pats(
            ir, fwd, c, f"{inner_loop.iter}", mk_inner_expr, only_replace_attrs=False
        )

    ir, fwdRepl = fwd(outer_loop_c)._child_node("iter")._replace(new_var)
    fwd = _compose(fwdRepl, fwd)

    new_hi = LoopIR.BinOp("*", outer_loop.hi, inner_hi, T.index, outer_loop.srcinfo)
    ir, fwdRepl = fwd(outer_loop_c)._child_node("hi")._replace(new_hi)
    fwd = _compose(fwdRepl, fwd)

    ir, fwdMv = fwd(inner_loop_c).body()._move(fwd(inner_loop_c).after())
    fwd = _compose(fwdMv, fwd)

    ir, fwdDel = fwd(inner_loop_c)._delete()
    fwd = _compose(fwdDel, fwd)

    return ir, fwd


def same_index_exprs(proc_cursor, idx1, s1, idx2, s2):
    try:
        assert len(idx1) == len(idx2)
        for i, j in zip(idx1, idx2):
            Check_ExprEqvInContext(proc_cursor, i, [s1], j, [s2])
        return True
    except SchedulingError as e:
        return False


def same_write_dest(proc_cursor, s1, s2):
    return same_index_exprs(proc_cursor, s1.idx, s1, s2.idx, s2)


def DoMergeWrites(c1, c2):
    s1, s2 = c1._node, c2._node

    if not same_write_dest(c1.get_root(), s1, s2):
        raise SchedulingError("expected the left hand side's indices to be the same.")

    if any(
        s1.name == name and s1.type == typ for name, typ in get_reads_of_expr(s2.rhs)
    ):
        raise SchedulingError(
            "expected the right hand side of the second statement to not "
            "depend on the left hand side of the first statement."
        )

    # Always delete the first assignment or reduction
    ir, fwd = c1._delete()

    # If the second statement is a reduction, the first one's type
    # "wins" and we add the two right-hand sides together.
    if isinstance(s2, LoopIR.Reduce):
        ir, fwd_repl = fwd(c2)._replace(
            [s1.update(rhs=LoopIR.BinOp("+", s1.rhs, s2.rhs, s1.type, s1.srcinfo))]
        )
        fwd = _compose(fwd_repl, fwd)

    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Split scheduling directive


def DoSplit(loop_cursor, quot, hi, lo, tail="guard", perfect=False):
    split_loop = loop_cursor._node
    N = split_loop.hi
    hi_i = Sym(hi)
    lo_i = Sym(lo)
    srcinfo = split_loop.srcinfo

    if not is_const_zero(split_loop.lo):
        raise SchedulingError(
            f"expected the lower bound of the loop to be zero, got {split_loop.lo}."
        )

    assert quot > 1

    def substitute(srcinfo):
        cnst = lambda x: LoopIR.Const(x, T.int, srcinfo)
        rd = lambda x: LoopIR.Read(x, [], T.index, srcinfo)
        op = lambda op, lhs, rhs: LoopIR.BinOp(op, lhs, rhs, T.index, srcinfo)

        return op("+", op("*", cnst(quot), rd(hi_i)), rd(lo_i))

    # short-hands for sanity
    def boolop(op, lhs, rhs, typ):
        return LoopIR.BinOp(op, lhs, rhs, typ, srcinfo)

    def szop(op, lhs, rhs):
        return LoopIR.BinOp(op, lhs, rhs, lhs.type, srcinfo)

    def cnst(intval):
        return LoopIR.Const(intval, T.int, srcinfo)

    def rd(i):
        return LoopIR.Read(i, [], T.index, srcinfo)

    def ceildiv(lhs, rhs):
        assert isinstance(rhs, LoopIR.Const) and rhs.val > 1
        rhs_1 = cnst(rhs.val - 1)
        return szop("/", szop("+", lhs, rhs_1), rhs)

    ir, fwd = loop_cursor._child_node("iter")._replace(hi_i)

    tail_strategy = "perfect" if perfect else tail

    # wrap body in a guard
    if tail_strategy == "guard":
        idx_sub = substitute(srcinfo)

        def guard_wrapper(body):
            cond = boolop("<", idx_sub, N, T.bool)
            return LoopIR.If(cond, body, [], None, srcinfo)

        ir, fwd_wrap = fwd(loop_cursor).body()._wrap(guard_wrapper, "body")
        fwd = _compose(fwd_wrap, fwd)

    # determine loop bounds and wrap inner loop
    lo_rng = cnst(quot)
    if tail_strategy == "guard":
        hi_rng = ceildiv(N, lo_rng)
    elif tail_strategy in ["cut", "cut_and_guard"]:
        hi_rng = szop("/", N, lo_rng)  # floor div
    elif tail_strategy == "perfect":
        if not isinstance(N, LoopIR.Const):
            is_N_divisible = False
            for pred in loop_cursor.get_root().preds:
                if (
                    isinstance(pred, LoopIR.BinOp)
                    and pred.op == "=="
                    and isinstance(pred.rhs, LoopIR.Const)
                    and pred.rhs.val == 0
                    and isinstance(pred.lhs, LoopIR.BinOp)
                    and pred.lhs.op == "%"
                    and isinstance(pred.lhs.rhs, LoopIR.Const)
                    and pred.lhs.rhs.val > 0
                    and pred.lhs.rhs.val % quot == 0
                    and isinstance(pred.lhs.lhs, LoopIR.Read)
                    and pred.lhs.lhs.name == split_loop.hi.name
                ):
                    is_N_divisible = True

            if not is_N_divisible:
                raise SchedulingError(
                    f"cannot perfectly split the '{split_loop.iter}' loop."
                )

            hi_rng = boolop("/", N, cnst(quot), T.index)
        else:
            if N.val % quot != 0:
                raise SchedulingError(
                    f"cannot perfectly split the '{split_loop.iter}' loop "
                    f"because {quot} does not evenly divide "
                    f"{N.val}"
                )
            hi_rng = cnst(N.val // quot)
    else:
        assert False, f"bad tail strategy: {tail_strategy}"

    def inner_wrapper(body):
        return LoopIR.Seq(
            lo_i, LoopIR.Const(0, T.index, srcinfo), lo_rng, body, None, srcinfo
        )

    ir, fwd_repl = fwd(loop_cursor)._child_node("hi")._replace(hi_rng)
    fwd = _compose(fwd_repl, fwd)

    ir, fwd_wrap = fwd(loop_cursor).body()._wrap(inner_wrapper, "body")
    fwd = _compose(fwd_wrap, fwd)

    # replace the iteration variable in the body
    def mk_main_iter(c):
        return substitute(c._node.srcinfo)

    ir, fwd = _replace_pats(
        ir,
        fwd,
        loop_cursor,
        f"{split_loop.iter}",
        mk_main_iter,
        only_replace_attrs=False,
    )

    # add the tail case
    if tail_strategy in ["cut", "cut_and_guard"]:
        cut_i = Sym(lo)
        Ntail = szop("%", N, lo_rng)

        # in the tail loop we want the iteration variable to
        # be mapped instead to (Ncut*Q + cut_i)
        cut_tail_sub = szop("+", rd(cut_i), szop("*", hi_rng, lo_rng))

        cut_body = Alpha_Rename(split_loop.body).result()
        env = {split_loop.iter: cut_tail_sub}
        cut_body = SubstArgs(cut_body, env).result()

        cut_s = LoopIR.Seq(
            cut_i, LoopIR.Const(0, T.index, srcinfo), Ntail, cut_body, None, srcinfo
        )
        if tail_strategy == "cut_and_guard":
            cond = boolop(">", Ntail, LoopIR.Const(0, T.int, srcinfo), T.bool)
            cut_s = LoopIR.If(cond, [cut_s], [], None, srcinfo)

        ir, fwd_ins = fwd(loop_cursor).after()._insert([cut_s])
        fwd = _compose(fwd_ins, fwd)

    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Unroll scheduling directive


def DoUnroll(c_loop):
    s = c_loop._node

    if not isinstance(s.hi, LoopIR.Const) or not isinstance(s.lo, LoopIR.Const):
        raise SchedulingError(f"expected loop '{s.iter}' to have constant bounds")

    iters = s.hi.val - s.lo.val
    orig_body = c_loop.body().resolve_all()

    unrolled = []
    for i in range(s.lo.val, s.hi.val):
        env = {s.iter: LoopIR.Const(i, T.index, s.srcinfo)}
        unrolled += Alpha_Rename(SubstArgs(orig_body, env).result()).result()

    return c_loop._replace(unrolled)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Inline scheduling directive


def DoInline(call):
    s = call._node

    # handle potential window expressions in call positions
    win_binds = []

    def map_bind(nm, a):
        if isinstance(a, LoopIR.WindowExpr):
            stmt = LoopIR.WindowStmt(nm, a, None, a.srcinfo)
            win_binds.append(stmt)
            return LoopIR.Read(nm, [], a.type, a.srcinfo)
        return a

    # first, set-up a binding from sub-proc arguments
    # to supplied expressions at the call-site
    call_bind = {xd.name: map_bind(xd.name, a) for xd, a in zip(s.f.args, s.args)}

    # we will substitute the bindings for the call
    body = SubstArgs(s.f.body, call_bind).result()

    # note that all sub-procedure assertions must be true
    # even if not asserted, or else this call being inlined
    # wouldn't have been valid to make in the first place

    # whenever we copy code we need to alpha-rename for safety
    # the code to splice in at this point
    new_body = Alpha_Rename(win_binds + body).result()

    ir, fwd = call._replace(new_body)
    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Partial Evaluation scheduling directive


class DoPartialEval(LoopIR_Rewrite):
    def __init__(self, env):
        assert env, "Don't call _PartialEval without any substitutions"
        self.env = env

    def map_proc(self, p):
        # Validate env:
        arg_types = {x.name: x.type for x in p.args}
        for k, v in self.env.items():
            if not arg_types[k].is_indexable() and not arg_types[k].is_bool():
                raise SchedulingError(
                    "cannot partially evaluate numeric (non-index, non-bool) arguments"
                )
            if not isinstance(v, int):
                raise SchedulingError(
                    "cannot partially evaluate to a non-int, non-bool value"
                )

        p = super().map_proc(p) or p

        return p.update(args=[a for a in p.args if a.name not in self.env])

    def map_e(self, e):
        if isinstance(e, LoopIR.Read):
            if e.type.is_indexable():
                assert len(e.idx) == 0
                if e.name in self.env:
                    return LoopIR.Const(self.env[e.name], T.int, e.srcinfo)
            elif e.type.is_bool():
                if e.name in self.env:
                    return LoopIR.Const(self.env[e.name], T.bool, e.srcinfo)

        return super().map_e(e)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Set Type and/or Memory Annotations scheduling directive


def DoSetTypAndMem(cursor, basetyp=None, win=None, mem=None):
    s = cursor._node
    oldtyp = s.type
    assert oldtyp.is_numeric()

    ir, fwd = cursor._root, lambda x: x
    if basetyp:
        assert basetyp.is_real_scalar()
        if oldtyp.is_real_scalar():
            ir, fwd_repl = cursor._child_node("type")._replace(basetyp)
            fwd = _compose(fwd_repl, fwd)
        elif isinstance(oldtyp, T.Tensor):
            assert oldtyp.type.is_real_scalar()
            ir, fwd_repl = (
                cursor._child_node("type")._child_node("type")._replace(basetyp)
            )
            fwd = _compose(fwd_repl, fwd)
        else:
            assert False, "bad case"
    elif win:
        if isinstance(s, LoopIR.Alloc):
            raise SchedulingError(
                "cannot change an allocation to " "be or not be a window"
            )
        if not oldtyp.is_tensor_or_window():
            raise SchedulingError(
                "cannot change windowing of a " "non-tensor/window argument"
            )

        assert isinstance(oldtyp, T.Tensor)
        assert isinstance(win, bool)
        ir, fwd_repl = cursor._child_node("type")._replace(oldtyp.update(is_window=win))
        fwd = _compose(fwd_repl, fwd)
    elif mem:
        ir, fwd_repl = cursor._child_node("mem")._replace(mem)
        fwd = _compose(fwd_repl, fwd)

    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Call Swap scheduling directive


def DoCallSwap(call_cursor, new_subproc):
    call_s = call_cursor._node
    assert isinstance(call_s, LoopIR.Call)

    is_eqv, configkeys = get_strictest_eqv_proc(call_s.f, new_subproc)
    if not is_eqv:
        raise SchedulingError(
            f"{call_s.srcinfo}: Cannot swap call because the two "
            f"procedures are not equivalent"
        )

    s_new = call_s.update(f=new_subproc)
    ir = call_cursor.get_root()
    mod_cfg = Check_ExtendEqv(ir, [call_s], [s_new], configkeys)
    ir, fwd = call_cursor._child_node("f")._replace(new_subproc)

    Check_Aliasing(ir)

    return ir, fwd, mod_cfg


def DoInlineWindow(window_cursor):
    window_s = window_cursor._node
    assert isinstance(window_s, LoopIR.WindowStmt)

    ir, fwd = window_cursor._delete()

    def calc_idx(idxs):
        win_idx = window_s.rhs.idx
        idxs = idxs.copy()  # make function non-destructive to input
        assert len(idxs) == sum([isinstance(w, LoopIR.Interval) for w in win_idx])

        def add(x, y):
            return LoopIR.BinOp("+", x, y, T.index, x.srcinfo)

        if len(idxs) > 0 and isinstance(idxs[0], LoopIR.w_access):

            def map_w(w):
                if isinstance(w, LoopIR.Point):
                    return w
                # i is from the windowing expression we're substituting into
                i = idxs.pop(0)
                if isinstance(i, LoopIR.Point):
                    return LoopIR.Point(add(i.pt, w.lo), i.srcinfo)
                else:
                    return LoopIR.Interval(add(i.lo, w.lo), add(i.hi, w.lo), i.srcinfo)

        else:

            def map_w(w):
                return w.pt if isinstance(w, LoopIR.Point) else add(idxs.pop(0), w.lo)

        return [map_w(w) for w in win_idx]

    # used to offset the stride in order to account for
    # dimensions hidden due to window-point accesses
    def calc_dim(dim):
        assert dim < len(
            [w for w in window_s.rhs.idx if isinstance(w, LoopIR.Interval)]
        )

        # Because our goal here is to offset `dim` in the original
        # call argument to the point indexing to the windowing expression,
        # new_dim should essencially be:
        # `dim` + "number of LoopIR.Points in the windowing expression before the `dim` number of LoopIR.Interval"
        new_dim = 0
        for w in window_s.rhs.idx:
            if isinstance(w, LoopIR.Interval):
                dim -= 1
            if dim == -1:
                return new_dim
            new_dim += 1

    def mk_read(c):
        rd = c._node
        buf_name = window_s.rhs.name

        if isinstance(rd, LoopIR.WindowExpr):
            new_idxs = calc_idx(rd.idx)
            old_typ = window_s.rhs.type
            new_typ = old_typ.update(src_buf=buf_name, idx=new_idxs)
            return {"name": buf_name, "idx": new_idxs, "type": new_typ}
        elif isinstance(rd, LoopIR.Read):
            new_idxs = calc_idx(rd.idx)
            return {"name": buf_name, "idx": new_idxs}

    def mk_stride_expr(c):
        e = c._node
        dim = calc_dim(e.dim)
        buf_name = window_s.rhs.name
        return {"name": buf_name, "dim": dim}

    def mk_write(c):
        s = c._node
        idxs = calc_idx(s.idx)
        return {"name": window_s.rhs.name, "idx": idxs}

    for c in get_rest_of_block(window_cursor)[1:]:
        ir, fwd = _replace_pats(ir, fwd, c, f"{window_s.lhs}[_]", mk_read)
        ir, fwd = _replace_pats(
            ir, fwd, c, f"stride({window_s.lhs}, _)", mk_stride_expr
        )
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{window_s.lhs} = _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{window_s.lhs} += _", mk_write)

    return ir, fwd


def DoConfigWrite(stmt_cursor, config, field, expr, before=False):
    assert isinstance(expr, (LoopIR.Read, LoopIR.StrideExpr, LoopIR.Const))
    s = stmt_cursor._node

    cw_s = LoopIR.WriteConfig(config, field, expr, None, s.srcinfo)

    if before:
        ir, fwd = stmt_cursor.before()._insert([cw_s])
    else:
        ir, fwd = stmt_cursor.after()._insert([cw_s])

    cfg = Check_DeleteConfigWrite(ir, [cw_s])

    return ir, fwd, cfg


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Bind Expression scheduling directive


def DoBindConfig(config, field, expr_cursor):
    e = expr_cursor._node
    assert isinstance(e, LoopIR.Read)

    c = expr_cursor
    while not isinstance(c._node, LoopIR.stmt):
        c = c.parent()

    cfg_write_s = LoopIR.WriteConfig(config, field, e, None, e.srcinfo)
    ir, fwd = c.before()._insert([cfg_write_s])

    mod_cfg = Check_DeleteConfigWrite(ir, [cfg_write_s])

    cfg_read_e = LoopIR.ReadConfig(config, field, e.type, e.srcinfo)
    if isinstance(expr_cursor.parent()._node, LoopIR.Call):
        cfg_read_e = [cfg_read_e]
    ir, fwd_repl = fwd(expr_cursor)._replace(cfg_read_e)
    fwd = _compose(fwd_repl, fwd)

    Check_Aliasing(ir)
    return ir, fwd, mod_cfg


def DoCommuteExpr(expr_cursors):
    ir, fwd = expr_cursors[0].get_root(), lambda x: x
    for expr_c in expr_cursors:
        e = expr_c._node
        assert isinstance(e, LoopIR.BinOp)
        ir, fwd_repl = fwd(expr_c._child_node("lhs"))._replace(e.rhs)
        fwd = _compose(fwd_repl, fwd)
        ir, fwd_repl = fwd(expr_c._child_node("rhs"))._replace(e.lhs)
        fwd = _compose(fwd_repl, fwd)
    return ir, fwd


def get_enclosing_stmt_cursor(c):
    while isinstance(c._node, LoopIR.expr):
        c = c.parent()
    assert isinstance(c._node, LoopIR.stmt)
    return c


def less(c1, c2):
    p1, p2 = c1._path, c2._path
    for i in range(min(len(p1), len(p2))):
        if p1[i] < p2[i]:
            return True
        elif p1[i] > p2[i]:
            return False
    return len(p1) < len(p2)


def DoBindExpr(new_name, expr_cursors, cse=False):
    assert expr_cursors

    if not cse:
        expr_cursors = expr_cursors[0:1]

    expr = expr_cursors[0]._node
    assert isinstance(expr, LoopIR.expr)
    assert expr.type.is_numeric()

    expr_reads = [name for (name, typ) in get_reads_of_expr(expr)]
    # TODO: dirty hack. need real CSE-equality (i.e. modulo srcinfo)
    expr_cursors = [c for c in expr_cursors if str(c._node) == str(expr)]

    init_c = get_enclosing_stmt_cursor(expr_cursors[0])

    new_name = Sym(new_name)
    alloc_s = LoopIR.Alloc(new_name, T.R, None, None, expr.srcinfo)
    assign_s = LoopIR.Assign(new_name, T.R, None, [], expr, None, expr.srcinfo)
    ir, fwd = init_c.before()._insert([alloc_s, assign_s])

    new_read = LoopIR.Read(new_name, [], expr.type, expr.srcinfo)
    first_write_c = None
    for c in get_rest_of_block(init_c):
        for block in match_pattern(c, "_ = _"):
            assert len(block) == 1
            sc = block[0]
            if sc._node.name in expr_reads:
                first_write_c = sc
                break
        for block in match_pattern(c, "_ += _"):
            assert len(block) == 1
            sc = block[0]
            if sc._node.name in expr_reads:
                if first_write_c:
                    if less(sc, first_write_c):
                        first_write_c = sc
                else:
                    first_write_c = sc
                break
        if first_write_c:
            while expr_cursors and (
                less(expr_cursors[0], first_write_c)
                or first_write_c.is_ancestor_of(expr_cursors[0])
            ):
                ir, fwd_repl = _replace_helper(
                    fwd(expr_cursors[0]), new_read, only_replace_attrs=False
                )
                fwd = _compose(fwd_repl, fwd)
                expr_cursors.pop(0)
            break
        else:
            while expr_cursors and c.is_ancestor_of(expr_cursors[0]):
                ir, fwd_repl = _replace_helper(
                    fwd(expr_cursors[0]), new_read, only_replace_attrs=False
                )
                fwd = _compose(fwd_repl, fwd)
                expr_cursors.pop(0)

    Check_Aliasing(ir)
    return ir, fwd


def DoLiftScope(inner_c):
    inner_s = inner_c._node
    assert isinstance(inner_s, (LoopIR.If, LoopIR.Seq))
    target_type = "if statement" if isinstance(inner_s, LoopIR.If) else "for loop"

    outer_c = inner_c.parent()
    if outer_c.root() == outer_c:
        raise SchedulingError("Cannot lift scope of top-level statement")
    outer_s = outer_c._node

    ir, fwd = inner_c.get_root(), lambda x: x

    if isinstance(outer_s, LoopIR.If):

        def if_wrapper(body, insert_orelse=False):
            src = outer_s.srcinfo
            # this is needed because _replace expects a non-zero length block
            orelse = [LoopIR.Pass(None, src)] if insert_orelse else []
            return LoopIR.If(outer_s.cond, body, orelse, None, src)

        def orelse_wrapper(orelse):
            src = outer_s.srcinfo
            body = [LoopIR.Pass(None, src)]
            return LoopIR.If(outer_s.cond, body, orelse, None, src)

        if isinstance(inner_s, LoopIR.If):
            if inner_s in outer_s.body:
                #                    if INNER:
                # if OUTER:            if OUTER: A
                #   if INNER: A        else:     C
                #   else:     B  ~>  else:
                # else: C              if OUTER: B
                #                      else:     C
                if len(outer_s.body) > 1:
                    raise SchedulingError(
                        f"expected {target_type} to be directly nested in parent"
                    )

                blk_c = outer_s.orelse
                wrapper = lambda body: if_wrapper(body, insert_orelse=bool(blk_c))

                ir, fwd = inner_c.body()._wrap(wrapper, "body")
                if blk_c:
                    ir, fwd_repl = fwd(inner_c).body()[0].orelse()._replace(blk_c)
                    fwd = _compose(fwd_repl, fwd)

                if inner_s.orelse:
                    ir, fwd_wrap = fwd(inner_c).orelse()._wrap(wrapper, "body")
                    fwd = _compose(fwd_wrap, fwd)
                    if blk_c:
                        ir, fwd_repl = fwd(inner_c).orelse()[0].orelse()._replace(blk_c)
                        fwd = _compose(fwd_repl, fwd)
            else:
                #                    if INNER:
                # if OUTER: A          if OUTER: A
                # else:                else:     B
                #   if INNER: B  ~>  else:
                #   else: C            if OUTER: A
                #                      else:     C
                assert inner_s in outer_s.orelse
                if len(outer_s.orelse) > 1:
                    raise SchedulingError(
                        f"expected {target_type} to be directly nested in parent"
                    )

                blk_a = outer_s.body

                ir, fwd = inner_c.body()._wrap(orelse_wrapper, "orelse")
                ir, fwd_repl = fwd(inner_c).body()[0].body()._replace(blk_a)
                fwd = _compose(fwd_repl, fwd)

                if inner_s.orelse:
                    ir, fwd_wrap = fwd(inner_c).orelse()._wrap(orelse_wrapper, "orelse")
                    fwd = _compose(fwd_wrap, fwd)
                    ir, fwd_repl = fwd(inner_c).orelse()[0].body()._replace(blk_a)
                    fwd = _compose(fwd_repl, fwd)
        elif isinstance(inner_s, LoopIR.Seq):
            # if OUTER:                for INNER in _:
            #   for INNER in _: A  ~>    if OUTER: A
            if len(outer_s.body) > 1:
                raise SchedulingError(
                    f"expected {target_type} to be directly nested in parent"
                )

            if outer_s.orelse:
                raise SchedulingError(
                    "cannot lift for loop when if has an orelse clause"
                )

            ir, fwd = inner_c.body()._move(inner_c.after())
            ir, fwd_move = fwd(inner_c)._move(fwd(outer_c).after())
            fwd = _compose(fwd_move, fwd)
            ir, fwd_move = fwd(outer_c)._move(fwd(inner_c).body()[0].after())
            fwd = _compose(fwd_move, fwd)
            ir, fwd_del = fwd(inner_c).body()[0]._delete()
            fwd = _compose(fwd_del, fwd)

            return ir, fwd

    elif isinstance(outer_s, LoopIR.Seq):
        if len(outer_s.body) > 1:
            raise SchedulingError(
                f"expected {target_type} to be directly nested in parent"
            )

        def loop_wrapper(body):
            return outer_s.update(body=body)

        if isinstance(inner_s, LoopIR.If):
            # for OUTER in _:      if INNER:
            #   if INNER: A    ~>    for OUTER in _: A
            #   else:     B        else:
            #                        for OUTER in _: B
            if outer_s.iter in _FV(inner_s.cond):
                raise SchedulingError("if statement depends on iteration variable")

            ir, fwd = inner_c.body()._wrap(loop_wrapper, "body")

            if inner_s.orelse:
                ir, fwd_wrap = fwd(inner_c).orelse()._wrap(loop_wrapper, "body")
                fwd = _compose(fwd_wrap, fwd)
        elif isinstance(inner_s, LoopIR.Seq):
            # for OUTER in _:          for INNER in _:
            #   for INNER in _: A  ~>    for OUTER in _: A
            reads = get_reads_of_expr(inner_s.lo) + get_reads_of_expr(inner_s.hi)
            if outer_s.iter in [name for name, _ in reads]:
                raise SchedulingError(
                    "inner loop's lo or hi depends on outer loop's iteration variable"
                )

            Check_ReorderLoops(inner_c.get_root(), outer_s)
            ir, fwd = inner_c._move(outer_c.after())
            ir, fwd_move = fwd(outer_c)._move(fwd(inner_c).body()[0].before())
            fwd = _compose(fwd_move, fwd)
            ir, fwd_move = fwd(inner_c).body()[1:]._move(fwd(outer_c).body()[0].after())
            fwd = _compose(fwd_move, fwd)
            ir, fwd_del = fwd(outer_c).body()[0]._delete()
            fwd = _compose(fwd_del, fwd)
            return ir, fwd

    ir, fwd_move = fwd(inner_c)._move(fwd(outer_c).after())
    fwd = _compose(fwd_move, fwd)
    ir, fwd_del = fwd(outer_c)._delete()
    fwd = _compose(fwd_del, fwd)

    return ir, fwd


def DoLiftConstant(assign_c, loop_c):
    orig_proc = assign_c.get_root()
    assign_s = assign_c._node
    loop = loop_c._node

    for (name, typ) in get_reads_of_stmts(loop.body):
        if assign_s.name == name and assign_s.type == typ:
            raise SchedulingError(
                "cannot lift constant because the buffer is read in the loop body"
            )

    only_has_scaled_reduces = True

    def find_relevant_scaled_reduces(stmts_c):
        nonlocal only_has_scaled_reduces
        reduces = []
        for sc in stmts_c:
            s = sc._node
            if isinstance(s, LoopIR.Assign):
                if s.name == assign_s.name:
                    only_has_scaled_reduces = False
            elif isinstance(s, LoopIR.Reduce):
                if s.name != assign_s.name:
                    continue

                if not (
                    same_write_dest(orig_proc, assign_s, s)
                    and isinstance(s.rhs, LoopIR.BinOp)
                    and s.rhs.op == "*"
                    and isinstance(s.rhs.lhs, (LoopIR.Const, LoopIR.Read))
                ):
                    only_has_scaled_reduces = False

                reduces.append(sc)
            elif isinstance(s, LoopIR.If):
                reduces += find_relevant_scaled_reduces(sc.body())
                if s.orelse:
                    reduces += find_relevant_scaled_reduces(sc.orelse())
            elif isinstance(s, LoopIR.Seq):
                reduces += find_relevant_scaled_reduces(sc.body())
            elif isinstance(s, (LoopIR.WindowStmt, LoopIR.WriteConfig, LoopIR.Call)):
                raise NotImplementedError(
                    f"unsupported stmt type in loop body: {type(s)}"
                )
            elif isinstance(s, (LoopIR.Pass, LoopIR.Alloc, LoopIR.Free)):
                pass
            else:
                raise NotImplementedError(f"unknown stmt type {type(s)}")
        return reduces

    relevant_reduces = find_relevant_scaled_reduces(loop_c.body())

    if not only_has_scaled_reduces:
        raise SchedulingError(
            f"cannot lift constant because there are other operations on the same buffer that may interfere"
        )
    if len(relevant_reduces) == 0:
        raise SchedulingError(
            "cannot lift constant because did not find a reduce in the loop body of the form `buffer += c * expr`"
        )

    def reduces_have_same_constant(s1, s2):
        c1 = s1.rhs.lhs
        c2 = s2.rhs.lhs
        if isinstance(c1, LoopIR.Const) and isinstance(c2, LoopIR.Const):
            return c1.val == c2.val
        elif isinstance(c1, LoopIR.Read) and isinstance(c2, LoopIR.Read):
            return c1.name == c2.name and same_index_exprs(
                orig_proc,
                c1.idx,
                s1,
                c2.idx,
                s2,
            )
        else:
            return False

    # check that reduces have the same constant scaling factor
    for s in relevant_reduces[1:]:
        if not reduces_have_same_constant(relevant_reduces[0]._node, s._node):
            raise SchedulingError(
                f"cannot lift constant because the reduces to buffer {assign_s.name} in the loop body have different constants"
            )

    constant = relevant_reduces[0]._node.rhs.lhs
    if isinstance(constant, LoopIR.Read):
        for (name, typ) in get_writes_of_stmts(loop.body):
            if constant.name == name and constant.type == typ:
                raise SchedulingError(
                    "cannot lift constant because it is a buffer that is written in the loop body"
                )

    ir, fwd = orig_proc, lambda x: x

    # replace all the relevant reduce statements
    for sc in relevant_reduces:
        rhs_c = sc._child_node("rhs")
        rhs = rhs_c._node
        ir, fwd_repl = fwd(rhs_c)._replace(rhs.rhs)
        fwd = _compose(fwd_repl, fwd)

    # insert new scaled assign statement after loop
    new_assign_buffer_read = LoopIR.Read(
        assign_s.name,
        assign_s.idx,
        assign_s.type,
        assign_s.srcinfo,
    )
    new_assign_rhs = LoopIR.BinOp(
        "*",
        constant,
        new_assign_buffer_read,
        assign_s.type,
        assign_s.srcinfo,
    )
    new_assign = assign_s.update(rhs=new_assign_rhs)
    ir, fwd_ins = fwd(loop_c).after()._insert([new_assign])
    fwd = _compose(fwd_ins, fwd)

    return ir, fwd


def DoExpandDim(alloc_cursor, alloc_dim, indexing):
    alloc_s = alloc_cursor._node
    assert isinstance(alloc_s, LoopIR.Alloc)
    assert isinstance(alloc_dim, LoopIR.expr)
    assert isinstance(indexing, LoopIR.expr)

    Check_IsPositiveExpr(alloc_cursor.get_root(), [alloc_s], alloc_dim)

    old_typ = alloc_s.type
    new_rngs = [alloc_dim]
    if isinstance(old_typ, T.Tensor):
        new_rngs += old_typ.shape()
    basetyp = old_typ.basetype()
    new_typ = T.Tensor(new_rngs, False, basetyp)
    new_alloc = alloc_s.update(type=new_typ)

    ir, fwd = alloc_cursor._child_node("type")._replace(new_typ)

    def mk_read(c):
        rd = c._node

        # TODO: do I need to worry about Builtins too?
        if isinstance(c.parent()._node, (LoopIR.Call)) and not rd.idx:
            raise SchedulingError(
                "TODO: Please Contact the developers to fix (i.e. add) "
                "support for passing windows to scalar arguments"
            )

        if isinstance(rd, LoopIR.Read):
            return {"idx": [indexing] + rd.idx}
        elif isinstance(rd, LoopIR.WindowExpr):
            return {"idx": [LoopIR.Point(indexing, rd.srcinfo)] + rd.idx}
        else:
            raise NotImplementedError(
                f"Did not implement {type(rd)}. This may be a bug."
            )

    def mk_write(c):
        s = c._node
        return {"idx": [indexing] + s.idx}

    for c in get_rest_of_block(alloc_cursor):
        ir, fwd = _replace_pats(ir, fwd, c, f"{alloc_s.name}[_]", mk_read)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_s.name} = _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_s.name} += _", mk_write)

    idx = alloc_cursor.get_index()
    after_alloc = [c._node for c in fwd(alloc_cursor.parent()).body()[idx + 1 :]]
    Check_Bounds(ir, new_alloc, after_alloc)

    return ir, fwd


def DoRearrangeDim(decl_cursor, permute_vector):
    decl_s = decl_cursor._node
    assert isinstance(decl_s, (LoopIR.Alloc, LoopIR.fnarg))

    all_permute = {decl_s.name: permute_vector}

    def permute(buf, es):
        permutation = all_permute[buf]
        return [es[i] for i in permutation]

    def check_permute_window(buf, idx):
        # for now just enforce a stability criteria on windowing
        # expressions w.r.t. dimension reordering
        permutation = all_permute[buf]
        # where each index of the output window now refers to in the
        # buffer being windowed
        keep_perm = [i for i in permutation if isinstance(idx[i], LoopIR.Interval)]
        # check that these indices are monotonic
        for i, ii in zip(keep_perm[:-1], keep_perm[1:]):
            if i > ii:
                return False
        return True

    # construct new_hi
    new_hi = permute(decl_s.name, decl_s.type.hi)
    # construct new_type
    new_type = LoopIR.Tensor(new_hi, decl_s.type.is_window, decl_s.type.type)
    ir, fwd = decl_cursor._child_node("type")._replace(new_type)

    def mk_read(c):
        rd = c._node
        if isinstance(c.parent()._node, LoopIR.Call):
            raise SchedulingError(
                f"Cannot permute buffer '{rd.name}' because it is "
                f"passed as a sub-procedure argument at {rd.srcinfo}"
            )

        if not rd.name in all_permute:
            return None

        if isinstance(rd, LoopIR.WindowExpr) and not check_permute_window(
            rd.name, rd.idx
        ):
            raise SchedulingError(
                f"Permuting the window expression at {rd.srcinfo} "
                f"would change the meaning of the window; "
                f"propagating dimension rearrangement through "
                f"windows is not currently supported"
            )
        return {"idx": permute(rd.name, rd.idx)}

    def mk_write(c):
        s = c._node
        if s.name in all_permute:
            new_idx = permute(s.name, s.idx)
            return {"idx": new_idx}

    def mk_stride_expr(c):
        e = c._node
        if e.name in all_permute:
            new_dim = all_permute[e.name].index(e.dim)
            return {"dim": new_dim}

    if isinstance(decl_s, LoopIR.Alloc):
        rest_of_block = get_rest_of_block(decl_cursor)
    else:
        rest_of_block = decl_cursor.root().body()
    for c in rest_of_block:
        for name in all_permute.keys():
            ir, fwd = _replace_pats(ir, fwd, c, f"{name}[_]", mk_read)
            ir, fwd = _replace_pats(ir, fwd, c, f"stride({name}, _)", mk_stride_expr)

            ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{name} = _", mk_write)
            ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{name} += _", mk_write)

    return ir, fwd


def DoDivideDim(alloc_cursor, dim_idx, quotient):
    alloc_s = alloc_cursor._node
    alloc_sym = alloc_s.name

    assert isinstance(alloc_s, LoopIR.Alloc)
    assert isinstance(dim_idx, int)
    assert isinstance(quotient, int)

    old_typ = alloc_s.type
    old_shp = old_typ.shape()
    dim = old_shp[dim_idx]
    if not isinstance(dim, LoopIR.Const):
        raise SchedulingError(f"Cannot divide non-literal dimension: {dim}")
    if not dim.val % quotient == 0:
        raise SchedulingError(f"Cannot divide {dim.val} evenly by {quotient}")
    denom = quotient
    numer = dim.val // denom
    new_shp = (
        old_shp[:dim_idx]
        + [
            LoopIR.Const(numer, T.int, dim.srcinfo),
            LoopIR.Const(denom, T.int, dim.srcinfo),
        ]
        + old_shp[dim_idx + 1 :]
    )
    new_typ = T.Tensor(new_shp, False, old_typ.basetype())

    ir, fwd = alloc_cursor._child_node("type")._replace(new_typ)

    def remap_idx(idx):
        orig_i = idx[dim_idx]
        srcinfo = orig_i.srcinfo
        quot = LoopIR.Const(quotient, T.int, srcinfo)
        hi = LoopIR.BinOp("/", orig_i, quot, orig_i.type, srcinfo)
        lo = LoopIR.BinOp("%", orig_i, quot, orig_i.type, srcinfo)
        return idx[:dim_idx] + [hi, lo] + idx[dim_idx + 1 :]

    def mk_read(c):
        rd = c._node

        if isinstance(rd, LoopIR.Read) and not rd.idx:
            raise SchedulingError(
                f"Cannot divide {alloc_sym} because buffer is passed as an argument"
            )
        elif isinstance(rd, LoopIR.WindowExpr):
            raise SchedulingError(
                f"Cannot divide {alloc_sym} because the buffer is windowed later on"
            )

        return {"idx": remap_idx(rd.idx)}

    def mk_write(c):
        s = c._node
        return {"idx": remap_idx(s.idx)}

    # TODO: add better iteration primitive
    for c in get_rest_of_block(alloc_cursor):
        ir, fwd = _replace_pats(ir, fwd, c, f"{alloc_s.name}[_]", mk_read)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_s.name} = _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_s.name} += _", mk_write)

    return ir, fwd


def DoMultiplyDim(alloc_cursor, hi_idx, lo_idx):
    alloc_s = alloc_cursor._node
    alloc_sym = alloc_s.name

    assert isinstance(alloc_s, LoopIR.Alloc)
    assert isinstance(hi_idx, int)
    assert isinstance(lo_idx, int)

    lo_dim = alloc_s.type.shape()[lo_idx]
    if not isinstance(lo_dim, LoopIR.Const):
        raise SchedulingError(
            f"Cannot multiply with non-literal second dimension: {str(lo_dim)}"
        )

    lo_val = lo_dim.val

    old_typ = alloc_s.type
    shp = old_typ.shape().copy()
    hi_dim = shp[hi_idx]
    lo_dim = shp[lo_idx]
    prod = LoopIR.BinOp("*", lo_dim, hi_dim, hi_dim.type, hi_dim.srcinfo)
    shp[hi_idx] = prod
    del shp[lo_idx]
    new_typ = T.Tensor(shp, False, old_typ.basetype())

    ir, fwd = alloc_cursor._child_node("type")._replace(new_typ)

    def remap_idx(idx):
        hi = idx[hi_idx]
        lo = idx[lo_idx]
        mulval = LoopIR.Const(lo_val, T.int, hi.srcinfo)
        mul_hi = LoopIR.BinOp("*", mulval, hi, hi.type, hi.srcinfo)
        prod = LoopIR.BinOp("+", mul_hi, lo, T.index, hi.srcinfo)
        idx[hi_idx] = prod
        del idx[lo_idx]
        return idx

    def mk_read(c):
        rd = c._node

        if isinstance(rd, LoopIR.Read) and not rd.idx:
            raise SchedulingError(
                f"Cannot multiply {alloc_sym} because "
                f"buffer is passed as an argument"
            )

        if isinstance(rd, LoopIR.WindowExpr):
            raise SchedulingError(
                f"Cannot multiply {alloc_sym} because "
                f"the buffer is windowed later on"
            )

        return {"idx": remap_idx(rd.idx)}

    def mk_write(c):
        s = c._node
        return {"idx": remap_idx(s.idx)}

    for c in get_rest_of_block(alloc_cursor):
        ir, fwd = _replace_pats(ir, fwd, c, f"{alloc_s.name}[_]", mk_read)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_s.name} = _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_s.name} += _", mk_write)

    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# *Only* lifting an allocation


def DoLiftAllocSimple(alloc_cursor, n_lifts):
    alloc_stmt = alloc_cursor._node

    assert isinstance(alloc_stmt, LoopIR.Alloc)
    assert is_pos_int(n_lifts)

    szvars = set()
    if alloc_stmt.type.shape():
        szvars = set.union(*[_FV(sz) for sz in alloc_stmt.type.shape()])

    stmt_c = alloc_cursor
    for i in range(n_lifts):
        try:
            stmt_c = stmt_c.parent()
            if stmt_c == stmt_c.root():
                raise ic.InvalidCursorError(
                    f"Cannot lift allocation {alloc_stmt} beyond its root proc."
                )
            if isinstance(stmt_c._node, LoopIR.Seq):
                if stmt_c._node.iter in szvars:
                    raise SchedulingError(
                        f"Cannot lift allocation statement {alloc_stmt} past loop "
                        f"with iteration variable {i} because "
                        f"the allocation size depends on {i}."
                    )
        except ic.InvalidCursorError:
            raise SchedulingError(
                f"specified lift level {n_lifts} is more than {i}, "
                "the number of loops and ifs above the allocation"
            )

    gap_c = stmt_c.before()
    ir, fwd = alloc_cursor._move(gap_c)
    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Lift Allocation scheduling directive

# TODO: Implement autolift_alloc's logic using high-level scheduling metaprogramming and
#       delete this code
class DoLiftAlloc(Cursor_Rewrite):
    def __init__(self, proc, alloc_cursor, n_lifts, mode, size, keep_dims):
        self.alloc_stmt = alloc_cursor._node

        assert isinstance(self.alloc_stmt, LoopIR.Alloc)
        assert is_pos_int(n_lifts)

        if mode not in ("row", "col"):
            raise SchedulingError(f"Unknown lift mode {mode}, should be 'row' or 'col'")

        self.alloc_sym = self.alloc_stmt.name
        self.alloc_deps = LoopIR_Dependencies(
            self.alloc_sym, proc._loopir_proc.body
        ).result()
        self.lift_mode = mode
        self.lift_size = size
        self.keep_dims = keep_dims

        self.n_lifts = n_lifts

        self.ctrl_ctxt = []
        self.lift_site = None

        self.lifted_stmt = None
        self.access_idxs = None
        self.alloc_type = None
        self._in_call_arg = False

        super().__init__(proc)

    def idx_mode(self, access, orig):
        if self.lift_mode == "row":
            return access + orig
        elif self.lift_mode == "col":
            return orig + access
        assert False

    def map_s(self, sc):
        s = sc._node
        if s is self.alloc_stmt:
            if self.n_lifts > len(self.ctrl_ctxt):
                raise SchedulingError(
                    f"specified lift level {self.n_lifts} "
                    "is higher than the number of loop "
                    f"{len(self.ctrl_ctxt)}"
                )
            self.lift_site = self.ctrl_ctxt[-self.n_lifts]

            # extract the ranges and variables of enclosing loops
            idxs, rngs = self.get_ctxt_itrs_and_rngs(self.n_lifts)

            # compute the lifted allocation buffer type, and
            # the new allocation statement
            new_typ = s.type
            new_rngs = []
            for r in rngs:
                if isinstance(r, LoopIR.Const):
                    assert r.val > 0, "Loop bound must be positive"
                    new_rngs.append(r)
                else:
                    new_rngs.append(
                        LoopIR.BinOp(
                            "+",
                            r,
                            LoopIR.Const(1, T.int, r.srcinfo),
                            T.index,
                            r.srcinfo,
                        )
                    )

            if isinstance(new_typ, T.Tensor):
                if self.lift_mode == "row":
                    new_rngs += new_typ.shape()
                elif self.lift_mode == "col":
                    new_rngs = new_typ.shape() + new_rngs
                else:
                    assert False

                new_typ = new_typ.basetype()

            if len(new_rngs) > 0:
                new_typ = T.Tensor(new_rngs, False, new_typ)

            # effect remains null
            self.lifted_stmt = LoopIR.Alloc(s.name, new_typ, s.mem, None, s.srcinfo)
            self.access_idxs = idxs
            self.alloc_type = new_typ

            # erase the statement from this location
            return []

        elif isinstance(s, (LoopIR.If, LoopIR.Seq)):
            # handle recursive part of pass at this statement
            self.ctrl_ctxt.append(s)
            stmts = super().map_s(sc)
            self.ctrl_ctxt.pop()

            # splice in lifted statement at the point to lift-to
            if s is self.lift_site:
                stmts = [self.lifted_stmt] + (stmts or s)

            return stmts

        elif isinstance(s, (LoopIR.Assign, LoopIR.Reduce)):
            # in this case, we may need to substitute the
            # buffer name on the lhs of the assignment/reduction
            if s.name is self.alloc_sym:
                assert self.access_idxs is not None
                idx = self.idx_mode(
                    [LoopIR.Read(i, [], T.index, s.srcinfo) for i in self.access_idxs],
                    s.idx,
                )
                rhs = self.apply_e(s.rhs)
                # return allocation or reduction...
                return s.update(idx=idx, rhs=rhs)

        elif isinstance(s, LoopIR.Call):
            # substitution in call arguments currently unsupported;
            # so setting flag here
            self._in_call_arg = True
            stmts = super().map_s(sc)
            self._in_call_arg = False
            return stmts

        # fall-through
        return super().map_s(sc)

    def map_e(self, e):
        if isinstance(e, LoopIR.Read) and e.name == self.alloc_sym:
            assert self.access_idxs is not None
            if not self.access_idxs:
                return None

            # if self._in_call_arg:
            if e.type.is_real_scalar():
                idx = self.idx_mode(
                    [LoopIR.Read(i, [], T.index, e.srcinfo) for i in self.access_idxs],
                    e.idx,
                )
                return LoopIR.Read(e.name, idx, e.type, e.srcinfo)
            else:
                assert self._in_call_arg
                assert len(e.idx) == 0
                # then we need to replace this read with a
                # windowing expression
                access = [
                    LoopIR.Point(LoopIR.Read(i, [], T.index, e.srcinfo), e.srcinfo)
                    for i in self.access_idxs
                ]
                orig = [
                    LoopIR.Interval(LoopIR.Const(0, T.int, e.srcinfo), hi, e.srcinfo)
                    for hi in e.type.shape()
                ]
                idx = self.idx_mode(access, orig)
                tensor_type = (
                    e.type.as_tensor if isinstance(e.type, T.Window) else e.type
                )
                win_typ = T.Window(self.alloc_type, tensor_type, e.name, idx)
                return LoopIR.WindowExpr(e.name, idx, win_typ, e.srcinfo)

        if isinstance(e, LoopIR.WindowExpr) and e.name == self.alloc_sym:
            assert self.access_idxs is not None
            if not self.access_idxs:
                return None
            # otherwise, extend windowing with accesses...

            idx = self.idx_mode(
                [
                    LoopIR.Point(LoopIR.Read(i, [], T.index, e.srcinfo), e.srcinfo)
                    for i in self.access_idxs
                ],
                e.idx,
            )
            win_typ = T.Window(self.alloc_type, e.type.as_tensor, e.name, idx)
            return LoopIR.WindowExpr(e.name, idx, win_typ, e.srcinfo)

        # fall-through
        return super().map_e(e)

    def get_ctxt_itrs_and_rngs(self, n_up):
        rngs = []
        idxs = []
        for s in self.ctrl_ctxt[-n_up:]:
            if isinstance(s, LoopIR.If):
                # if-statements do not affect allocations
                # note that this may miss opportunities to
                # shrink the allocation by being aware of
                # guards; oh well.
                continue
            elif isinstance(s, LoopIR.Seq):
                # TODO: may need to fix to support lo for backwards compatability
                if s.iter in self.alloc_deps and self.keep_dims:
                    idxs.append(s.iter)
                    if isinstance(s.hi, LoopIR.Read):
                        assert s.hi.type.is_indexable()
                        assert len(s.hi.idx) == 0
                    elif isinstance(s.hi, LoopIR.Const):
                        assert s.hi.type == T.int
                    elif isinstance(s.hi, LoopIR.BinOp):
                        assert s.hi.type.is_indexable()
                    else:
                        assert False, "bad case"

                    if self.lift_size is not None:
                        assert isinstance(self.lift_size, int)
                        # TODO: More robust checking of self.lift_size >= s.hi
                        if isinstance(s.hi, LoopIR.Const):
                            if s.hi.val > self.lift_size:
                                raise SchedulingError(
                                    f"Lift size cannot "
                                    f"be less than for-loop bound {s.hi.val}"
                                )
                        elif isinstance(s.hi, LoopIR.BinOp) and s.hi.op == "%":
                            assert isinstance(s.hi.rhs, LoopIR.Const)
                            if s.hi.rhs.val > self.lift_size:
                                raise SchedulingError(
                                    f"Lift size cannot "
                                    f"be less than for-loop bound {s.hi}"
                                )
                        else:
                            raise NotImplementedError

                        rngs.append(LoopIR.Const(self.lift_size, T.int, s.srcinfo))
                    else:
                        rngs.append(s.hi)
            else:
                assert False, "bad case"

        return idxs, rngs


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Fissioning at a Statement scheduling directive


def check_used(variables, eff):
    for e in eff:
        if e in variables:
            return True
    return False


class _Is_Alloc_Free(LoopIR_Do):
    def __init__(self, pre, post):
        self._is_alloc_free = True
        self._alloc_var = []

        self.do_stmts(pre)

        # make sure all of _alloc_vars are not used in any of the
        # post statement
        for s in post:
            if isinstance(s, LoopIR.Reduce):  # Allow reduce
                continue

            reads = [a for a, _ in get_reads_of_stmts([s])]
            writes = [a for a, _ in get_writes_of_stmts([s])]

            if check_used(self._alloc_var, reads):
                self._is_alloc_free = False
                break
            if check_used(self._alloc_var, writes):
                self._is_alloc_free = False
                break

    def result(self):
        return self._is_alloc_free

    def do_s(self, s):
        if isinstance(s, LoopIR.Alloc):
            self._alloc_var.append(s.name)

        super().do_s(s)


def _is_alloc_free(pre, post):
    return _Is_Alloc_Free(pre, post).result()


# which variable symbols are free
class _FreeVars(LoopIR_Do):
    def __init__(self, stmts):
        self._fvs = set()
        self._bound = set()

        if isinstance(stmts, LoopIR.expr):
            self.do_e(stmts)
        else:
            self.do_stmts(stmts)

    def result(self):
        return self._fvs

    def do_s(self, s):
        if isinstance(s, (LoopIR.Assign, LoopIR.Reduce)):
            if s.name not in self._bound:
                self._fvs.add(s.name)
        elif isinstance(s, LoopIR.Seq):
            self._bound.add(s.iter)
        elif isinstance(s, LoopIR.Alloc):
            self._bound.add(s.name)

        super().do_s(s)

    def do_e(self, e):
        if isinstance(e, LoopIR.Read):
            if e.name not in self._bound:
                self._fvs.add(e.name)

        super().do_e(e)


def _FV(stmts):
    return _FreeVars(stmts).result()


def _is_idempotent(stmts):
    def _stmt(s):
        styp = type(s)
        if styp is LoopIR.Reduce:
            return False
        elif styp is LoopIR.Call:
            return _is_idempotent(s.f.body)
        elif styp is LoopIR.If:
            return _is_idempotent(s.body) and _is_idempotent(s.orelse)
        elif styp is LoopIR.Seq:
            return _is_idempotent(s.body)
        else:
            return True

    return all(_stmt(s) for s in stmts)


def DoRemoveLoop(loop):
    s = loop._node

    # Check if we can remove the loop. Conditions are:
    # 1. Body does not depend on the loop iteration variable
    if s.iter in _FV(s.body):
        raise SchedulingError(
            f"Cannot remove loop, {s.iter} is not " "free in the loop body."
        )

    # 2. Body is idempotent
    Check_IsIdempotent(loop.get_root(), [s])

    # 3. The loop runs at least once;
    #    If not, then place a guard around the statement
    ir, fwd = loop.get_root(), lambda x: x
    try:
        Check_IsPositiveExpr(loop.get_root(), [s], s.hi)
    except SchedulingError:
        cond = LoopIR.BinOp(">", s.hi, s.lo, T.bool, s.srcinfo)

        def wrapper(body):
            return LoopIR.If(cond, body, [], None, s.srcinfo)

        ir, fwd = loop.body()._wrap(wrapper, "body")

    ir, fwd_move = fwd(loop).body()._move(fwd(loop).after())
    fwd = _compose(fwd_move, fwd)
    ir, fwd_del = fwd(loop)._delete()
    fwd = _compose(fwd_del, fwd)

    return ir, fwd


# This is same as original FissionAfter, except that
# this does not remove loop. We have separate remove_loop
# operator for that purpose.
def DoFissionAfterSimple(stmt_cursor, n_lifts):
    tgt_stmt = stmt_cursor._node
    assert isinstance(tgt_stmt, LoopIR.stmt)
    assert is_pos_int(n_lifts)

    ir, fwd = stmt_cursor.get_root(), lambda x: x

    def alloc_check(pre, post):
        if not _is_alloc_free(pre, post):
            pre_allocs = {s.name for s in pre if isinstance(s, LoopIR.Alloc)}
            post_FV = _FV(post)
            for nm in pre_allocs:
                if nm in post_FV:
                    raise SchedulingError(
                        f"Will not fission here, because "
                        f"doing so will hide the allocation "
                        f"of {nm} from a later use site."
                    )

    cur_c = stmt_cursor
    while n_lifts > 0:
        n_lifts -= 1

        idx = cur_c.get_index() + 1
        par_c = cur_c.parent()
        par_s = par_c._node

        if isinstance(par_s, LoopIR.Seq):
            pre_c = par_c.body()[:idx]
            post_c = par_c.body()[idx:]
        elif isinstance(par_s, LoopIR.If):
            if cur_c._node in par_s.body:
                pre_c = par_c.body()[:idx]
                post_c = par_c.body()[idx:]
            else:
                pre_c = par_c.orelse()[:idx]
                post_c = par_c.orelse()[idx:]
        else:
            raise SchedulingError("Can only lift past a for loop or an if statement")

        pre = [s._node for s in pre_c]
        post = [s._node for s in post_c]

        if not (pre and post):
            continue

        alloc_check(pre, post)

        if isinstance(par_s, LoopIR.Seq):
            # we must check whether the two parts of the
            # fission can commute appropriately
            no_loop_var_pre = par_s.iter not in _FV(pre)
            Check_FissionLoop(ir, par_s, pre, post, no_loop_var_pre)

            # we can skip the loop iteration if the
            # body doesn't depend on the loop
            # and the body is idempotent

            def wrapper(body):
                return par_s.update(body=body)

            ir, fwd_wrap = post_c._wrap(wrapper, "body")
            fwd = _compose(fwd_wrap, fwd)

            post_c = fwd_wrap(par_c).body()[-1]
            ir, fwd_move = post_c._move(fwd_wrap(par_c).after())
            fwd = _compose(fwd_move, fwd)

            cur_c = fwd_move(fwd_wrap(par_c))
        elif isinstance(par_s, LoopIR.If):
            if cur_c._node in par_s.body:

                def wrapper(body):
                    return par_s.update(body=body)

                ir, fwd_wrap = pre_c._wrap(wrapper, "body")
                fwd = _compose(fwd_wrap, fwd)

                pre_c = fwd_wrap(par_c).body()[0]
                ir, fwd_move = pre_c._move(fwd_wrap(par_c).before())
                fwd = _compose(fwd_move, fwd)

                cur_c = fwd_move(fwd_wrap(par_c)).prev()
            else:
                assert cur_c._node in par_s.orelse

                def wrapper(orelse):
                    return par_s.update(body=None, orelse=orelse)

                ir, fwd_wrap = post_c._wrap(wrapper, "orelse")
                fwd = _compose(fwd_wrap, fwd)

                post_c = fwd_wrap(par_c).orelse()[-1]
                ir, fwd_move = post_c._move(fwd_wrap(par_c).after())
                fwd = _compose(fwd_move, fwd)

                cur_c = fwd_move(fwd_wrap(par_c))

    return ir, fwd


# TODO: Deprecate this with the one above
# structure is weird enough to skip using the Rewrite-pass super-class
class DoFissionLoops:
    def __init__(self, proc, stmt_cursor, n_lifts):
        self.tgt_stmt = stmt_cursor._node
        assert isinstance(self.tgt_stmt, LoopIR.stmt)
        assert is_pos_int(n_lifts)
        self.provenance = proc
        self.orig_proc = proc._loopir_proc
        self.n_lifts = n_lifts

        self.hit_fission = False  # signal to map_stmts

        pre_body, post_body = self.map_stmts(self.orig_proc.body)
        self.proc = LoopIR.proc(
            name=self.orig_proc.name,
            args=self.orig_proc.args,
            preds=self.orig_proc.preds,
            body=pre_body + post_body,
            instr=None,
            eff=self.orig_proc.eff,
            srcinfo=self.orig_proc.srcinfo,
        )

    def result(self):
        return api.Procedure(self.proc, _provenance_eq_Procedure=self.provenance)

    def alloc_check(self, pre, post):
        if not _is_alloc_free(pre, post):
            raise SchedulingError(
                "Will not fission here, because "
                "an allocation might be buried "
                "in a different scope than some use-site"
            )

    # returns a pair of stmt-lists
    # for those statements occurring before and
    # after the fission point
    def map_stmts(self, stmts):
        pre_stmts = []
        post_stmts = []
        for orig_s in stmts:
            pre, post = self.map_s(orig_s)
            pre_stmts += pre
            post_stmts += post

        return pre_stmts, post_stmts

    # see map_stmts comment
    def map_s(self, s):
        if s is self.tgt_stmt:
            # assert self.hit_fission == False
            self.hit_fission = True
            # none-the-less make sure we return this statement in
            # the pre-fission position
            return [s], []

        elif isinstance(s, LoopIR.If):

            # first, check if we need to split the body
            pre, post = self.map_stmts(s.body)
            fission_body = len(pre) > 0 and len(post) > 0 and self.n_lifts > 0
            if fission_body:
                self.n_lifts -= 1
                self.alloc_check(pre, post)
                pre = LoopIR.If(s.cond, pre, [], None, s.srcinfo)
                post = LoopIR.If(s.cond, post, s.orelse, None, s.srcinfo)
                return [pre], [post]

            body = pre + post

            # if we don't, then check if we need to split the or-else
            pre, post = self.map_stmts(s.orelse)
            fission_orelse = len(pre) > 0 and len(post) > 0 and self.n_lifts > 0
            if fission_orelse:
                self.n_lifts -= 1
                self.alloc_check(pre, post)
                pre = LoopIR.If(s.cond, body, pre, None, s.srcinfo)
                post = LoopIR.If(
                    s.cond, [LoopIR.Pass(None, s.srcinfo)], post, None, s.srcinfo
                )
                return [pre], [post]

            orelse = pre + post

            # if we neither split the body nor the or-else,
            # then we need to gather together the pre and post.
            single_stmt = LoopIR.If(s.cond, body, orelse, None, s.srcinfo)

        # TODO: may need to fix to support lo for backwards compatability
        elif isinstance(s, LoopIR.Seq):
            # check if we need to split the loop
            pre, post = self.map_stmts(s.body)
            do_fission = len(pre) > 0 and len(post) > 0 and self.n_lifts > 0
            if do_fission:
                self.n_lifts -= 1
                self.alloc_check(pre, post)

                # we can skip the loop iteration if the
                # body doesn't depend on the loop
                # and the body is idempotent
                if s.iter in _FV(pre) or not _is_idempotent(pre):
                    pre = [s.update(body=pre)]
                    # since we are copying the binding of s.iter,
                    # we should perform an Alpha_Rename for safety
                    pre = Alpha_Rename(pre).result()
                if s.iter in _FV(post) or not _is_idempotent(post):
                    post = [s.update(body=post)]

                return pre, post

            # if we didn't split, then compose pre and post of the body
            single_stmt = s.update(body=pre + post)

        else:
            # all other statements cannot recursively
            # contain statements, so...
            single_stmt = s

        if self.hit_fission:
            return [], [single_stmt]
        else:
            return [single_stmt], []


class DoAddUnsafeGuard(Cursor_Rewrite):
    def __init__(self, proc_cursor, stmt_cursor, cond):
        self.stmt = stmt_cursor._node
        self.cond = cond
        self.in_loop = False

        super().__init__(proc_cursor)

    def map_s(self, sc):
        s = sc._node
        if s is self.stmt:
            # Check_ExprEqvInContext(self.orig_proc,
            #                       self.cond, [s],
            #                       LoopIR.Const(True, T.bool, s.srcinfo))
            s1 = Alpha_Rename([s]).result()
            return [LoopIR.If(self.cond, s1, [], None, s.srcinfo)]

        return super().map_s(sc)


def DoSpecialize(stmt_cursor, conds):
    assert conds, "Must add at least one condition"
    s = stmt_cursor._node

    def is_valid_condition(e):
        assert isinstance(e, LoopIR.BinOp)
        if e.op in ["and", "or"]:
            return is_valid_condition(e.lhs) and is_valid_condition(e.rhs)
        elif e.op in ["==", "!=", "<", "<=", ">", ">="]:
            return e.lhs.type.is_indexable() and e.rhs.type.is_indexable()
        else:
            return False

    else_br = Alpha_Rename([s]).result()
    for cond in reversed(conds):
        if not is_valid_condition(cond):
            raise SchedulingError("Invalid specialization condition. ")

        then_br = Alpha_Rename([s]).result()
        else_br = [LoopIR.If(cond, then_br, else_br, None, s.srcinfo)]

    ir, fwd = stmt_cursor._replace(else_br)
    return ir, fwd


def _get_constant_bound(e):
    if isinstance(e, LoopIR.BinOp) and e.op == "%":
        return e.rhs
    raise SchedulingError(f"Could not derive constant bound on {e}")


class DoBoundAndGuard(Cursor_Rewrite):
    def __init__(self, proc_cursor, loop_cursor):
        self.loop = loop_cursor._node
        super().__init__(proc_cursor)

    def map_s(self, sc):
        s = sc._node
        if s == self.loop:
            assert isinstance(s, LoopIR.Seq)
            bound = _get_constant_bound(s.hi)
            guard = LoopIR.If(
                LoopIR.BinOp(
                    "<",
                    LoopIR.Read(s.iter, [], T.index, s.srcinfo),
                    s.hi,
                    T.bool,
                    s.srcinfo,
                ),
                s.body,
                [],
                None,
                s.srcinfo,
            )
            return [s.update(hi=bound, body=[guard])]

        return super().map_s(sc)


def DoFuseLoop(f_cursor, s_cursor, unsafe_disable_check=False):
    proc = f_cursor.get_root()

    if f_cursor.next() != s_cursor:
        raise SchedulingError(
            "expected the two loops to be fused to come one right after the other"
        )

    # check if the loop bounds are equivalent
    loop1 = f_cursor._node
    loop2 = s_cursor._node
    Check_ExprEqvInContext(proc, loop1.hi, [loop1], loop2.hi, [loop2])

    def mk_read(e):
        return LoopIR.Read(loop1.iter, [], T.index, loop1.srcinfo)

    ir, fwd = proc, lambda x: x
    ir, fwd = _replace_pats(
        ir, fwd, s_cursor, f"{loop2.iter}", mk_read, only_replace_attrs=False
    )
    ir, fwd_move = fwd(s_cursor).body()._move(fwd(f_cursor).body()[-1].after())
    fwd = _compose(fwd_move, fwd)
    ir, fwdDel = fwd(s_cursor)._delete()
    fwd = _compose(fwdDel, fwd)

    if not unsafe_disable_check:
        x = LoopIR.Read(loop1.iter, [], T.index, loop1.srcinfo)
        y = loop2.iter
        body1 = loop1.body
        body2 = SubstArgs(loop2.body, {y: x}).result()
        loop = fwd(f_cursor)._node
        Check_FissionLoop(ir, loop, body1, body2)

    return ir, fwd


def DoFuseIf(f_cursor, s_cursor):
    proc = f_cursor.get_root()
    if f_cursor.next() != s_cursor:
        raise SchedulingError(
            "expected the two if statements to be fused to come one right after the other"
        )

    if1 = f_cursor._node
    if2 = s_cursor._node
    Check_ExprEqvInContext(proc, if1.cond, [if1], if2.cond, [if2])

    cond = if1.cond
    body1 = if1.body
    body2 = if2.body
    orelse1 = if1.orelse
    orelse2 = if2.orelse
    ifstmt = LoopIR.If(cond, body1 + body2, orelse1 + orelse2, None, if1.srcinfo)

    ir, fwd = s_cursor.body()._move(f_cursor.body()[-1].after())
    if f_cursor.orelse():
        ir, fwd_move = fwd(s_cursor).orelse()._move(fwd(f_cursor).orelse()[-1].after())
        fwd = _compose(fwd_move, fwd)
    else:
        ir, fwd_repl = fwd(f_cursor).orelse()._replace(orelse1 + orelse2)
        fwd = _compose(fwd_repl, fwd)
    ir, fwd_del = fwd(s_cursor)._delete()
    fwd = _compose(fwd_del, fwd)
    return ir, fwd


def DoAddLoop(stmt_cursor, var, hi, guard, unsafe_disable_check):
    proc = stmt_cursor.get_root()
    s = stmt_cursor._node

    if not unsafe_disable_check:
        Check_IsIdempotent(proc, [s])
        Check_IsPositiveExpr(proc, [s], hi)

    sym = Sym(var)

    def wrapper(body):
        if guard:
            rdsym = LoopIR.Read(sym, [], T.index, s.srcinfo)
            zero = LoopIR.Const(0, T.int, s.srcinfo)
            cond = LoopIR.BinOp("==", rdsym, zero, T.bool, s.srcinfo)
            body = [LoopIR.If(cond, body, [], None, s.srcinfo)]

        return LoopIR.Seq(
            sym, LoopIR.Const(0, T.index, s.srcinfo), hi, body, None, s.srcinfo
        )

    ir, fwd = stmt_cursor.as_block()._wrap(wrapper, "body")
    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#   Factor out a sub-statement as a Procedure scheduling directive


def _make_closure(name, stmts, var_types, order):
    FVs = list(sorted(_FV(stmts)))
    info = stmts[0].srcinfo

    # work out the calling arguments (args) and sub-proc args (fnargs)
    args = []
    fnargs = []

    # first, scan over all the arguments and convert them.
    # accumulate all size symbols separately
    sizes = set()
    for v in FVs:
        typ = var_types[v]
        if typ is T.size:
            sizes.add(v)
        elif typ is T.index:
            args.append(LoopIR.Read(v, [], typ, info))
            fnargs.append(LoopIR.fnarg(v, typ, None, info))
        else:
            # add sizes (that this arg depends on) to the signature
            for sz in typ.shape():
                if isinstance(sz, Sym):
                    sizes.add(sz)
            args.append(LoopIR.Read(v, [], typ, info))
            fnargs.append(LoopIR.fnarg(v, typ, None, info))

    # now prepend all sizes to the argument list
    sizes = list(sorted(sizes))
    args = [LoopIR.Read(sz, [], T.size, info) for sz in sizes] + args
    fnargs = [LoopIR.fnarg(sz, T.size, None, info) for sz in sizes] + fnargs

    def shuffle(arg_list):
        if sorted(order.values()) != [i for i in range(0, len(arg_list))]:
            raise SchedulingError(f"expected to provide full ordering of arguments")

        new_args = [0 for a in arg_list]
        for key in order:
            for i in range(len(arg_list)):
                if arg_list[i].name.name() == key:
                    new_args[order[key]] = arg_list[i]

        return new_args

    if order:
        args = shuffle(args)
        fnargs = shuffle(fnargs)

    eff = None
    # TODO: raise NotImplementedError("need to figure out effect of new closure")
    closure = LoopIR.proc(name, fnargs, [], stmts, None, eff, info)

    return closure, args


def DoInsertPass(gap):
    srcinfo = gap.parent()._node.srcinfo
    ir, fwd = gap._insert([LoopIR.Pass(None, srcinfo=srcinfo)])
    return ir, fwd


def DoDeleteConfig(proc_cursor, config_cursor):
    eq_mod_config = Check_DeleteConfigWrite(proc_cursor._node, [config_cursor._node])
    p, fwd = config_cursor._delete()
    return p, fwd, eq_mod_config


class DoDeletePass(Cursor_Rewrite):
    def __init__(self, proc_cursor):
        super().__init__(proc_cursor)

    def map_s(self, sc):
        s = sc._node
        if isinstance(s, LoopIR.Pass):
            return []

        elif isinstance(s, LoopIR.Seq):
            body = self.map_stmts(sc.body())
            if body is None:
                return None
            elif not body:
                return []
            else:
                return [s.update(body=body)]

        return super().map_s(sc)


class DoExtractMethod(Cursor_Rewrite):
    def __init__(self, proc, name, stmt_cursor, order):
        self.match_stmt = stmt_cursor._node
        assert isinstance(self.match_stmt, LoopIR.stmt)
        self.sub_proc_name = name
        self.new_subproc = None
        self.orig_proc = proc._loopir_proc
        self.order = order

        self.var_types = ChainMap()

        for a in self.orig_proc.args:
            self.var_types[a.name] = a.type

        super().__init__(proc)
        Check_Aliasing(self.proc)

    def subproc(self):
        return api.Procedure(self.new_subproc)

    def push(self):
        self.var_types = self.var_types.new_child()

    def pop(self):
        self.var_types = self.var_types.parents

    def map_s(self, sc):
        s = sc._node
        if s is self.match_stmt:
            subproc, args = _make_closure(
                self.sub_proc_name, [s], self.var_types, self.order
            )
            self.new_subproc = subproc
            return [LoopIR.Call(subproc, args, None, s.srcinfo)]
        elif isinstance(s, LoopIR.Alloc):
            self.var_types[s.name] = s.type
            return None
        elif isinstance(s, LoopIR.Seq):
            self.push()
            self.var_types[s.iter] = T.index
            body = self.map_stmts(sc.body())
            self.pop()

            if body:
                return [s.update(body=body)]

            return None
        elif isinstance(s, LoopIR.If):
            self.push()
            body = self.map_stmts(sc.body())
            self.pop()
            self.push()
            orelse = self.map_stmts(sc.orelse())
            self.pop()

            if body or orelse:
                return [s.update(body=body or s.body, orelse=orelse or s.orlse)]

            return None

        return super().map_s(sc)

    def map_e(self, e):
        return None


class _DoNormalize(Cursor_Rewrite):
    # This class operates on an idea of creating a coefficient map for each
    # indexing expression (normalize_e), and writing the map back to LoopIR
    # (get_loopir in index_start).
    # For example, when you have Assign statement:
    # y[n*4 - n*4 + 1] = 0.0
    # index_start will be called with e : n*4 - n*4 + 1.
    # Then, normalize_e will create a map of symbols and its coefficients.
    # The map for the expression `n*4 + 1` is:
    # { temporary_constant_symbol : 1, n : 4 }
    # and the map for the expression `n*4 - n*4 + 1` is:
    # { temporary_constant_symbol : 1, n : 0 }
    # This map concatenation is handled by concat_map function.
    def __init__(self, proc):
        self.C = Sym("temporary_constant_symbol")
        self.env = ChainMap()
        # TODO: dispatch to Z3 to reason about preds ranges
        for arg in proc._loopir_proc.args:
            self.env[arg.name] = None

        self.ir = proc._loopir_proc
        self.fwd = lambda x: x

        super().__init__(proc)

        # need to update self.ir with pred changes
        new_preds = self.map_exprs(self.ir.preds)
        if new_preds:
            self.ir, fwd = (
                ic.Cursor.create(self.ir)._child_block("preds")._replace(new_preds)
            )
            self.fwd = _compose(fwd, self.fwd)

    def result(self, **kwargs):
        return api.Procedure(
            self.ir, _provenance_eq_Procedure=self.provenance, _forward=self.fwd
        )

    def concat_map(self, op, lhs, rhs):
        if op == "+":
            # if has same key: add value
            common = {key: (lhs[key] + rhs[key]) for key in lhs if key in rhs}
            return lhs | rhs | common
        elif op == "-":
            # has same key: sub value
            common = {key: (lhs[key] - rhs[key]) for key in lhs if key in rhs}
            # else, negate the rhs and cat map
            neg_rhs = {key: -rhs[key] for key in rhs}
            return lhs | neg_rhs | common
        elif op == "*":
            # rhs or lhs NEEDS to be constant
            assert len(rhs) == 1 or len(lhs) == 1
            # multiply the other one's value by that constant
            if len(rhs) == 1 and self.C in rhs:
                return {key: lhs[key] * rhs[self.C] for key in lhs}
            else:
                assert len(lhs) == 1 and self.C in lhs
                return {key: rhs[key] * lhs[self.C] for key in rhs}
        else:
            assert False, "bad case"

    def normalize_e(self, e):
        assert e.type.is_indexable(), f"{e} is not indexable!"

        if isinstance(e, LoopIR.Read):
            assert len(e.idx) == 0, "Indexing inside indexing does not make any sense"
            return {e.name: 1}
        elif isinstance(e, LoopIR.Const):
            return {self.C: e.val}
        elif isinstance(e, LoopIR.USub):
            e_map = self.normalize_e(e.arg)
            return {key: -e_map[key] for key in e_map}
        elif isinstance(e, LoopIR.BinOp):
            lhs_map = self.normalize_e(e.lhs)
            rhs_map = self.normalize_e(e.rhs)
            return self.concat_map(e.op, lhs_map, rhs_map)
        else:
            assert False, (
                "index_start should only be called by"
                + f" an indexing expression. e was {e}"
            )

    @staticmethod
    def has_div_mod_config(e):
        if isinstance(e, LoopIR.Read):
            return False
        elif isinstance(e, LoopIR.Const):
            return False
        elif isinstance(e, LoopIR.USub):
            return _DoNormalize.has_div_mod_config(e.arg)
        elif isinstance(e, LoopIR.BinOp):
            if e.op == "/" or e.op == "%":
                return True
            else:
                lhs = _DoNormalize.has_div_mod_config(e.lhs)
                rhs = _DoNormalize.has_div_mod_config(e.rhs)
                return lhs or rhs
        elif isinstance(e, LoopIR.ReadConfig):
            return True
        else:
            assert False, "bad case"

    # Call this when e is one indexing expression
    # e should be an indexing expression
    def index_start(self, e):
        def get_normalized_expr(e):
            # Make a map of symbols and coefficients
            n_map = self.normalize_e(e)

            new_e = LoopIR.Const(n_map.get(self.C, 0), T.int, e.srcinfo)

            delete_zero = [
                (n_map[v], v) for v in n_map if v != self.C and n_map[v] != 0
            ]

            return new_e, delete_zero

        def division_simplification(e):
            constant, normalization_list = get_normalized_expr(e.lhs)

            d = e.rhs.val

            non_divisible_terms = [
                (coeff, v) for coeff, v in normalization_list if coeff % d != 0
            ]

            if len(non_divisible_terms) == 0:
                normalization_list = [
                    (coeff // d, v) for coeff, v in normalization_list
                ]
                return generate_loopIR(
                    e.lhs, constant.update(val=constant.val // d), normalization_list
                )
            elif constant.val % d == 0:
                non_divisible_expr = generate_loopIR(
                    e.lhs, constant.update(val=0), non_divisible_terms
                )
                non_divisible_expr_range = IndexRangeAnalysis(
                    non_divisible_expr, self.env
                ).result()

                if (
                    non_divisible_expr_range is not None
                    and 0 <= non_divisible_expr_range[0]
                    and non_divisible_expr_range[1] < d
                ):
                    divisible_terms = [
                        (coeff // d, v)
                        for coeff, v in normalization_list
                        if coeff % d == 0
                    ]
                    return generate_loopIR(
                        e.lhs, constant.update(val=constant.val // d), divisible_terms
                    )
            else:
                non_divisible_expr = generate_loopIR(
                    e.lhs, constant, non_divisible_terms
                )
                non_divisible_expr_range = IndexRangeAnalysis(
                    non_divisible_expr, self.env
                ).result()

                if (
                    non_divisible_expr_range is not None
                    and 0 <= non_divisible_expr_range[0]
                    and non_divisible_expr_range[1] < d
                ):
                    divisible_terms = [
                        (coeff // d, v)
                        for coeff, v in normalization_list
                        if coeff % d == 0
                    ]
                    return generate_loopIR(
                        e.lhs, constant.update(val=0), divisible_terms
                    )

            new_lhs = generate_loopIR(e.lhs, constant, normalization_list)
            return LoopIR.BinOp("/", new_lhs, e.rhs, e.type, e.srcinfo)

        def division_denominator_simplification(e):
            assert e.op == "/"

            def has_nested_const_denominator(expr):
                # (n / c1) / c2
                if expr.op == "/" and isinstance(
                    expr.rhs, LoopIR.Const
                ):  # (something / c2)
                    if (
                        isinstance(expr.lhs, LoopIR.BinOp) and expr.lhs.op == "/"
                    ):  # (n / c1)
                        if isinstance(expr.lhs.rhs, LoopIR.Const):
                            return True

                return False

            new_e = e
            # call division_denominator_simplification recursively
            while has_nested_const_denominator(new_e):
                new_e = new_e.update(
                    lhs=new_e.lhs.lhs,
                    rhs=LoopIR.Const(
                        new_e.lhs.rhs.val * new_e.rhs.val,
                        new_e.lhs.type,
                        new_e.lhs.srcinfo,
                    ),
                )

            return new_e

        def division_simplification_and_try_spliting_denominator(e):
            def still_division(e):
                return isinstance(e, LoopIR.BinOp) and e.op == "/"

            e = division_simplification(e)

            if not still_division(e):
                return e

            d = e.rhs.val
            lhs = e.lhs

            divisor = 2
            while divisor * divisor <= d:
                if d % divisor == 0:
                    new_e = LoopIR.BinOp(
                        "/", lhs, e.rhs.update(val=divisor), e.type, e.srcinfo
                    )
                    new_e = division_simplification(new_e)
                    if not still_division(new_e):
                        return LoopIR.BinOp(
                            "/",
                            new_e,
                            e.rhs.update(val=d // divisor),
                            e.type,
                            e.srcinfo,
                        )
                    new_e = LoopIR.BinOp(
                        "/", lhs, e.rhs.update(val=d // divisor), e.type, e.srcinfo
                    )
                    new_e = division_simplification(new_e)
                    if not still_division(new_e):
                        return LoopIR.BinOp(
                            "/", new_e, e.rhs.update(val=divisor), e.type, e.srcinfo
                        )
                divisor += 1

            return e

        def modulo_simplification(e):
            constant, normalization_list = get_normalized_expr(e.lhs)

            m = e.rhs.val

            normalization_list = [
                (coeff, v) for coeff, v in normalization_list if coeff % m != 0
            ]

            if len(normalization_list) == 0:
                return constant.update(val=constant.val % m)

            if constant.val % m == 0:
                constant = constant.update(val=0)

            new_lhs = generate_loopIR(e.lhs, constant, normalization_list)
            new_lhs_range = IndexRangeAnalysis(new_lhs, self.env).result()
            if new_lhs_range is not None and new_lhs_range[1] < m:
                assert new_lhs_range[0] >= 0
                return new_lhs

            return LoopIR.BinOp("%", new_lhs, e.rhs, e.type, e.srcinfo)

        def generate_loopIR(e_context, constant, normalization_list):
            def scale_read(coeff, key):
                return LoopIR.BinOp(
                    "*",
                    LoopIR.Const(coeff, T.int, e_context.srcinfo),
                    LoopIR.Read(key, [], e_context.type, e_context.srcinfo),
                    e_context.type,
                    e_context.srcinfo,
                )

            new_e = constant
            for coeff, v in sorted(normalization_list):
                if coeff > 0:
                    new_e = LoopIR.BinOp(
                        "+",
                        new_e,
                        scale_read(coeff, v),
                        e_context.type,
                        e_context.srcinfo,
                    )
                else:
                    new_e = LoopIR.BinOp(
                        "-",
                        new_e,
                        scale_read(-coeff, v),
                        e_context.type,
                        e_context.srcinfo,
                    )
            return new_e

        assert isinstance(e, LoopIR.expr)

        if isinstance(e, LoopIR.BinOp):
            new_lhs = self.index_start(e.lhs)
            new_rhs = self.index_start(e.rhs)
            e = e.update(lhs=new_lhs, rhs=new_rhs)

        if isinstance(e, LoopIR.BinOp) and e.op in ("/", "%"):
            assert isinstance(e.rhs, LoopIR.Const)
            if self.has_div_mod_config(e.lhs):
                if e.op == "/":
                    return division_denominator_simplification(e)
                else:
                    return e

            if e.op == "/":
                return division_simplification_and_try_spliting_denominator(e)

            return modulo_simplification(e)

        # Div and mod special cases are handleded before, if that didn't succeed we cannot normalize
        # Skip ReadConfigs, they need careful handling because they're not Sym.
        if self.has_div_mod_config(e):
            return e

        constant, normalization_list = get_normalized_expr(e)
        return generate_loopIR(e, constant, normalization_list)

    def map_e(self, e):
        if e.type.is_indexable():
            return self.index_start(e)

        return super().map_e(e)

    def map_s(self, sc):
        s = sc._node
        if isinstance(s, LoopIR.If):
            new_cond = self.map_e(s.cond)

            self.env = self.env.new_child()
            self.map_stmts(sc.body())
            self.env = self.env.parents

            self.env = self.env.new_child()
            self.map_stmts(sc.orelse())
            self.env = self.env.parents

            if new_cond:
                self.ir, fwd_repl = self.fwd(sc)._child_node("cond")._replace(new_cond)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Seq):
            new_lo = self.map_e(s.lo)
            new_hi = self.map_e(s.hi)

            self.env = self.env.new_child()
            lo_range = IndexRangeAnalysis(new_lo, self.env).result()
            hi_range = IndexRangeAnalysis(new_hi, self.env).result()

            if lo_range is not None and hi_range is not None:
                assert lo_range[0] <= hi_range[1]
                if lo_range[0] == hi_range[1]:
                    self.env[s.iter] = None
                else:
                    self.env[s.iter] = (lo_range[0], hi_range[1] - 1)
            else:
                self.env[s.iter] = None

            self.map_stmts(sc.body())
            if new_lo:
                self.ir, fwd_repl = self.fwd(sc)._child_node("lo")._replace(new_lo)
                self.fwd = _compose(fwd_repl, self.fwd)
            if new_hi:
                self.ir, fwd_repl = self.fwd(sc)._child_node("hi")._replace(new_hi)
                self.fwd = _compose(fwd_repl, self.fwd)

            self.env = self.env.parents

        elif isinstance(s, (LoopIR.Assign, LoopIR.Reduce)):
            new_type = self.map_t(s.type)
            new_idx = self.map_exprs(s.idx)
            new_rhs = self.map_e(s.rhs)
            if new_type:
                self.ir, fwd_repl = self.fwd(sc)._child_node("type")._replace(new_type)
                self.fwd = _compose(fwd_repl, self.fwd)
            if new_idx:
                self.ir, fwd_repl = self.fwd(sc)._child_block("idx")._replace(new_idx)
                self.fwd = _compose(fwd_repl, self.fwd)
            if new_rhs:
                self.ir, fwd_repl = self.fwd(sc)._child_node("rhs")._replace(new_rhs)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, (LoopIR.WriteConfig, LoopIR.WindowStmt)):
            new_rhs = self.map_e(s.rhs)
            if new_rhs:
                self.ir, fwd_repl = sc._child_node("rhs")._replace(new_rhs)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Call):
            new_args = self.map_exprs(s.args)
            if new_args:
                self.ir, fwd_repl = self.fwd(sc)._child_block("args")._replace(new_args)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Alloc):
            new_type = self.map_t(s.type)
            if new_type:
                self.ir, fwd_repl = self.fwd(sc)._child_node("type")._replace(new_type)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Pass):
            pass
        else:
            raise NotImplementedError(f"bad case {type(s)}")

        return None


class DoSimplify(Cursor_Rewrite):
    def __init__(self, proc):
        proc = _DoNormalize(proc).result()

        self.facts = ChainMap()

        self.ir = proc._loopir_proc
        self.fwd = lambda x: x

        super().__init__(proc)

        # might need to update IR with predicate changes
        if new_preds := self.map_exprs(self.ir.preds):
            # TODO KQ: is this line covered? do we not need to forward here?
            self.ir = self.ir.update(preds=new_preds)

    def cfold(self, op, lhs, rhs):
        if op == "+":
            return lhs.val + rhs.val
        if op == "-":
            return lhs.val - rhs.val
        if op == "*":
            return lhs.val * rhs.val
        if op == "/":
            if lhs.type == T.f64 or lhs.type == T.f32 or lhs.type == T.f16:
                return lhs.val / rhs.val
            else:
                return lhs.val // rhs.val
        if op == "%":
            return lhs.val % rhs.val
        if op == "and":
            return lhs.val and rhs.val
        if op == "or":
            return lhs.val or rhs.val
        if op == "<":
            return lhs.val < rhs.val
        if op == ">":
            return lhs.val > rhs.val
        if op == "<=":
            return lhs.val <= rhs.val
        if op == ">=":
            return lhs.val >= rhs.val
        if op == "==":
            return lhs.val == rhs.val
        raise ValueError(f"Unknown operator ({op})")

    @staticmethod
    def is_quotient_remainder(e):
        """
        Checks if e is of the form (up to commutativity):
            N % K + K * (N / K)
        and returns N if so. Otherwise, returns None.
        """
        assert isinstance(e, LoopIR.BinOp)
        if e.op != "+":
            return None

        if isinstance(e.lhs, LoopIR.BinOp) and e.lhs.op == "%":
            assert isinstance(e.lhs.rhs, LoopIR.Const)
            num = e.lhs.lhs
            mod: LoopIR.Const = e.lhs.rhs
            rem = e.lhs
            quot = e.rhs
        elif isinstance(e.rhs, LoopIR.BinOp) and e.rhs.op == "%":
            assert isinstance(e.rhs.rhs, LoopIR.Const)
            num = e.rhs.lhs
            mod: LoopIR.Const = e.rhs.rhs
            rem = e.rhs
            quot = e.lhs
        else:
            return None

        # Validate form of remainder
        if not (
            isinstance(rem, LoopIR.BinOp)
            and rem.op == "%"
            and str(rem.lhs) == str(num)
            and str(rem.rhs) == str(mod)
        ):
            return None

        # Validate form of quotient
        if not (isinstance(quot, LoopIR.BinOp) and quot.op == "*"):
            return None

        def check_quot(const, div):
            if (
                isinstance(const, LoopIR.Const)
                and (isinstance(div, LoopIR.BinOp) and div.op == "/")
                and (str(const) == str(mod))
                and (str(div.lhs) == str(num))
                and (str(div.rhs) == str(mod))
            ):
                return num
            return None

        return check_quot(quot.lhs, quot.rhs) or check_quot(quot.rhs, quot.lhs)

    def map_binop(self, e: LoopIR.BinOp):
        lhs = self.map_e(e.lhs) or e.lhs
        rhs = self.map_e(e.rhs) or e.rhs

        if isinstance(lhs, LoopIR.Const) and isinstance(rhs, LoopIR.Const):
            return LoopIR.Const(self.cfold(e.op, lhs, rhs), lhs.type, lhs.srcinfo)

        if e.op == "+":
            if is_const_zero(lhs):
                return rhs
            if is_const_zero(rhs):
                return lhs
            if val := self.is_quotient_remainder(
                LoopIR.BinOp(e.op, lhs, rhs, lhs.type, lhs.srcinfo)
            ):
                return val
        elif e.op == "-":
            if is_const_zero(rhs):
                return lhs
            if isinstance(lhs, LoopIR.BinOp) and lhs.op == "+":
                if lhs.lhs == rhs:
                    return lhs.rhs
                if lhs.rhs == rhs:
                    return lhs.lhs
        elif e.op == "*":
            if is_const_zero(lhs) or is_const_zero(rhs):
                return LoopIR.Const(0, lhs.type, lhs.srcinfo)
            if isinstance(lhs, LoopIR.Const) and lhs.val == 1:
                return rhs
            if isinstance(rhs, LoopIR.Const) and rhs.val == 1:
                return lhs
        elif e.op == "/":
            if isinstance(rhs, LoopIR.Const) and rhs.val == 1:
                return lhs
        elif e.op == "%":
            if isinstance(rhs, LoopIR.Const) and rhs.val == 1:
                return LoopIR.Const(0, lhs.type, lhs.srcinfo)

        return LoopIR.BinOp(e.op, lhs, rhs, e.type, e.srcinfo)

    def map_e(self, e):
        # If we get a match, then replace it with the known constant right away.
        # No need to run further simplify steps on this node.
        if const := self.is_known_constant(e):
            return const

        if isinstance(e, LoopIR.BinOp):
            e = self.map_binop(e)
        else:
            e = super().map_e(e) or e

        # After simplifying, we might match a known constant, so check again.
        if const := self.is_known_constant(e):
            return const

        return e

    def add_fact(self, cond):
        if (
            isinstance(cond, LoopIR.BinOp)
            and cond.op == "=="
            and isinstance(cond.rhs, LoopIR.Const)
        ):
            expr = cond.lhs
            const = cond.rhs
        elif (
            isinstance(cond, LoopIR.BinOp)
            and cond.op == "=="
            and isinstance(cond.lhs, LoopIR.Const)
        ):
            expr = cond.rhs
            const = cond.lhs
        else:
            return

        self.facts[str(expr)] = const

        # if we know that X / M == 0 then we also know that X % M == X.
        if isinstance(expr, LoopIR.BinOp) and expr.op == "/" and const.val == 0:
            mod_expr = LoopIR.BinOp("%", expr.lhs, expr.rhs, expr.type, expr.srcinfo)
            self.facts[str(mod_expr)] = expr.lhs

    def is_known_constant(self, e):
        if self.facts:
            return self.facts.get(str(e))
        return None

    def result(self, mod_config=None):
        return api.Procedure(
            self.ir, _provenance_eq_Procedure=self.provenance, _forward=self.fwd
        )

    def map_s(self, sc):
        s = sc._node
        if isinstance(s, LoopIR.If):
            cond = self.map_e(s.cond)
            safe_cond = cond or s.cond

            # If constant true or false, then drop the branch
            if isinstance(safe_cond, LoopIR.Const):
                if safe_cond.val:
                    self.ir, fwd_move = self.fwd(sc).body()._move(self.fwd(sc).before())
                    self.fwd = _compose(fwd_move, self.fwd)
                    self.ir, fwd_del = self.fwd(sc)._delete()
                    self.fwd = _compose(fwd_del, self.fwd)
                    self.map_stmts(sc.body())
                    return
                else:
                    self.ir, fwd_move = (
                        self.fwd(sc).orelse()._move(self.fwd(sc).before())
                    )
                    self.fwd = _compose(fwd_move, self.fwd)
                    self.ir, fwd_del = self.fwd(sc)._delete()
                    self.fwd = _compose(fwd_del, self.fwd)
                    self.map_stmts(sc.orelse())
                    return

            # Try to use the condition while simplifying body
            self.facts = self.facts.new_child()
            self.add_fact(safe_cond)
            self.map_stmts(sc.body())
            self.facts = self.facts.parents

            # Try to use the negation while simplifying orelse
            self.facts = self.facts.new_child()
            # TODO: negate fact here
            self.map_stmts(sc.orelse())
            self.facts = self.facts.parents

            if cond:
                self.ir, fwd_repl = self.fwd(sc)._child_node("cond")._replace(cond)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Seq):
            lo = self.map_e(s.lo)
            hi = self.map_e(s.hi)

            # Delete the loop if it would not run at all
            if (
                isinstance(hi, LoopIR.Const)
                and isinstance(lo, LoopIR.Const)
                and hi.val == lo.val
            ):
                self.ir, fwd_del = self.fwd(sc)._delete()
                self.fwd = _compose(fwd_del, self.fwd)
                return

            # Delete the loop if it would have an empty body
            self.map_stmts(sc.body())
            if self.fwd(sc).body() == []:
                self.ir, fwd_del = self.fwd(sc)._delete()
                self.fwd = _compose(fwd_del, self.fwd)
                return

            if lo:
                self.ir, fwd_repl = self.fwd(sc)._child_node("lo")._replace(lo)
                self.fwd = _compose(fwd_repl, self.fwd)
            if hi:
                self.ir, fwd_repl = self.fwd(sc)._child_node("hi")._replace(hi)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, (LoopIR.Assign, LoopIR.Reduce)):
            new_type = self.map_t(s.type)
            new_idx = self.map_exprs(s.idx)
            new_rhs = self.map_e(s.rhs)
            if new_type:
                self.ir, fwd_repl = self.fwd(sc)._child_node("type")._replace(new_type)
                self.fwd = _compose(fwd_repl, self.fwd)
            if new_idx:
                self.ir, fwd_repl = self.fwd(sc)._child_block("idx")._replace(new_idx)
                self.fwd = _compose(fwd_repl, self.fwd)
            if new_rhs:
                self.ir, fwd_repl = self.fwd(sc)._child_node("rhs")._replace(new_rhs)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, (LoopIR.WriteConfig, LoopIR.WindowStmt)):
            new_rhs = self.map_e(s.rhs)
            if new_rhs:
                self.ir, fwd_repl = sc._child_node("rhs")._replace(new_rhs)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Call):
            new_args = self.map_exprs(s.args)
            if new_args:
                self.ir, fwd_repl = self.fwd(sc)._child_block("args")._replace(new_args)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Alloc):
            new_type = self.map_t(s.type)
            if new_type:
                self.ir, fwd_repl = self.fwd(sc)._child_node("type")._replace(new_type)
                self.fwd = _compose(fwd_repl, self.fwd)
        elif isinstance(s, LoopIR.Pass):
            return None
        else:
            raise NotImplementedError(f"bad case {type(s)}")


class DoAssertIf(Cursor_Rewrite):
    def __init__(self, proc_cursor, if_cursor, cond):
        self.if_stmt = if_cursor._node

        assert isinstance(self.if_stmt, LoopIR.If)
        assert isinstance(cond, bool)

        self.cond = cond

        super().__init__(proc_cursor)

    def map_s(self, sc):
        s = sc._node
        if s is self.if_stmt:
            # check if the condition matches the asserted constant
            cond_node = LoopIR.Const(self.cond, T.bool, s.srcinfo)
            Check_ExprEqvInContext(self.orig_proc._node, s.cond, [s], cond_node)
            # if so, then we can simplify away the guard
            if self.cond:
                body = self.map_stmts(sc.body())
                if body is None:
                    return [node._node for node in sc.body()]
                else:
                    return body
            else:
                orelse = self.map_stmts(sc.orelse())
                if orelse is None:
                    return [node._node for node in sc.orelse()]
                else:
                    return orelse
        elif isinstance(s, LoopIR.Seq):
            body = self.map_stmts(sc.body())
            if body is None:
                return None
            elif body == []:
                return []
            else:
                return [s.update(body=body)]

        return super().map_s(sc)


def DoDataReuse(buf_cursor, rep_cursor):
    assert isinstance(buf_cursor._node, LoopIR.Alloc)
    assert isinstance(rep_cursor._node, LoopIR.Alloc)
    assert buf_cursor._node.type == rep_cursor._node.type

    buf_name = buf_cursor._node.name
    buf_dims = len(buf_cursor._node.type.shape())
    rep_name = rep_cursor._node.name
    first_assn = True

    ir, fwd = rep_cursor._delete()

    def mk_read(c):
        return {"name": buf_name}

    def mk_write(c):
        nonlocal first_assn
        if first_assn:
            first_assn = False
            Check_IsDeadAfter(buf_cursor.get_root(), [c._node], buf_name, buf_dims)
        return {"name": buf_name}

    for c in get_rest_of_block(rep_cursor)[1:]:
        ir, fwd = _replace_pats(ir, fwd, c, f"{rep_name}[_]", mk_read)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{rep_name} = _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{rep_name} += _", mk_write)

    return ir, fwd


# TODO: This can probably be re-factored into a generic
# "Live Variables" analysis w.r.t. a context/stmt separation?
class _DoStageMem_FindBufData(LoopIR_Do):
    def __init__(self, proc, buf_name, stmt_start):
        self.buf_str = buf_name
        self.buf_sym = None
        self.buf_typ = None
        self.buf_mem = None
        self.stmt_start = stmt_start
        self.buf_map = ChainMap()
        self.orig_proc = proc

        for fa in self.orig_proc.args:
            if fa.type.is_numeric():
                self.buf_map[str(fa.name)] = (fa.name, fa.type, fa.mem)

        super().__init__(proc)

    def result(self):
        return self.buf_sym, self.buf_typ, self.buf_mem

    def push(self):
        self.buf_map = self.buf_map.new_child()

    def pop(self):
        self.buf_map = self.buf_map.parents

    def do_s(self, s):
        if s is self.stmt_start:
            if self.buf_str not in self.buf_map:
                raise SchedulingError(
                    f"no buffer or window "
                    f"named {self.buf_str} was live "
                    f"in the indicated statement block"
                )
            nm, typ, mem = self.buf_map[self.buf_str]
            self.buf_sym = nm
            self.buf_typ = typ
            self.buf_mem = mem

        if isinstance(s, LoopIR.Alloc):
            self.buf_map[str(s.name)] = (s.name, s.type, s.mem)
        if isinstance(s, LoopIR.WindowStmt):
            nm, typ, mem = self.buf_map[s.rhs.name]
            self.buf_map[str(s.name)] = (s.name, s.rhs.type, mem)
        elif isinstance(s, LoopIR.If):
            self.push()
            self.do_stmts(s.body)
            self.pop()
            self.push()
            self.do_stmts(s.orelse)
            self.pop()
        elif isinstance(s, LoopIR.Seq):
            self.push()
            self.do_stmts(s.body)
            self.pop()
        else:
            super().do_s(s)

    # short-circuit
    def do_e(self, e):
        pass


def DoStageMem(block_cursor, buf_name, w_exprs, new_name, use_accum_zero=False):
    proc = block_cursor.get_root()
    new_name = Sym(new_name)

    buf_name, buf_typ, mem = _DoStageMem_FindBufData(
        proc, buf_name, block_cursor[0]._node
    ).result()
    buf_typ = buf_typ if not isinstance(buf_typ, T.Window) else buf_typ.as_tensor

    if len(w_exprs) != len(buf_typ.shape()):
        raise SchedulingError(
            f"expected windowing of '{buf_name}' "
            f"to have {len(buf_typ.shape())} indices, "
            f"but only got {len(w_exprs)}"
        )

    shape = [
        LoopIR.BinOp("-", w[1], w[0], T.index, w[0].srcinfo)
        for w in w_exprs
        if isinstance(w, tuple)
    ]
    if all(isinstance(w, LoopIR.expr) for w in w_exprs):
        new_typ = buf_typ.basetype()
    else:
        new_typ = T.Tensor(shape, False, buf_typ.basetype())

    def rewrite_idx(idx):
        assert len(idx) == len(w_exprs)
        return [
            LoopIR.BinOp("-", i, w[0], T.index, i.srcinfo)
            for i, w in zip(idx, w_exprs)
            if isinstance(w, tuple)
        ]

    def rewrite_win(w_idx):
        assert len(w_idx) == len(w_exprs)

        def off_w(w, off):
            if isinstance(w, LoopIR.Interval):
                lo = LoopIR.BinOp("-", w.lo, off, T.index, w.srcinfo)
                hi = LoopIR.BinOp("-", w.hi, off, T.index, w.srcinfo)
                return LoopIR.Interval(lo, hi, w.srcinfo)
            else:
                assert isinstance(w, LoopIR.Point)
                pt = LoopIR.BinOp("-", w.pt, off, T.index, w.srcinfo)
                return LoopIR.Point(pt, w.srcinfo)

        return [off_w(w_i, w_e[0]) for w_i, w_e in zip(w_idx, w_exprs)]

    ir = block_cursor.get_root()
    block = [s._node for s in block_cursor]
    if use_accum_zero:
        n_dims = len(buf_typ.shape())
        Check_BufferReduceOnly(
            ir,
            block,
            buf_name,
            n_dims,
        )

    n_dims = len(buf_typ.shape())
    basetyp = new_typ.basetype() if isinstance(new_typ, T.Tensor) else new_typ
    srcinfo = block[0].srcinfo

    new_alloc = [LoopIR.Alloc(new_name, new_typ, mem, None, srcinfo)]
    ir, fwd = block_cursor[0].before()._insert(new_alloc)

    isR, isW = Check_BufferRW(ir, block, buf_name, n_dims)
    if isR:
        load_iter = [Sym(f"i{i}") for i, _ in enumerate(shape)]
        load_widx = [LoopIR.Read(s, [], T.index, srcinfo) for s in load_iter]
        if use_accum_zero:
            load_rhs = LoopIR.Const(0.0, basetyp, srcinfo)
        else:
            cp_load_widx = load_widx.copy()
            load_ridx = []
            for w in w_exprs:
                if isinstance(w, tuple):
                    load_ridx.append(
                        LoopIR.BinOp("+", cp_load_widx.pop(0), w[0], T.index, srcinfo)
                    )
                else:
                    load_ridx.append(w)
            load_rhs = LoopIR.Read(buf_name, load_ridx, basetyp, srcinfo)

        load_nest = [
            LoopIR.Assign(new_name, basetyp, None, load_widx, load_rhs, None, srcinfo)
        ]

        for i, n in reversed(list(zip(load_iter, shape))):
            loop = LoopIR.Seq(
                i, LoopIR.Const(0, T.index, srcinfo), n, load_nest, None, srcinfo
            )
            load_nest = [loop]

        ir, fwd_ins = fwd(block_cursor[0]).before()._insert(load_nest)
        fwd = _compose(fwd_ins, fwd)
    if isW:
        store_iter = [Sym(f"i{i}") for i, _ in enumerate(shape)]
        store_ridx = [LoopIR.Read(s, [], T.index, srcinfo) for s in store_iter]
        cp_store_ridx = store_ridx.copy()
        store_widx = []
        for w in w_exprs:
            if isinstance(w, tuple):
                store_widx.append(
                    LoopIR.BinOp("+", cp_store_ridx.pop(0), w[0], T.index, srcinfo)
                )
            else:
                store_widx.append(w)

        store_rhs = LoopIR.Read(new_name, store_ridx, basetyp, srcinfo)
        store_stmt = LoopIR.Reduce if use_accum_zero else LoopIR.Assign
        store_nest = [
            store_stmt(buf_name, basetyp, None, store_widx, store_rhs, None, srcinfo)
        ]

        for i, n in reversed(list(zip(store_iter, shape))):
            loop = LoopIR.Seq(
                i, LoopIR.Const(0, T.index, srcinfo), n, store_nest, None, srcinfo
            )
            store_nest = [loop]

        ir, fwd_ins = fwd(block_cursor[-1]).after()._insert(store_nest)
        fwd = _compose(fwd_ins, fwd)

    def mk_read(c):
        rd = c._node
        if isinstance(rd, LoopIR.Read):
            return {
                "name": new_name,
                "idx": rewrite_idx(rd.idx),
                "type": rd.type,  # non-ideal, but easiest for now
            }
        elif isinstance(rd, LoopIR.WindowExpr):
            w_idx = rewrite_win(rd.idx)
            return {
                "name": new_name,
                "idx": w_idx,
                "type": T.Window(new_typ, rd.type.as_tensor, new_name, w_idx),
            }

    def mk_write(c):
        s = c._node
        return {"name": new_name, "idx": rewrite_idx(s.idx)}

    for c in block_cursor:
        ir, fwd = _replace_pats(ir, fwd, c, f"{buf_name}[_]", mk_read)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{buf_name} += _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{buf_name} = _", mk_write)

    # new alloc, load_nest + new_body + store_nest
    new_block_c = fwd(block_cursor[0]).as_block().expand(0, len(block_cursor) - 1)
    if isR:
        new_block_c = new_block_c.expand(1, 0)
    if isW:
        new_block_c = new_block_c.expand(0, 1)
    alloc_c = new_block_c[0].prev()
    Check_Bounds(ir, alloc_c._node, [c._node for c in new_block_c])
    return ir, fwd


class DoStageWindow(Cursor_Rewrite):
    def __init__(self, proc_cursor, new_name, memory, expr):
        # Inputs
        self.new_name = Sym(new_name)
        self.memory = memory
        self.target_expr = expr._node

        # Visitor state
        self._found_expr = False
        self._complete = False
        self._copy_code = None

        super().__init__(proc_cursor)
        Check_Aliasing(self.proc)

    def _make_staged_alloc(self):
        """
        proc(Win[0:10, N, lo:hi])
        =>
        Staged : ty[10, hi - lo]
        for i0 in par(0, 10):
          for i1 in par(0, hi - lo):
            Staged[i0, i1] = Buf[0 + i0, N, lo + i1]
        proc(Staged[0:10, 0:(hi - lo)])
        """

        staged_extents = []  # e.g. 10, hi - lo
        staged_vars = []  # e.g. i0, i1
        staged_var_reads = []  # reads of staged_vars

        buf_points = []  # e.g. 0 + i0, N, lo + i1

        for idx in self.target_expr.idx:
            assert isinstance(idx, (LoopIR.Interval, LoopIR.Point))

            if isinstance(idx, LoopIR.Interval):
                assert isinstance(idx.hi.type, (T.Index, T.Size)), f"{idx.hi.type}"

                sym_i = Sym(f"i{len(staged_vars)}")
                staged_vars.append(sym_i)
                staged_extents.append(
                    LoopIR.BinOp("-", idx.hi, idx.lo, T.index, idx.srcinfo)
                )
                offset = LoopIR.Read(sym_i, [], T.index, idx.lo.srcinfo)
                buf_points.append(
                    LoopIR.BinOp("+", idx.lo, offset, T.index, idx.srcinfo)
                )
                staged_var_reads.append(LoopIR.Read(sym_i, [], T.index, idx.lo.srcinfo))
            elif isinstance(idx, LoopIR.Point):
                # TODO: test me!
                buf_points.append(idx.pt)

        assert staged_vars, "Window expression had no intervals"
        assert len(staged_vars) == len(staged_extents)

        # Staged : ty[10, hi - lo]
        srcinfo = self.target_expr.srcinfo
        data_type = self.target_expr.type.src_type.type
        alloc_type = T.Tensor(staged_extents, False, data_type)
        alloc = LoopIR.Alloc(self.new_name, alloc_type, self.memory, None, srcinfo)

        # Staged[i0, i1] = Buf[0 + i0, N, lo + i1]
        copy_stmt = LoopIR.Assign(
            self.new_name,
            data_type,
            None,
            staged_var_reads,
            LoopIR.Read(self.target_expr.name, buf_points, data_type, srcinfo),
            None,
            srcinfo,
        )

        # for i0 in par(0, 10):
        #     for i1 in par(0, hi - lo):
        for sym_i, extent_i in reversed(list(zip(staged_vars, staged_extents))):
            copy_stmt = LoopIR.Seq(
                sym_i,
                LoopIR.Const(0, T.index, srcinfo),
                extent_i,
                [copy_stmt],
                None,
                srcinfo,
            )

        # Staged[0:10, 0:(hi - lo)]
        w_extents = [
            LoopIR.Interval(LoopIR.Const(0, T.index, srcinfo), hi, srcinfo)
            for hi in staged_extents
        ]
        new_window = LoopIR.WindowExpr(
            self.new_name,
            w_extents,
            T.Window(data_type, alloc_type, self.new_name, w_extents),
            srcinfo,
        )

        return [alloc, copy_stmt], new_window

    def map_stmts(self, stmts):
        result = []

        if self.target_expr.name in [
            name for name, _ in get_writes_of_stmts([s._node for s in stmts])
        ]:
            raise NotImplementedError("StageWindow does not handle writes yet.")

        for s in stmts:
            # TODO: be smarter about None here
            s = self.apply_s(s)

            if self._found_expr and not self._complete:
                assert len(s) == 1
                assert self._copy_code
                s = s[0]

                s = self._copy_code + [s]
                self._complete = True

            result.extend(s)

        return result

    def map_e(self, e):
        if self._found_expr:
            return None

        if e is self.target_expr:
            self._found_expr = True
            self._copy_code, new_window = self._make_staged_alloc()
            return new_window

        return super().map_e(e)


class DoBoundAlloc(Cursor_Rewrite):
    def __init__(self, proc_cursor, alloc_cursor, bounds):
        self.alloc_site = alloc_cursor._node
        self.bounds = bounds
        super().__init__(proc_cursor)

    def map_stmts(self, stmts_c):
        new_stmts = []
        for i, sc in enumerate(stmts_c):
            s = sc._node
            if s is self.alloc_site:
                assert isinstance(s.type, T.Tensor)
                if len(self.bounds) != len(s.type.hi):
                    raise SchedulingError(
                        f"bound_alloc: dimensions do not match: "
                        f"{len(self.bounds)} != {len(s.type.hi)} (expected)"
                    )

                new_bounds = [
                    new if new else old for old, new in zip(s.type.hi, self.bounds)
                ]
                newtyp = T.Tensor(new_bounds, s.type.is_window, s.type.type)

                # TODO: CHECK THE BOUNDS OF ACCESSES IN stmts[i+1:] here

                s = LoopIR.Alloc(s.name, newtyp, s.mem, s.eff, s.srcinfo)
                return new_stmts + [s] + [s._node for s in stmts_c[i + 1 :]]
            else:
                new_stmts += self.map_s(sc) or [s]

        return new_stmts


def DoUnrollBuffer(alloc_cursor, dim):
    alloc_stmt = alloc_cursor._node

    assert isinstance(alloc_stmt, LoopIR.Alloc)

    if not alloc_stmt.type.shape():
        raise SchedulingError("Cannot unroll a scalar buffer")

    buf_size = alloc_stmt.type.shape()[dim]
    if not isinstance(buf_size, LoopIR.Const):
        raise SchedulingError(
            f"Expected a constant buffer dimension, got {buf_size} at {dim}'th dimension."
        )

    used_allocs = set()
    buf_syms = []
    for i in range(0, buf_size.val):
        new_name = str(alloc_stmt.name) + "_" + str(i)
        buf_syms.append(Sym(new_name))

    def mk_read(c):
        nonlocal used_allocs
        e = c._node
        if isinstance(e, LoopIR.Read):
            if not isinstance(e.idx[dim], LoopIR.Const):
                raise SchedulingError(
                    f"Expected a constant buffer access, got {e.idx[dim]} at {dim}'th dimension. Try unrolling the loop. "
                )

            used_allocs.add(e.idx[dim].val)

            sym = [e.idx[dim].val]
            new_idx = e.idx.copy()
            del new_idx[dim]

            return {"name": sym, "idx": new_idx}
        elif isinstance(e, LoopIR.WindowExpr):
            if not isinstance(e.idx[dim], LoopIR.Point):
                raise SchedulingError(
                    f"Cannot unroll a buffer at a dimension used as a window."
                )

            pt = e.idx[dim].pt
            if not isinstance(pt, LoopIR.Const):
                raise SchedulingError(
                    f"Expected a constant buffer access, got {pt} at {dim}'th dimension. Try unrolling the loop. "
                )

            used_allocs.add(pt.val)
            sym = buf_syms[pt.val]
            new_access = e.idx.copy()
            del new_access[dim]

            return {"name": sym, "idx": new_access}

    def mk_write(c):
        s = c._node
        if not isinstance(s.idx[dim], LoopIR.Const):
            raise SchedulingError(
                f"Expected a constant buffer access, got {s.idx[dim]} at {dim}'th dimension. Try unrolling the loop. "
            )

        nonlocal used_allocs
        used_allocs.add(s.idx[dim].val)

        sym = buf_syms[s.idx[dim].val]
        new_idx = s.idx.copy()
        del new_idx[dim]

        return {"name": sym, "idx": new_idx}

    ir, fwd = alloc_cursor.get_root(), lambda x: x
    for c in get_rest_of_block(alloc_cursor):
        ir, fwd = _replace_pats(ir, fwd, c, f"{alloc_stmt.name}[_]", mk_read)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_stmt.name} += _", mk_write)
        ir, fwd = _replace_pats_stmts(ir, fwd, c, f"{alloc_stmt.name} = _", mk_write)

    new_shape = alloc_stmt.type.shape().copy()
    del new_shape[dim]
    new_type = LoopIR.Tensor(new_shape, False, alloc_stmt.type.basetype())

    new_allocs = []
    for itr in used_allocs:
        alloc = LoopIR.Alloc(
            buf_syms[itr],
            new_type,
            alloc_stmt.mem,
            None,
            alloc_stmt.srcinfo,
        )
        new_allocs.append(alloc)

    ir, fwd_repl = fwd(alloc_cursor)._replace(new_allocs)
    fwd = _compose(fwd_repl, fwd)

    return ir, fwd


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# The Passes to export

__all__ = [
    ### BEGIN Scheduling Ops with Cursor Forwarding ###
    "DoSimplify",
    "DoSetTypAndMem",
    "DoInsertPass",
    "DoReorderStmt",
    "DoCommuteExpr",
    "DoSpecialize",
    "DoSplit",
    "DoUnroll",
    "DoAddLoop",
    "DoPartitionLoop",
    "DoProductLoop",
    "DoRemoveLoop",
    "DoLiftAllocSimple",
    "DoLiftConstant",
    "DoLiftScope",
    "DoFissionAfterSimple",
    "DoMergeWrites",
    "DoFuseIf",
    "DoFuseLoop",
    "DoBindExpr",
    "DoStageMem",
    "DoDataReuse",
    "DoInlineWindow",
    "DoDivideDim",
    "DoExpandDim",
    "DoMultiplyDim",
    "DoRearrangeDim",
    "DoInline",
    "DoCallSwap",
    "DoBindConfig",
    "DoConfigWrite",
    "DoDeleteConfig",
    "DoUnrollBuffer",
    ### END Scheduling Ops with Cursor Forwarding ###
    "DoPartialEval",
    "DoExtractMethod",
    "DoLiftAlloc",
    "DoFissionLoops",
    "DoBoundAndGuard",
    "DoDeletePass",
    "DoAssertIf",
    "DoAddUnsafeGuard",
    "DoStageWindow",
    "DoBoundAlloc",
]
