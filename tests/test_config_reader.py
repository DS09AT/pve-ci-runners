from collections.abc import Callable

import pytest

from pve_ci_runners.config_reader import Section
from pve_ci_runners.errors import ConfigError


@pytest.mark.parametrize(
    ("value", "read", "message"),
    [
        (1, lambda s: s.text("k"), "t.k: must be a string"),
        ("a", lambda s: s.texts("k"), "t.k: must be a list of strings"),
        (["a", 1], lambda s: s.texts("k"), "t.k: must be a list of strings"),
        (["a", "B"], lambda s: s.texts("k", pattern="[a-z]"), "t.k: 'B' does not match"),
        (["a", "a"], lambda s: s.texts("k"), "t.k: lists 'a' twice"),
        (["a", "b"], lambda s: s.texts("k", maximum=1), "t.k: must hold between 0 and 1 entries"),
        ("1,2", lambda s: s.numbers("k", count=2), "t.k: must be a list of 2 integers"),
        ([1], lambda s: s.numbers("k", count=2), "t.k: must be a list of 2 integers"),
        ([1, True], lambda s: s.numbers("k", count=2), "t.k: entries must be integers between"),
        (
            [1, 9],
            lambda s: s.numbers("k", count=2, maximum=5),
            "t.k: entries must be integers between 0 and 5",
        ),
        ([3, 3], lambda s: s.numbers("k", count=2), "t.k: lists 3 twice"),
        ("192.0.2.300", lambda s: s.ipv4("k"), "t.k: '192.0.2.300' is not an IPv4 address"),
        ("/a/../b", lambda s: s.absolute_path("k"), "must be an absolute path"),
        ("a/b", lambda s: s.absolute_path("k"), "must be an absolute path"),
        ("/a//b", lambda s: s.absolute_path("k"), "must be an absolute path"),
        ([], lambda s: s.section("k"), "t.k: must be a table"),
        ({}, lambda s: s.sections("k"), "t.k: must be an array of tables"),
        ([{}], lambda s: s.sections("k", minimum=2), "t.k: needs at least 2 entries"),
        ([{}, 1], lambda s: s.sections("k"), "t.k: must be an array of tables"),
        (
            {"bad": 1},
            lambda s: s.scalars("k", key_pattern="good"),
            r"t.k.bad: is not an allowed key \(good\)",
        ),
        (
            {"good": True},
            lambda s: s.scalars("k", key_pattern="good"),
            "t.k.good: must be a string or an integer",
        ),
    ],
)
def test_each_reader_rejects_values_of_the_wrong_shape(
    value: object, read: Callable[[Section], object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        read(Section({"k": value}, "t"))


def test_readers_return_typed_values_and_defaults() -> None:
    section = Section({"names": ["a", "b"], "ids": [2, 1], "tables": [{"x": 1}, {}]}, "t")

    assert section.texts("names") == ("a", "b")
    assert section.texts("more", default=("c",)) == ("c",)
    assert section.numbers("ids", count=2) == (2, 1)
    tables = section.sections("tables")
    assert [t.error("x", "bad").args[0] for t in tables] == ["t.tables[0].x: bad", "t.tables[1].x: bad"]
    assert section.section("nested") is None
    assert section.scalars("options", key_pattern="x") == {}
    section.done()


def test_a_required_key_and_an_unread_key_are_errors() -> None:
    with pytest.raises(ConfigError, match=r"^k: is required$"):
        Section({}, "").text("k")
    with pytest.raises(ConfigError, match=r"t: unknown key\(s\) a, b"):
        Section({"b": 1, "a": 2}, "t").done()
