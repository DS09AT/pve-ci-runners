import pytest
from scripts.check_quality import Function, complexities, crap, find_summary, function_coverage, violations

FULL = {"line": 90.0, "branch": 90.0}


def summary(statements: int, covered: int, branches: int = 0, covered_branches: int = 0) -> dict[str, int]:
    return {
        "num_statements": statements,
        "covered_lines": covered,
        "num_branches": branches,
        "covered_branches": covered_branches,
    }


@pytest.mark.parametrize(
    ("complexity", "coverage", "score"),
    [(1, 0.0, 2.0), (5, 1.0, 5.0), (5, 0.0, 30.0), (4, 0.5, 6.0), (3, 0.5, 4.125)],
)
def test_crap_grows_with_complexity_squared_and_the_cube_of_what_is_untested(
    complexity: int, coverage: float, score: float
) -> None:
    assert crap(complexity, coverage) == pytest.approx(score)


def test_function_coverage_counts_lines_and_branches_together() -> None:
    assert function_coverage(summary(4, 3, 2, 1)) == pytest.approx(4 / 6)
    assert function_coverage(summary(0, 0)) == pytest.approx(1.0)


def test_complexity_is_per_function_and_method_and_skips_module_code() -> None:
    source = (
        "if True:\n    pass\n"
        "def f(x):\n    if x:\n        return 1\n    for _ in x:\n        pass\n    return 0\n"
        "class C:\n    def m(self):\n"
        "        try:\n            pass\n        except ValueError:\n            pass\n"
    )

    assert sorted(complexities(source)) == [("C.m", 10, 3), ("f", 3, 3)]


def test_coverage_entries_are_found_by_qualified_name_or_start_line() -> None:
    entries = {
        "C.m": {"start_line": 10, "summary": summary(1, 1)},
        "": {"start_line": 1, "summary": summary(2, 0)},
    }

    assert find_summary(entries, "C.m", 99) == summary(1, 1)
    assert find_summary(entries, "renamed", 10) == summary(1, 1)
    assert find_summary(entries, "g", 20) is None


def test_each_limit_is_inclusive_and_every_violation_is_its_own_line() -> None:
    assert violations(FULL, [Function("a.py:1 f", 5, 5.0)]) == []

    found = violations(
        {"line": 89.99, "branch": 90.0},
        [Function("a.py:1 f", 6, 6.0), Function("a.py:9 g", 1, None), Function("a.py:5 h", 4, 5.01)],
    )

    assert found == [
        "total line coverage 89.99 % < 90.0 %",
        "a.py:1 f: complexity 6 > 5",
        "a.py:9 g: no coverage data",
        "a.py:1 f: CRAP 6.00 > 5.0",
        "a.py:5 h: CRAP 5.01 > 5.0",
    ]
