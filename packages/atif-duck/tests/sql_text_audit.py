# SPDX-License-Identifier: Apache-2.0

"""AST audit: every placeholder in a SQL-building f-string resolves to trusted text.

The registry modules build statements from f-strings. The rule this audit
enforces is the one the ``SqlFragment`` type documents: a placeholder may hold
a module constant, a catalog constant (an UPPER_CASE import), a projection
call (``render`` and friends), or a ``sql_literal(...)`` / ``int(...)`` /
``float(...)`` coercion. Anything that reached the function from outside
(a parameter with no trusted call site, a value read from DuckDB or the
filesystem) is a violation, whether it is interpolated directly or through a
local variable, a comprehension, a helper's return value, or a ``.join``.

It is a static over-approximation on purpose: trust is decided from source
text alone, one module at a time, so a value it cannot follow is untrusted.
The tests plant an ``f"SELECT * FROM {user_input}"`` and check it is caught.

Which f-strings are audited: any whose literal text carries a SQL marker
(``SELECT``, ``FROM``, ``CAST``, ``AS``, ``read_json``, ...), any inside a
function annotated to return ``SqlFragment``, and any passed straight to
``SqlFragment(...)``, ``execute(...)`` or ``sql(...)``. A logging f-string
without a marker is not SQL and is left alone.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

#: Uppercase SQL words and DuckDB readers that mark an f-string as SQL text.
SQL_MARKER = re.compile(
    r"\b(SELECT|FROM|WHERE|CREATE|TABLE|VIEW|MACRO|UNNEST|UNION|ATTACH|CAST|VALUES|DELETE|AS"
    r"|read_json|read_parquet|json_extract|json_extract_string|epoch_us)\b"
)

#: Calls whose RESULT is SQL-safe whatever the argument was.
SANITIZERS = frozenset({"sql_literal", "int", "float"})

#: Calls that produce SQL text from their (trusted) arguments.
PRODUCERS = frozenset({"render", "step_columns", "step_key_columns", "SqlFragment"})

#: Builtins that pass their arguments' trust through unchanged.
PASS_THROUGH = frozenset(
    {"str", "sorted", "tuple", "list", "dict", "zip", "enumerate", "reversed", "len", "repr"}
)

#: Sinks whose direct f-string argument is always audited.
SINKS = frozenset({"execute", "sql", "SqlFragment"})

_CONSTANT_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


@dataclass
class Violation:
    line: int
    expression: str

    def __str__(self) -> str:
        return f"line {self.line}: {{{self.expression}}} is not trusted SQL text"


@dataclass
class ModuleAudit:
    """One module's trust analysis. Build with :func:`audit_source`."""

    tree: ast.Module
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)
    constants: set[str] = field(default_factory=set)
    audited: list[ast.JoinedStr] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    _in_progress: set[tuple[str, str]] = field(default_factory=set)

    # ----------------------------------------------------------------- setup
    def __post_init__(self) -> None:
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
        for node in self.tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if _CONSTANT_NAME.match(name):
                        self.constants.add(name)
            for target in targets:
                if isinstance(target, ast.Name) and _CONSTANT_NAME.match(target.id):
                    self.constants.add(target.id)

    def enclosing_function(self, node: ast.AST) -> ast.FunctionDef | None:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.FunctionDef):
                return current
            current = self.parents.get(current)
        return None

    # ------------------------------------------------------------- selection
    def _returns_sql_fragment(self, fn: ast.FunctionDef | None) -> bool:
        return (
            fn is not None and isinstance(fn.returns, ast.Name) and fn.returns.id == "SqlFragment"
        )

    def _is_sink_argument(self, node: ast.JoinedStr) -> bool:
        parent = self.parents.get(node)
        return (
            isinstance(parent, ast.Call)
            and node in parent.args
            and (
                (isinstance(parent.func, ast.Name) and parent.func.id in SINKS)
                or (isinstance(parent.func, ast.Attribute) and parent.func.attr in SINKS)
            )
        )

    def _is_sql(self, node: ast.JoinedStr) -> bool:
        literal = "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return (
            SQL_MARKER.search(literal) is not None
            or self._returns_sql_fragment(self.enclosing_function(node))
            or self._is_sink_argument(node)
        )

    def run(self) -> ModuleAudit:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.JoinedStr) and self._is_sql(node):
                self.audited.append(node)
                fn = self.enclosing_function(node)
                bound = self._comprehension_bindings(node, fn)
                for part in node.values:
                    if isinstance(part, ast.FormattedValue) and not self.trusted(
                        part.value, fn, bound
                    ):
                        self.violations.append(Violation(node.lineno, ast.unparse(part.value)))
        return self

    def _comprehension_bindings(self, node: ast.AST, fn: ast.FunctionDef | None) -> dict[str, bool]:
        """Names bound by the comprehensions enclosing ``node``, outermost first.

        An f-string inside ``", ".join(f"..." for name, t in columns)`` reads
        ``name`` from the generator, not from the function's locals, and the
        generator's target inherits the trust of what it iterates.
        """
        comprehensions: list[ast.GeneratorExp | ast.ListComp | ast.SetComp] = []
        current: ast.AST | None = self.parents.get(node)
        while current is not None and not isinstance(current, ast.FunctionDef):
            if isinstance(current, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                comprehensions.append(current)
            current = self.parents.get(current)
        bound: dict[str, bool] = {}
        for comprehension in reversed(comprehensions):
            for generator in comprehension.generators:
                iterable_ok = self.trusted(generator.iter, fn, bound)
                for name in _target_names(generator.target):
                    bound[name] = iterable_ok
        return bound

    # ----------------------------------------------------------------- trust
    def trusted(self, node: ast.expr, fn: ast.FunctionDef | None, bound: dict[str, bool]) -> bool:  # noqa: PLR0911
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.JoinedStr):
            return all(
                self.trusted(part.value, fn, bound)
                for part in node.values
                if isinstance(part, ast.FormattedValue)
            )
        if isinstance(node, ast.FormattedValue):
            return self.trusted(node.value, fn, bound)
        if isinstance(node, ast.Name):
            return self._name_trusted(node.id, fn, bound)
        if isinstance(node, ast.Call):
            return self._call_trusted(node, fn, bound)
        if isinstance(node, ast.Subscript):
            return self.trusted(node.value, fn, bound) and self._slice_trusted(
                node.slice, fn, bound
            )
        if isinstance(node, ast.Attribute):
            return self.trusted(node.value, fn, bound)
        if isinstance(node, ast.BinOp):
            return self.trusted(node.left, fn, bound) and self.trusted(node.right, fn, bound)
        if isinstance(node, ast.IfExp):
            return self.trusted(node.body, fn, bound) and self.trusted(node.orelse, fn, bound)
        if isinstance(node, ast.BoolOp):
            return all(self.trusted(value, fn, bound) for value in node.values)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return all(self.trusted(elt, fn, bound) for elt in node.elts)
        if isinstance(node, ast.Dict):
            return all(
                self.trusted(part, fn, bound)
                for part in (*node.keys, *node.values)
                if part is not None
            )
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            inner = dict(bound)
            for generator in node.generators:
                iterable_ok = self.trusted(generator.iter, fn, inner)
                for name in _target_names(generator.target):
                    inner[name] = iterable_ok
            return self.trusted(node.elt, fn, inner)
        if isinstance(node, ast.Starred):
            return self.trusted(node.value, fn, bound)
        if isinstance(node, ast.NamedExpr):
            return self.trusted(node.value, fn, bound)
        return False

    def _slice_trusted(
        self, node: ast.expr, fn: ast.FunctionDef | None, bound: dict[str, bool]
    ) -> bool:
        if isinstance(node, ast.Slice):
            return all(
                part is None or self.trusted(part, fn, bound)
                for part in (node.lower, node.upper, node.step)
            )
        return self.trusted(node, fn, bound)

    def _call_trusted(  # noqa: PLR0911
        self, node: ast.Call, fn: ast.FunctionDef | None, bound: dict[str, bool]
    ) -> bool:
        args_ok = all(self.trusted(arg, fn, bound) for arg in node.args) and all(
            self.trusted(keyword.value, fn, bound) for keyword in node.keywords
        )
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in SANITIZERS:
                return True
            if func.id in PRODUCERS or func.id in PASS_THROUGH:
                return args_ok
            if func.id in self.functions:
                # A module-defined helper is trusted by what it RETURNS; how it
                # treats each parameter is settled when that parameter is
                # interpolated (see _parameter_trusted), so a helper that
                # sanitizes an untrusted argument is still trusted here.
                return self._function_returns_trusted(func.id)
            return False
        if isinstance(func, ast.Attribute):
            # ``", ".join(...)``, ``WHOLE_STEP.json(...)``, ``columns.items()``,
            # ``_VIEW_PROJECTIONS.get(...)``: trusted receiver, trusted arguments.
            return args_ok and self.trusted(func.value, fn, bound)
        return False

    def _function_returns_trusted(self, name: str) -> bool:
        key = ("return", name)
        if key in self._in_progress:
            return False
        self._in_progress.add(key)
        try:
            target = self.functions[name]
            returns = [
                node.value
                for node in ast.walk(target)
                if isinstance(node, ast.Return) and node.value is not None
            ]
            return bool(returns) and all(self.trusted(value, target, {}) for value in returns)
        finally:
            self._in_progress.discard(key)

    def _name_trusted(self, name: str, fn: ast.FunctionDef | None, bound: dict[str, bool]) -> bool:
        if name in bound:
            return bound[name]
        if name in self.constants:
            return True
        if fn is None:
            return False
        params = [arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)]
        if name in params:
            return self._parameter_trusted(fn, name)
        return self._local_trusted(fn, name)

    def _parameter_trusted(self, fn: ast.FunctionDef, name: str) -> bool:  # noqa: PLR0911
        key = ("param", f"{fn.name}.{name}")
        if key in self._in_progress:
            return False
        self._in_progress.add(key)
        try:
            for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs):
                if (
                    arg.arg == name
                    and isinstance(arg.annotation, ast.Name)
                    and arg.annotation.id == "SqlFragment"
                ):
                    return True
            call_sites = self._call_sites(fn)
            if not call_sites:
                return False
            positional = [arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args)]
            offset = 1 if positional and positional[0] in {"self", "cls"} else 0
            for call in call_sites:
                caller = self.enclosing_function(call)
                supplied: ast.expr | None = None
                for keyword in call.keywords:
                    if keyword.arg == name:
                        supplied = keyword.value
                if supplied is None:
                    index = positional.index(name) - offset if name in positional else -1
                    if 0 <= index < len(call.args):
                        supplied = call.args[index]
                if supplied is None:
                    # Defaulted at every such call: trust the default's text.
                    default = self._default_for(fn, name)
                    if default is None or not self.trusted(default, None, {}):
                        return False
                    continue
                if not self.trusted(supplied, caller, {}):
                    return False
            return True
        finally:
            self._in_progress.discard(key)

    @staticmethod
    def _default_for(fn: ast.FunctionDef, name: str) -> ast.expr | None:
        positional = [*fn.args.posonlyargs, *fn.args.args]
        defaults = fn.args.defaults
        for arg, default in zip(
            positional[len(positional) - len(defaults) :], defaults, strict=True
        ):
            if arg.arg == name:
                return default
        for arg, kw_default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
            if arg.arg == name:
                return kw_default
        return None

    def _call_sites(self, fn: ast.FunctionDef) -> list[ast.Call]:
        sites: list[ast.Call] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id == fn.name) or (
                isinstance(func, ast.Attribute) and func.attr == fn.name
            ):
                sites.append(node)
        return sites

    def _local_trusted(self, fn: ast.FunctionDef, name: str) -> bool:  # noqa: PLR0911
        key = ("local", f"{fn.name}.{name}")
        if key in self._in_progress:
            return False
        self._in_progress.add(key)
        try:
            bindings = 0
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(
                    name in _target_names(t) for t in node.targets
                ):
                    bindings += 1
                    for target in node.targets:
                        if not self._assignment_trusted(target, node.value, fn, name):
                            return False
                elif isinstance(node, ast.AnnAssign) and name in _target_names(node.target):
                    bindings += 1
                    if node.value is None or not self._assignment_trusted(
                        node.target, node.value, fn, name
                    ):
                        return False
                elif (isinstance(node, ast.AugAssign) and name in _target_names(node.target)) or (
                    isinstance(node, ast.NamedExpr) and node.target.id == name
                ):
                    bindings += 1
                    if not self.trusted(node.value, fn, {}):
                        return False
                elif isinstance(node, ast.For) and name in _target_names(node.target):
                    bindings += 1
                    if not self._assignment_trusted(node.target, node.iter, fn, name, unpack=True):
                        return False
                elif isinstance(node, ast.With):
                    for item in node.items:
                        if item.optional_vars is not None and name in _target_names(
                            item.optional_vars
                        ):
                            return False
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == name
                    and node.func.attr in {"append", "extend", "insert", "update", "add"}
                ):
                    if not all(self.trusted(arg, fn, {}) for arg in node.args):
                        return False
            return bindings > 0
        finally:
            self._in_progress.discard(key)

    def _assignment_trusted(  # noqa: PLR0911
        self,
        target: ast.expr,
        value: ast.expr,
        fn: ast.FunctionDef,
        name: str,
        *,
        unpack: bool = False,
    ) -> bool:
        """Trust of ``name`` given ``target = value`` (or ``for target in value``)."""
        if isinstance(target, ast.Name):
            return self.trusted(value, fn, {})
        if isinstance(target, (ast.Tuple, ast.List)):
            if unpack:
                # ``for a, b in iterable``: every element inherits the iterable.
                if isinstance(value, (ast.Tuple, ast.List)):
                    return all(self.trusted(elt, fn, {}) for elt in value.elts)
                return self.trusted(value, fn, {})
            if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
                for elt_target, elt_value in zip(target.elts, value.elts, strict=True):
                    if name in _target_names(elt_target):
                        return self.trusted(elt_value, fn, {})
            return self.trusted(value, fn, {})
        return False


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in target.elts:
            names |= _target_names(elt)
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def audit_source(source: str) -> ModuleAudit:
    """Audit one module's source text."""
    return ModuleAudit(tree=ast.parse(source)).run()


__all__ = ["ModuleAudit", "Violation", "audit_source"]
