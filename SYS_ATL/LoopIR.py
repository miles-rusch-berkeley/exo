from .asdl.adt import ADT
from .asdl.adt import memo as ADTmemo

from .prelude import *

from . import shared_types as T

from .instruction_type import Instruction

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Untyped AST

front_ops = {
    "+":    True,
    "-":    True,
    "*":    True,
    "/":    True,
    "%":    True,
    #
    "<":    True,
    ">":    True,
    "<=":   True,
    ">=":   True,
    "==":   True,
    #
    "and":  True,
    "or":   True,
}

UAST = ADT("""
module UAST {
    proc    = ( name?           name,
                sym*            sizes,
                fnarg*          args,
                stmt*           body,
                srcinfo         srcinfo )

    fnarg   = ( sym             name,
                type            type,
                effect          effect,
                string?         mem,
                srcinfo         srcinfo )

    stmt    = Assign  ( sym name, expr* idx, expr rhs )
            | Reduce  ( sym name, expr* idx, expr rhs )
            | Pass    ()
            | If      ( expr cond, stmt* body,  stmt* orelse )
            | ForAll  ( sym iter,  expr cond,   stmt* body )
            | Alloc   ( sym name, type type, string? mem )
            | Instr   ( instr op, stmt body )
            attributes( srcinfo srcinfo )

    expr    = Read    ( sym name, expr* idx )
            | Const   ( object val )
            | USub    ( expr arg ) -- i.e.  -(...)
            | BinOp   ( op op, expr lhs, expr rhs )
            | ParRange( expr lo, expr hi ) -- only use for loop cond
            attributes( srcinfo srcinfo )

} """, {
    'name':     is_valid_name,
    'sym':      lambda x: type(x) is Sym,
    'type':     T.is_type,
    'effect':   T.is_effect,
    'instr':    lambda x: isinstance(x, Instruction),
    'op':       lambda x: x in front_ops,
    'srcinfo':  lambda x: type(x) is SrcInfo,
})


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Loop IR

"""
procedures/programs
P       ::= function name ( size*, fnarg* ) s   // top level function
size    ::= n                            // size variable name
fnarg   ::= name : type : effect?        // function argument
statements
s       ::= x[a*]  = e                   // assignment to buffer
          | x[a*] += e                   // reduction to buffer
          | s0 ; s1                      // serial composition
          | if p then s                  // conditional guard
          | if p then s1 else s2         // conditional guard
          | forall i=0,a do s            // unordered looping
          | alloc name : type            // memory allocation
                                         // assume sensible auto-free
expressions
e       ::= x[a*]                        // variable access
                                         // if an array, must be
                                         // fully indexed
          | c                            // scalar constant
          | e0 + e1                      // scalar addition
          | e0 * e1                      // scalar multiplication
          | e0 / e1                      // scalar division
          | f(e0,...)                    // built-in scalar functions
          | (p)? e                       // select/indicator (optional)
predicates
p       ::= true | false                 // constants
          | a0 = a1 | a0 < a1            // affine-comparisons
          | p0 and p1 | p0 or p1         // boolean combinations
          | R(a0,...)                    // data-predicates (optional)
affine-index-expressions
a       ::= i                            // index variable
          | c                            // rational(?) constant
          | c * a                        // scaling
          | a / c                        // division
          | a % c                        // remainder
          | a0 + a1                      // addition
type    ::= R                            // scalar "real" number
          | [n]type                      // array of n things
"""

bin_ops = {
    "+":    True,
    "-":    True,
    "*":    True,
    "/":    True,
    "%":    True,

    "and":  True,
    "or":   True,

    "<":    True,
    ">":    True,
    "<=":   True,
    ">=":   True,
    "==":   True,
}

LoopIR = ADT("""
module LoopIR {
    proc    = ( name?           name,
                fnarg*          args,
                stmt            body,
                srcinfo         srcinfo )

    fnarg   = ( sym             name,
                type            type,
                effect?         effect,
                mem?            mem,
                srcinfo         srcinfo )

    stmt    = Assign( sym name, aexpr* idx, expr rhs)
            | Reduce( sym name, aexpr* idx, expr rhs )
            | Pass()
            | If ( pred cond, stmt* body, stmt* orelse )
            | ForAll ( sym iter, aexpr hi, stmt* body )
            | Alloc ( sym name, type type, mem? mem )
            | Free  ( sym name, type type, mem? mem )
            | Instr ( instr op, stmt body )
            attributes( srcinfo srcinfo )

    expr    = Read( sym name, aexpr* idx )
            | Const( object val )
            | BinOp( binop op, expr lhs, expr rhs )
            | Select( expr cond, expr body )
            attributes( type type, srcinfo srcinfo )

} """, {
    'name':     is_valid_name,
    'sym':      lambda x: type(x) is Sym,
    'type':     T.is_type,
    'effect':   T.is_effect,
    'instr':    lambda x: isinstance(x, Instruction),
    'mem':      lambda x: type(x) is str,
    'binop':    lambda x: x in bin_ops,
    'srcinfo':  lambda x: type(x) is SrcInfo,
})
