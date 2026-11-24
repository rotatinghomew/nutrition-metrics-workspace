"""派生指标表达式语言。

语法（分析员在登记表里直接书写，保存前由 :mod:`app.checks` 校核）：

    expr            := term (('+'|'-'|'*'|'/') term)*
    term            := NUMBER | NAME '(' args? ')' | NAME | '(' expr ')'
    args            := expr (',' expr)*

* NAME 必须引用已登记指标的 ``code``；
* 函数仅允许白名单（当前为 ``safe_div``，分母为 0 或空时返回不可计算而非伪造 0）；
* 不允许任何属性访问、字符串、调用任意 Python —— 解析器只认上面的语法。

表达式同时解析为 AST（机器校核）与人类可读的规范化文本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALLOWED_FUNCTIONS = {"safe_div"}
OPERATORS = {"+", "-", "*", "/"}


class ExpressionError(ValueError):
    """表达式语法或引用不合法。"""


@dataclass(frozen=True)
class Node:
    kind: str                 # number | ref | call | binary
    value: Any = None
    children: tuple["Node", ...] = ()
    op: str | None = None

    def references(self) -> set[str]:
        if self.kind == "ref":
            return {self.value}
        out: set[str] = set()
        for child in self.children:
            out |= child.references()
        return out

    def to_text(self) -> str:
        if self.kind == "number":
            return str(self.value)
        if self.kind == "ref":
            return str(self.value)
        if self.kind == "call":
            return f"{self.value}({', '.join(c.to_text() for c in self.children)})"
        left, right = self.children
        return f"({left.to_text()} {self.op} {right.to_text()})"


class _Tokenizer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.tokens: list[tuple[str, Any]] = []
        self._tokenize()
        self.i = 0

    def _tokenize(self) -> None:
        text = self.text
        while self.pos < len(text):
            ch = text[self.pos]
            if ch.isspace():
                self.pos += 1
            elif ch in "()+-,*/":
                self.tokens.append((ch, ch))
                self.pos += 1
            elif ch.isdigit() or ch == ".":
                start = self.pos
                dots = 0
                digits = 0
                while self.pos < len(text) and (text[self.pos].isdigit() or text[self.pos] == "."):
                    if text[self.pos] == ".":
                        dots += 1
                    else:
                        digits += 1
                    self.pos += 1
                if dots > 1 or digits == 0:
                    raise ExpressionError(f"非法数字: {text[start:self.pos]}")
                self.tokens.append(("num", float(text[start:self.pos])))
            elif ch.isalpha() or ch == "_":
                start = self.pos
                while self.pos < len(text) and (text[self.pos].isalnum() or text[self.pos] == "_"):
                    self.pos += 1
                self.tokens.append(("name", text[start:self.pos]))
            else:
                raise ExpressionError(f"非法字符 {ch!r}（表达式不允许字符串或属性访问）")

    def peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> tuple[str, Any]:
        tok = self.peek()
        if tok is None:
            raise ExpressionError("表达式意外结束")
        self.i += 1
        return tok


def parse(text: str) -> Node:
    """解析表达式为 AST。"""
    if not text or not text.strip():
        raise ExpressionError("表达式为空")
    tz = _Tokenizer(text)
    node = _parse_expr(tz)
    if tz.peek() is not None:
        raise ExpressionError(f"表达式尾部存在多余记号: {tz.peek()}")
    return node


def _parse_expr(tz: _Tokenizer) -> Node:
    node = _parse_term(tz)
    while tz.peek() is not None and tz.peek()[0] in OPERATORS:
        op, _ = tz.next()
        right = _parse_term(tz)
        node = Node(kind="binary", op=op, children=(node, right))
    return node


def _parse_term(tz: _Tokenizer) -> Node:
    kind, value = tz.next()
    if kind == "num":
        return Node(kind="number", value=value)
    if kind == "name":
        if tz.peek() and tz.peek()[0] == "(":
            tz.next()
            args: list[Node] = []
            if tz.peek() and tz.peek()[0] != ")":
                args.append(_parse_expr(tz))
                while tz.peek() and tz.peek()[0] == ",":
                    tz.next()
                    args.append(_parse_expr(tz))
            close = tz.next()
            if close[0] != ")":
                raise ExpressionError(f"函数 {value} 缺少右括号")
            if value not in ALLOWED_FUNCTIONS:
                raise ExpressionError(f"函数 {value} 不在白名单 {sorted(ALLOWED_FUNCTIONS)}")
            return Node(kind="call", value=value, children=tuple(args))
        return Node(kind="ref", value=value)
    if kind == "(":
        inner = _parse_expr(tz)
        close = tz.next()
        if close[0] != ")":
            raise ExpressionError("缺少右括号")
        return inner
    raise ExpressionError(f"意外的记号 {value!r}")


def evaluate(node: Node, values: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """在已算出的依赖指标结果上求 AST 的值。

    每个值是 ``{"value": float|None, "uncertain_value": float|None, ...}``；
    任一依赖不可计算时结果不可计算（而不是默默得到 0）。
    """
    if node.kind == "number":
        return {"value": node.value, "uncertain_value": None, "uncertain": False}
    if node.kind == "ref":
        if node.value not in values:
            return {"value": None, "uncertain_value": None, "uncertain": True,
                    "reason": f"依赖指标 {node.value} 不可计算"}
        return values[node.value]
    if node.kind == "call":
        args = [evaluate(a, values) for a in node.children]
        if node.value == "safe_div":
            numerator, denominator = args
            return _safe_div(numerator, denominator)
        raise ExpressionError(f"未知函数 {node.value}")
    if node.kind == "binary":
        left = evaluate(node.children[0], values)
        right = evaluate(node.children[1], values)
        return _apply_op(node.op, left, right)
    raise ExpressionError(f"未知节点 {node.kind}")


def _num(part: dict[str, Any], key: str = "value"):
    return part.get(key)


def _safe_div(numerator: dict, denominator: dict) -> dict:
    n, d = _num(numerator), _num(denominator)
    n_u, d_u = _num(numerator, "uncertain_value"), _num(denominator, "uncertain_value")
    if n is None or d is None or d == 0:
        reason = "分母为 0 或不可计算" if d in (None, 0) else "分子不可计算"
        return {"value": None, "uncertain_value": None, "uncertain": True, "reason": reason}
    certain = n / d
    uncertain = None
    if n_u is not None and d_u not in (None, 0):
        uncertain = n_u / d_u
    return {"value": certain, "uncertain_value": uncertain, "uncertain": False}


def _apply_op(op: str, left: dict, right: dict) -> dict:
    a, b = _num(left), _num(right)
    if a is None or b is None:
        return {"value": None, "uncertain_value": None, "uncertain": True,
                "reason": "参与运算的指标不可计算"}
    if op == "+":
        v = a + b
    elif op == "-":
        v = a - b
    elif op == "*":
        v = a * b
    elif op == "/":
        if b == 0:
            return {"value": None, "uncertain_value": None, "uncertain": True, "reason": "除数为 0"}
        v = a / b
    else:  # pragma: no cover - 解析阶段已拦截
        raise ExpressionError(f"未知运算符 {op}")
    au, bu = _num(left, "uncertain_value"), _num(right, "uncertain_value")
    uv = None
    if au is not None and bu is not None:
        uv = {"+": au + bu, "-": au - bu, "*": au * bu, "/": au / bu if bu else None}[op]
    return {"value": v, "uncertain_value": uv, "uncertain": False}
