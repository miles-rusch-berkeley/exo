from __future__ import annotations
import subprocess
import os
import ctypes
from ctypes import *
import numpy as np
import sys
import pytest
from PIL import Image
import scipy.stats as st
sys.path.append(sys.path[0]+"/..")
from SYS_ATL.debug_frontend_LoopIR import *
from SYS_ATL.prelude import *
from SYS_ATL.LoopIR_compiler import Compiler, run_compile
from SYS_ATL.LoopIR_interpreter import Interpreter
from SYS_ATL import proc, Procedure
from .helper import *

# Test 1 is add vector
#
#   add_vec( n : size, x : R[n], y : R[n], res : R[n]):
#       forall i = 0,n:
#           res[i] = x[i] + y[i]
#

def gen_add_vec_ir():
    n = Sym('n')
    x = Sym('x')
    y = Sym('y')
    res = Sym('res')
    i = Sym('i')

    src0 = null_srcinfo()

    ai = IR.AVar(i, src0)
    rhs = IR.BinOp('+', IR.Read(x, [ai], src0),
                   IR.Read(y, [ai], src0),
                   src0)
    s_a = IR.Assign(res, [ai], rhs, src0)
    an = IR.ASize(n, src0)
    loop = IR.ForAll(i, an, s_a, src0)

    return Proc('add_vec',
                [n],
                [(x, R[n], 'IN'),
                 (y, R[n], 'IN'),
                 (res, R[n], 'OUT')],
                [
                    loop
                ])


def test_add_vec_ir():
    TEST_1 = gen_add_vec_ir()
    filename = "test1"
    run_compile([TEST_1], directory, (filename + ".c"), (filename + ".h"))
    compile_so_cmd = ("clang -Wall -Werror -fPIC -O3 -shared " +
                      "-o " + directory + filename + ".so " +
                      directory + filename + ".c")
    subprocess.run(compile_so_cmd, check=True, shell=True)
    abspath = os.path.dirname(os.path.abspath(filename))
    test_lib = ctypes.CDLL(abspath + '/' + directory + filename + ".so")
    x = nparray([3.0, 6.0, 9.0])
    y = nparray([1.0, 2.0, 3.0])
    a_size = 3
    res = nprand(size=a_size)
    res_c = cvt_c(res)
    test_lib.add_vec(c_int(a_size), cvt_c(x), cvt_c(y), res_c)
    res_c = np.ctypeslib.as_array(res_c, shape=(a_size,))
    Interpreter(TEST_1, n=3, x=x, y=y, res=res)
    np.testing.assert_almost_equal(res, res_c)
    np.testing.assert_almost_equal(res, [4, 8, 12])

# TEST 2 is alloc
#   alloc( n : size, x : R[n]):
#       float *ptr = (float*) malloc (n * sizeof(float));
#       forall i = 0,n:
#           ptr[i] = x[i];
#       free(ptr);


def gen_alloc_ir():
    n = Sym('n')
    x = Sym('x')
    ptr = Sym('ptr')
    i = Sym('i')

    src0 = null_srcinfo()

    # How to pass n to alloc?
    ma = IR.Alloc(ptr, R[n].typ, None, src0)
    ai = IR.AVar(i, src0)
    rhs = IR.Read(x, [ai], src0)
    s_a = IR.Assign(ptr, [ai], rhs, src0)
    an = IR.ASize(n, src0)
    loop = IR.ForAll(i, an, s_a, src0)
    seq = IR.Seq(ma, loop, src0)

    return Proc('alloc',
                [n],
                [(x, R[n], 'IN')],
                [
                    seq
                ])


def test_alloc_ir():
    TEST_2 = gen_alloc_ir()
    run_compile([TEST_2], directory, "test_alloc.c", "test_alloc.h")

# TEST 3 is nested alloc
#   alloc_nest( n : size, m : size, x : R[n,m], y: R[n,m], res : R[n,m] ):
#       rloc : R[m]
#       forall i = 0,n:
#           xloc : R[m]
#           yloc : R[m]
#           forall j = 0,m:
#               xloc[j] = x[i,j]
#           forall j = 0,m:
#               yloc[j] = y[i,j]
#           forall j = 0,m:
#               rloc[j] = xloc[j] + yloc[j]
#           forall j = 0,m:
#               res[i,j] = rloc[j]


def gen_alloc_nest_ir():
    n = Sym('n')
    m = Sym('m')
    x = Sym('x')
    y = Sym('y')
    res = Sym('res')
    i = Sym('i')
    j1 = Sym('j1')
    j2 = Sym('j2')
    j3 = Sym('j3')
    j4 = Sym('j4')

    rloc = Sym('rloc')
    xloc = Sym('xloc')
    yloc = Sym('yloc')

    src0 = null_srcinfo()

    rloc_a = IR.Alloc(rloc, R[m].typ, None, src0)

    ai = IR.AVar(i, src0)
    aj1 = IR.AVar(j1, src0)
    aj2 = IR.AVar(j2, src0)
    aj3 = IR.AVar(j3, src0)
    aj4 = IR.AVar(j4, src0)

    xloc_a = IR.Alloc(xloc, R[m].typ, None, src0)
    yloc_a = IR.Alloc(yloc, R[m].typ, None, src0)
    seq_alloc = IR.Seq(xloc_a, yloc_a, src0)

#           forall j = 0,m:
#               xloc[j] = x[i,j]
    rhs_1 = IR.Read(x, [ai, aj1], src0)
    body_1 = IR.Assign(xloc, [aj1], rhs_1, src0)
    am = IR.ASize(m, src0)
    loop_1 = IR.ForAll(j1, am, body_1, src0)
    seq_1 = IR.Seq(seq_alloc, loop_1, src0)

#           forall j = 0,m:
#               yloc[j] = y[i,j]
    rhs_2 = IR.Read(y, [ai, aj2], src0)
    body_2 = IR.Assign(yloc, [aj2], rhs_2, src0)
    loop_2 = IR.ForAll(j2, am, body_2, src0)
    seq_2 = IR.Seq(seq_1, loop_2, src0)

#           forall j = 0,m:
#               rloc[j] = xloc[j] + yloc[j]
    rhs_3 = IR.BinOp('+', IR.Read(xloc, [aj3], src0),
                     IR.Read(yloc, [aj3], src0),
                     src0)
    body_3 = IR.Assign(rloc, [aj3], rhs_3, src0)
    loop_3 = IR.ForAll(j3, am, body_3, src0)
    seq_3 = IR.Seq(seq_2, loop_3, src0)

#           forall j = 0,m:
#               res[i,j] = rloc[j]
    rhs_4 = IR.Read(rloc, [aj4], src0)
    body_4 = IR.Assign(res, [ai, aj4], rhs_4, src0)
    loop_4 = IR.ForAll(j4, am, body_4, src0)
    seq_4 = IR.Seq(seq_3, loop_4, src0)

    an = IR.ASize(n, src0)
    loop = IR.ForAll(i, an, seq_4, src0)
    seq_top = IR.Seq(rloc_a, loop, src0)

    return Proc('alloc_nest',
                [n, m],
                [
                    (x, R[n, m], 'IN'),
                    (y, R[n, m], 'IN'),
                    (res, R[n, m], 'OUT')
                ],
                [
                    seq_top
                ])



def test_alloc_nest_ir():
    TEST_3 = gen_alloc_nest_ir()
    filename = "test_alloc_nest"
    run_compile([TEST_3], directory, (filename + ".c"), (filename + ".h"))
    compile_so_cmd = ("clang -Wall -Werror -fPIC -O3 -shared " +
                      "-o " + directory + filename + ".so " +
                      directory + filename + ".c")
    subprocess.run(compile_so_cmd, check=True, shell=True)
    abspath = os.path.dirname(os.path.abspath(filename))
    test_lib = ctypes.CDLL(abspath + '/' + directory + filename + ".so")
    x = nparray([[1.0, 2.0, 3.0], [3.2, 4.0, 5.3]])
    y = nparray([[2.6, 3.7, 8.9], [1.3, 2.3, 6.7]])
    n_size = 2
    m_size = 3
    res = nprand(size=(n_size, m_size))
    res_c = cvt_c(res)
    test_lib.alloc_nest(c_int(n_size), c_int(
        m_size), cvt_c(x), cvt_c(y), res_c)
    res_c = np.ctypeslib.as_array(res_c, shape=(n_size, m_size))
    Interpreter(TEST_3, n=n_size, m=m_size, x=x, y=y, res=res)
    np.testing.assert_almost_equal(res, res_c)
    np.testing.assert_almost_equal(res_c, nparray(
        [[3.6, 5.7, 11.9], [4.5, 6.3, 12.0]]))


"""
@proc
GEMM_Load(y : R[...], i : index, x : R[...], j : index, n : size):
    for j in par(0,n):
        y[i + k] = x[j + k]
    => gemmini_extended_mvin(x + (i0_26)*DIM, y, DIM, DIM);
    GEMM_Load(y, i0, x, i0, 16)
    alloc1 = alloc1.inline('GEMM_Load')
"""
def gen_alloc1():
    @proc
    def alloc1( n : size, x : R[n] @ IN, y : R[n] @ OUT @ GEMM ):
        for i0 in par(0,n/16):
            if i0 == n/16-1:
                instr(GEMM_Load)
                for i1 in par(0,n%16):
                    y[i0] = x[i0*16+i1]
            else:
                instr(GEMM_Load)
                for i1 in par(0,16):
                    y[i0] = x[i0*16+i1]

    return alloc1

def test_alloc1():
    alloc1 = gen_alloc1()
    assert type(alloc1) is Procedure

    filename = "compiler_test_alloc1"

    # Write pretty printing to a file
    f_pretty = open(os.path.join(directory, filename + "_pretty.atl"), "w")
    f_pretty.write(str(alloc1))
    f_pretty.close()

    alloc1.compile_c(directory, filename)


def gen_alloc2():
    @proc
    def alloc2( n : size, x : R[n] @ IN, y : R[n] @ OUT @ GEMM ):
        for i0 in par(0,n/16-1):
            instr(GEMM_Load)
            for i1 in par(0,16):
                y[i0] = x[i0*16+i1]
        instr(GEMM_Load)
        for i1 in par(0,n%16):
            y[n/16-1] = x[(n/16-1)*16+i1]

    return alloc2

def test_alloc2():
    alloc2 = gen_alloc2()
    assert type(alloc2) is Procedure

    filename = "compiler_test_alloc2"

    # Write pretty printing to a file
    f_pretty = open(os.path.join(directory, filename + "_pretty.atl"), "w")
    f_pretty.write(str(alloc2))
    f_pretty.close()

    alloc2.compile_c(directory, filename)