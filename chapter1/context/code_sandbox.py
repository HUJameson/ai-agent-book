"""Statement-level AST whitelist for code_interpreter (DESIGN 6.5 / D8 修复).

Background
----------
The interpreter previously executed model-generated code with a bare
``exec(code, {"__builtins__": {safe dict}})`` namespace restriction. As
``calc_sandbox.py`` documents for the expression case, ``__builtins__`` only
governs *name resolution* -- attribute access and subscripts are interpreter
bytecode and bypass it entirely, so the classic escape works unchanged:

    ().__class__.__mro__[1].__subclasses__()[i].__init__.__globals__["os"]

This module applies the same philosophy as ``calc_sandbox.py`` at statement
level: validate every syntax-tree node before the interpreter sees it.

The single escape primitive is attribute access, so ``ast.Attribute`` is
rejected except when its value is the name ``math`` with a public attribute
(``math.sin(x)`` must keep working; ``math.__class__`` must not). With
attributes gone, ``().__class__`` fails on its very first step, and imports,
function/class definitions, lambdas and comprehensions (all rejected) cannot
reintroduce it. Operators are deliberately not type-checked: they only combine
values that already passed the whitelist and cannot reach the object model.
"""

import ast
import contextlib
import io
import math

# Names the executed code may reference as builtins (mirrors the namespace the
# previous sandbox hand-built in agent.py, so the supported surface is kept).
SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "sum": sum,
    "min": min,
    "max": max,
    "round": round,
    "len": len,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "sorted": sorted,
    "reversed": reversed,
    "range": range,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "print": print,
}

# The one module whose (public) attributes may be accessed.
_ALLOWED_MODULE_NAME = "math"

_STATEMENTS = (
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Expr,
    ast.For,
    ast.While,
    ast.If,
    ast.Break,
    ast.Continue,
    ast.Pass,
)
_EXPRESSIONS = (
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.IfExp,
)


class UnsafeCode(ValueError):
    """Raised when the code uses syntax outside the code_interpreter whitelist."""


def _reject(node):
    raise UnsafeCode(f"unsupported syntax: {type(node).__name__}")


# Operator context nodes are validated field-by-field; the operator objects
# themselves (ast.Add, ast.Mult, ...) carry no code and pass through here.
_OPERATORS = (
    ast.operator, ast.unaryop, ast.boolop, ast.cmpop,
)


def _check(node):
    """Recursively validate that every node is on the whitelist."""
    if node is None:
        return
    if isinstance(node, ast.Module):
        for stmt in node.body:
            _check(stmt)
    elif isinstance(node, _OPERATORS):
        return  # operator objects carry no code of their own
    elif isinstance(node, _STATEMENTS):
        _check_statement(node)
    elif isinstance(node, _EXPRESSIONS):
        _check_expression(node)
    else:
        _reject(node)


def _check_statement(node):
    if isinstance(node, ast.Expr):
        # An expression used as a statement (print(...), bare calls): its value
        # must go through the expression whitelist like any other expression.
        _check(node.value)
    elif isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            _check(target)
        _check(node.value)
    elif isinstance(node, ast.AnnAssign):
        _check(node.target)
        _check(node.annotation)
        _check(node.value)
    elif isinstance(node, ast.For):
        _check(node.target)
        _check(node.iter)
        for stmt in node.body + node.orelse:
            _check(stmt)
    elif isinstance(node, ast.While):
        _check(node.test)
        for stmt in node.body + node.orelse:
            _check(stmt)
    elif isinstance(node, ast.If):
        _check(node.test)
        for stmt in node.body + node.orelse:
            _check(stmt)
    # Break / Continue / Pass have no children to validate.


def _check_expression(node):
    if isinstance(node, ast.Call):
        _check_call(node)
    elif isinstance(node, ast.Attribute):
        # The only allowed attribute base is the name `math` with a public
        # attribute; anything else is an escape attempt in progress.
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == _ALLOWED_MODULE_NAME
            and not node.attr.startswith("_")
        ):
            return
        _reject(node)
    elif isinstance(node, ast.Name):
        # Free variables are fine: unknown names fail at runtime with a plain
        # NameError, and dangerous names are simply absent from the namespace.
        return
    elif isinstance(node, ast.Constant):
        # Strings are needed for print labels and dict keys; bytes stay out.
        if node.value is None or isinstance(
            node.value, (int, float, complex, bool, str)
        ):
            return
        _reject(node)
    elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare)):
        # Operator types are not restricted: they combine already-whitelisted
        # values and cannot reach the object model on their own. The operator
        # objects themselves pass through _check via _OPERATORS.
        for child in ast.iter_child_nodes(node):
            _check(child)
    elif isinstance(node, ast.Subscript):
        _check(node.value)
        _check(node.slice)
    elif isinstance(node, ast.Slice):
        for child in (node.lower, node.upper, node.step):
            _check(child)
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            _check(value)
    elif isinstance(node, ast.FormattedValue):
        _check(node.value)
        _check(node.format_spec)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            _check(elt)
    elif isinstance(node, ast.Dict):
        for key in node.keys:
            _check(key)
        for value in node.values:
            _check(value)
    elif isinstance(node, ast.IfExp):
        _check(node.test)
        _check(node.body)
        _check(node.orelse)


def _check_call(node):
    # Only whitelisted builtin names and the allowed module's attributes may be
    # called; anything else (method calls, chained attribute access) aborts.
    func = node.func
    if isinstance(func, ast.Name):
        if func.id not in SAFE_BUILTINS:
            raise UnsafeCode(f"only whitelisted functions may be called: {func.id!r}")
    elif isinstance(func, ast.Attribute):
        _check_expression(func)  # enforces the math.<public attr> rule
    else:
        _reject(node)
    for arg in node.args:
        _check(arg)
    for keyword in node.keywords:
        if keyword.arg is None:
            raise UnsafeCode("**kwargs is not allowed")
        _check(keyword.value)


def safe_exec(code):
    """Validate and execute whitelisted Python statements.

    Returns ``(namespace, printed_output)`` where *namespace* is the execution
    globals (variables the code defined, plus ``math`` and ``__builtins__``)
    and *printed_output* is everything the code printed.

    Raises:
        UnsafeCode: if the code uses syntax outside the whitelist.
        ValueError: for empty input or unparsable code.
        NameError / ZeroDivisionError / ...: runtime errors from the code
            itself, exactly as a normal interpreter would raise them.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("empty code")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"invalid code: {exc}") from exc
    _check(tree)

    namespace = {"__builtins__": dict(SAFE_BUILTINS), _ALLOWED_MODULE_NAME: math}
    output_buffer = io.StringIO()
    with contextlib.redirect_stdout(output_buffer):
        exec(compile(tree, "<safe_code>", "exec"), namespace)
    return namespace, output_buffer.getvalue()
