"""Fail when coverage, cyclomatic complexity or CRAP score of the package exceed their limits.

Usage: check_quality.py [coverage.json], run from the repository root after `pytest --cov --cov-report=json`.
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

import mccabe  # type: ignore[import-untyped]  # mccabe ships no type information

PACKAGE = Path("src/pve_ci_runners")
MIN_COVERAGE = 90.0
MAX_COMPLEXITY = 5
MAX_CRAP = 5.0


class Function(NamedTuple):
    """One measured function; `crap` is None when the coverage report does not know it."""

    where: str
    complexity: int
    crap: float | None


def crap(complexity: int, coverage: float) -> float:
    """Change Risk Anti-Patterns score of one function; coverage is a fraction from 0 to 1."""
    return complexity**2 * (1 - coverage) ** 3 + complexity


def function_coverage(summary: dict[str, int]) -> float:
    """Covered lines and branches over all lines and branches; 1 for a function with neither."""
    total = summary["num_statements"] + summary["num_branches"]
    if total == 0:
        return 1.0
    return (summary["covered_lines"] + summary["covered_branches"]) / total


def complexities(source: str) -> list[tuple[str, int, int]]:
    """Qualified name, first line and cyclomatic complexity of every function and method."""
    visitor = mccabe.PathGraphingAstVisitor()
    visitor.preorder(ast.parse(source), visitor)
    # Module-level statements get graphs too; only function graphs have a name that differs from the entity.
    return [(g.entity, g.lineno, g.complexity()) for g in visitor.graphs.values() if g.name != g.entity]


def find_summary(functions: dict[str, Any], name: str, line: int) -> dict[str, int] | None:
    """The coverage summary of a function, by qualified name or else by start line."""
    entry = functions.get(name) or next((f for f in functions.values() if f["start_line"] == line), None)
    return entry["summary"] if entry else None


def totals(files: dict[str, Any], package: Path = PACKAGE) -> dict[str, float]:
    """Line and branch coverage in percent, summed over the package files."""
    sums = {"covered_lines": 0, "num_statements": 0, "covered_branches": 0, "num_branches": 0}
    for path, data in files.items():
        if Path(path).is_relative_to(package):
            for key in sums:
                sums[key] += data["summary"][key]
    return {
        "line": 100 * sums["covered_lines"] / max(sums["num_statements"], 1),
        "branch": 100 * sums["covered_branches"] / max(sums["num_branches"], 1),
    }


def functions(files: dict[str, Any], package: Path = PACKAGE) -> list[Function]:
    """Complexity and CRAP score of every function in the package."""
    measured: list[Function] = []
    for path in sorted(package.rglob("*.py")):
        entries = files.get(str(path), {}).get("functions", {})
        for name, line, complexity in complexities(path.read_text(encoding="utf-8")):
            summary = find_summary(entries, name, line)
            score = None if summary is None else crap(complexity, function_coverage(summary))
            measured.append(Function(f"{path}:{line} {name}", complexity, score))
    return measured


def violations(coverage: dict[str, float], measured: list[Function]) -> list[str]:
    """One line per exceeded limit."""
    found = [
        f"total {kind} coverage {value:.2f} % < {MIN_COVERAGE} %"
        for kind, value in coverage.items()
        if value < MIN_COVERAGE
    ]
    found += [
        f"{f.where}: complexity {f.complexity} > {MAX_COMPLEXITY}"
        for f in measured
        if f.complexity > MAX_COMPLEXITY
    ]
    found += [f"{f.where}: no coverage data" for f in measured if f.crap is None]
    found += [
        f"{f.where}: CRAP {f.crap:.2f} > {MAX_CRAP}"
        for f in measured
        if f.crap is not None and f.crap > MAX_CRAP
    ]
    return found


def main(argv: list[str]) -> int:
    """Print the measured values and one line per violation; 1 when there is any."""
    report = Path(argv[1] if len(argv) > 1 else "coverage.json")
    files = json.loads(report.read_text(encoding="utf-8"))["files"]
    coverage, measured = totals(files), functions(files)
    most_complex = max(measured, key=lambda f: f.complexity)
    riskiest = max(measured, key=lambda f: f.crap or 0.0)
    print(
        f"line coverage {coverage['line']:.2f} %, branch coverage {coverage['branch']:.2f} %; "
        f"max complexity {most_complex.complexity} ({most_complex.where}); "
        f"max CRAP {riskiest.crap or 0.0:.2f} ({riskiest.where})"
    )
    found = violations(coverage, measured)
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
