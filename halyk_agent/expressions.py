from __future__ import annotations

import ast
from decimal import Decimal


ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


class ExpressionError(ValueError):
    pass


def referenced_names(expression: str) -> set[str]:
    """Return fact identifiers after validating the complete DSL syntax."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression: {expression}") from exc
    names: set[str] = set()
    _validate(tree.body, names)
    return names


def evaluate(expression: str, values: dict[str, Decimal]) -> Decimal:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression: {expression}") from exc
    return _eval(tree.body, values)


def _validate(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, ast.Name):
        names.add(node.id)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
        try:
            Decimal(str(node.value))
        except Exception as exc:
            raise ExpressionError(f"Invalid decimal constant: {node.value!r}") from exc
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        _validate(node.operand, names)
        return
    if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_BINOPS:
        _validate(node.left, names)
        _validate(node.right, names)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "abs" and len(node.args) == 1:
            _validate(node.args[0], names)
            return
        if node.func.id in {"min", "max"} and node.args:
            for arg in node.args:
                _validate(arg, names)
            return
    raise ExpressionError(f"Expression construct is not allowed: {ast.dump(node)}")


def _eval(node: ast.AST, values: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise ExpressionError(f"Unknown fact: {node.id}")
        return values[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval(node.operand, values)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_BINOPS:
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if isinstance(node.op, ast.Div) and right == 0:
            raise ExpressionError("Division by zero")
        return ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_eval(arg, values) for arg in node.args]
        if node.func.id == "abs" and len(args) == 1:
            return abs(args[0])
        if node.func.id == "min" and args:
            return min(args)
        if node.func.id == "max" and args:
            return max(args)
    raise ExpressionError(f"Expression construct is not allowed: {ast.dump(node)}")
