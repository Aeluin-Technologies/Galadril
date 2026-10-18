"""Reviewed numerical callbacks never expose native modules or host resources."""

from __future__ import annotations

import ast
import math
import re
import threading
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.text import Text
from pydantic_monty import ClassInstance

_RENDER_LOCK = threading.Lock()
_MAX_POINTS = 2048


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected finite numbers")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("Expected finite numbers")
    return converted


def vector(value: object) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_POINTS:
        raise ValueError("Use at most 2048 numbers per series")
    return [number(item) for item in value]


class Numbers:
    __slots__ = ()
    pi = math.pi
    e = math.e

    def array(self, values: object) -> list[float]:
        return vector(values)

    def linspace(self, start: float, stop: float, num: int = 50) -> list[float]:
        if (
            isinstance(num, bool)
            or not isinstance(num, int)
            or not 1 <= num <= _MAX_POINTS
        ):
            raise ValueError("Use 1 to 2048 samples")
        return [
            float(item)
            for item in np.linspace(number(start), number(stop), num)
        ]

    def arange(
        self, start: float, stop: float | None = None, step: float = 1
    ) -> list[float]:
        first, last = (
            (0.0, number(start))
            if stop is None
            else (number(start), number(stop))
        )
        stride = number(step)
        if (
            stride == 0
            or not math.isfinite((last - first) / stride)
            or abs((last - first) / stride) > _MAX_POINTS
        ):
            raise ValueError("Use at most 2048 samples and a nonzero step")
        return [float(item) for item in np.arange(first, last, stride)]

    def sin(self, values: object) -> float | list[float]:
        return self._map(values, "sin")

    def cos(self, values: object) -> float | list[float]:
        return self._map(values, "cos")

    def exp(self, values: object) -> float | list[float]:
        return self._map(values, "exp")

    def sqrt(self, values: object) -> float | list[float]:
        return self._map(values, "sqrt")

    def _map(self, values: object, operation: str) -> float | list[float]:
        callback = {
            "sin": math.sin,
            "cos": math.cos,
            "exp": math.exp,
            "sqrt": math.sqrt,
        }[operation]
        if isinstance(values, (list, tuple)):
            return [number(callback(item)) for item in vector(values)]
        return number(callback(number(values)))

    def mean(self, values: object) -> float:
        data = vector(values)
        if not data:
            raise ValueError("A sample is required")
        return number(float(np.mean(data)))

    def std(self, values: object) -> float:
        data = vector(values)
        if not data:
            raise ValueError("A sample is required")
        return number(float(np.std(data)))

    def sum(self, values: object) -> float:
        return number(math.fsum(vector(values)))

    def min(self, values: object) -> float:
        return min(vector(values))

    def max(self, values: object) -> float:
        return max(vector(values))


def label(value: str) -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("Chart labels must not exceed 256 characters")
    return value


@dataclass(slots=True)
class Plot:
    series: list[tuple[str, list[float], list[float], str, str]] = field(
        default_factory=list
    )
    heading: str = ""
    horizontal: str = ""
    vertical: str = ""
    legends: bool = False
    gridlines: bool = False

    def plot(
        self, x: object, y: object = None, fmt: str = "", *, label: str = ""
    ) -> None:
        if not isinstance(fmt, str) or not re.fullmatch(
            r"[bgrcmykw]?[.ov^<>s*+xDd|_]?(?:--|-\.|-|:)?", fmt
        ):
            raise ValueError("Unsupported line format")
        self._series("line", x, y, fmt, label)

    def scatter(self, x: object, y: object, *, label: str = "") -> None:
        self._series("scatter", x, y, "", label)

    def bar(self, x: object, height: object, *, label: str = "") -> None:
        self._series("bar", x, height, "", label)

    def _series(
        self, kind: str, x: object, y: object, fmt: str, heading: str
    ) -> None:
        horizontal = vector(x)
        vertical = vector(y) if y is not None else horizontal
        if y is None:
            horizontal = [float(index) for index in range(len(vertical))]
        if (
            len(horizontal) != len(vertical)
            or not horizontal
            or len(self.series) >= 8
        ):
            raise ValueError("Use up to eight nonempty, equally sized series")
        self.series.append((kind, horizontal, vertical, fmt, label(heading)))

    def title(self, value: str) -> None:
        self.heading = label(value)

    def xlabel(self, value: str) -> None:
        self.horizontal = label(value)

    def ylabel(self, value: str) -> None:
        self.vertical = label(value)

    def legend(self) -> None:
        self.legends = True

    def grid(self, visible: bool = True) -> None:
        if not isinstance(visible, bool):
            raise ValueError("Expected a boolean")
        self.gridlines = visible

    def show(self) -> None:
        return None

    def tight_layout(self) -> None:
        return None

    def figure(self, *, figsize: object = None) -> None:
        if figsize is not None:
            size = vector(figsize)
            if len(size) != 2 or any(not 1 <= item <= 10 for item in size):
                raise ValueError("Use a bounded figure size")

    def savefig(self, filename: str) -> None:
        if (
            not isinstance(filename, str)
            or PurePosixPath(filename).name != filename
            or not filename.endswith(".png")
            or len(filename) > 128
        ):
            raise ValueError(
                "Use a PNG filename without a path; images are returned directly"
            )

    def render(self) -> bytes | None:
        """Fixed dimensions and literal labels bound native renderer work."""
        if not self.series:
            return None
        with _RENDER_LOCK:
            figure = Figure(figsize=(6, 4), dpi=100)
            FigureCanvasAgg(figure)
            axes = figure.subplots()
            for kind, x, y, fmt, heading in self.series:
                if kind == "scatter":
                    axes.scatter(x, y, label=heading)
                elif kind == "bar":
                    axes.bar(x, y, label=heading)
                else:
                    axes.plot(x, y, fmt, label=heading)
            axes.set_title(self.heading, parse_math=False)
            axes.set_xlabel(self.horizontal, parse_math=False)
            axes.set_ylabel(self.vertical, parse_math=False)
            axes.grid(self.gridlines)
            if self.legends:
                axes.legend()
            for text in figure.findobj(Text):
                text.set_parse_math(False)
            buffer = BytesIO()
            figure.savefig(
                buffer, format="png", metadata={"Software": "Galadril"}
            )
            result = buffer.getvalue()
            if len(result) > 131072:
                raise ValueError("Chart output exceeds limit")
            return result


def scientific_source(code: str, plot: Plot) -> tuple[str, dict[str, object]]:
    """Rewrite only reviewed import bindings; all user execution stays in Monty."""
    tree = ast.parse(code)
    if sum(1 for _ in ast.walk(tree)) > 2048:
        raise ValueError("Calculation syntax exceeds limit")
    numbers = ClassInstance(
        Numbers(),
        eager_attrs=("pi", "e"),
        allowed_methods=(
            "array",
            "linspace",
            "arange",
            "sin",
            "cos",
            "exp",
            "sqrt",
            "mean",
            "std",
            "sum",
            "min",
            "max",
        ),
    )
    charts = ClassInstance(
        plot,
        allowed_methods=(
            "plot",
            "scatter",
            "bar",
            "title",
            "xlabel",
            "ylabel",
            "legend",
            "grid",
            "show",
            "tight_layout",
            "figure",
            "savefig",
        ),
    )
    bindings: dict[str, object] = {
        "__galadril_numpy": numbers,
        "__galadril_pyplot": charts,
    }
    modules = {
        "numpy": "__galadril_numpy",
        "matplotlib.pyplot": "__galadril_pyplot",
    }

    class Imports(ast.NodeTransformer):
        def visit_Import(self, node: ast.Import) -> ast.stmt | list[ast.stmt]:
            replacements: list[ast.stmt] = []
            for alias in node.names:
                if alias.name in modules and (
                    alias.asname or "." not in alias.name
                ):
                    replacements.append(
                        ast.Assign(
                            targets=[
                                ast.Name(
                                    id=alias.asname or alias.name,
                                    ctx=ast.Store(),
                                )
                            ],
                            value=ast.Name(
                                id=modules[alias.name], ctx=ast.Load()
                            ),
                        )
                    )
                else:
                    replacements.append(ast.Import(names=[alias]))
            return replacements

        def visit_ImportFrom(
            self, node: ast.ImportFrom
        ) -> ast.stmt | list[ast.stmt]:
            if (
                node.module == "matplotlib"
                and node.level == 0
                and all(alias.name == "pyplot" for alias in node.names)
            ):
                return [
                    ast.Assign(
                        targets=[
                            ast.Name(
                                id=alias.asname or alias.name, ctx=ast.Store()
                            )
                        ],
                        value=ast.Name(id="__galadril_pyplot", ctx=ast.Load()),
                    )
                    for alias in node.names
                ]
            if node.module in modules and node.level == 0:
                if any(
                    alias.name == "*" or alias.name.startswith("_")
                    for alias in node.names
                ):
                    raise ValueError("Use explicit public scientific imports")
                return [
                    ast.Assign(
                        targets=[
                            ast.Name(
                                id=alias.asname or alias.name, ctx=ast.Store()
                            )
                        ],
                        value=ast.Attribute(
                            value=ast.Name(
                                id=modules[node.module], ctx=ast.Load()
                            ),
                            attr=alias.name,
                            ctx=ast.Load(),
                        ),
                    )
                    for alias in node.names
                ]
            return node

    rewritten = ast.fix_missing_locations(Imports().visit(tree))
    return ast.unparse(rewritten), bindings
