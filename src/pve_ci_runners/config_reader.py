"""Typed, strict access to one table of the configuration file.

Every key a section offers must be read; whatever is left is an unknown key and an error, so a typo in
the configuration fails loudly instead of silently falling back to a default.
"""

import ipaddress
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Final, cast

from pve_ci_runners.errors import ConfigError


class _Missing:
    pass


MISSING: Final = _Missing()


class Section:
    """One TOML table, read key by key and then closed with `done`."""

    def __init__(self, data: Mapping[str, object], path: str) -> None:
        self._data = data
        self._path = path
        self._used: set[str] = set()

    @property
    def path(self) -> str:
        """The dotted location of this table, used in error messages."""
        return self._path

    def has(self, key: str) -> bool:
        """Whether the key is present."""
        return key in self._data

    def error(self, key: str, message: str) -> ConfigError:
        """An error that names the exact key."""
        return ConfigError(f"{self._where(key)}: {message}")

    def text(self, key: str, *, default: str | _Missing = MISSING, pattern: str | None = None) -> str:
        """A string, optionally restricted to a full-match pattern."""
        value = self._get(key, default)
        if not isinstance(value, str):
            raise self.error(key, "must be a string")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise self.error(key, f"{value!r} does not match {pattern}")
        return value

    def number(
        self, key: str, *, default: int | _Missing = MISSING, minimum: int = 0, maximum: int = 2**53
    ) -> int:
        """An integer within the given closed range."""
        value = self._get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.error(key, "must be an integer")
        if not minimum <= value <= maximum:
            raise self.error(key, f"must be between {minimum} and {maximum}")
        return value

    def flag(self, key: str, *, default: bool | _Missing = MISSING) -> bool:
        """A boolean."""
        value = self._get(key, default)
        if not isinstance(value, bool):
            raise self.error(key, "must be true or false")
        return value

    def texts(
        self,
        key: str,
        *,
        default: tuple[str, ...] | _Missing = MISSING,
        pattern: str | None = None,
        minimum: int = 0,
        maximum: int = 1000,
    ) -> tuple[str, ...]:
        """A list of strings, each optionally restricted to a pattern; duplicates are an error."""
        value = self._get(key, default)
        if isinstance(value, tuple):
            items = cast("tuple[object, ...]", value)
        elif isinstance(value, list):
            items = tuple(cast("list[object]", value))
        else:
            raise self.error(key, "must be a list of strings")
        if not minimum <= len(items) <= maximum:
            raise self.error(key, f"must hold between {minimum} and {maximum} entries")
        result: list[str] = []
        for item in items:
            if not isinstance(item, str):
                raise self.error(key, "must be a list of strings")
            if pattern is not None and re.fullmatch(pattern, item) is None:
                raise self.error(key, f"{item!r} does not match {pattern}")
            if item in result:
                raise self.error(key, f"lists {item!r} twice")
            result.append(item)
        return tuple(result)

    def numbers(self, key: str, *, count: int, minimum: int = 0, maximum: int = 2**53) -> tuple[int, ...]:
        """A list of exactly `count` distinct integers within the given closed range."""
        value = self._get(key, MISSING)
        if not isinstance(value, list):
            raise self.error(key, f"must be a list of {count} integers")
        items = cast("list[object]", value)
        if len(items) != count:
            raise self.error(key, f"must be a list of {count} integers")
        result: list[int] = []
        for item in items:
            if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
                raise self.error(key, f"entries must be integers between {minimum} and {maximum}")
            if item in result:
                raise self.error(key, f"lists {item} twice")
            result.append(item)
        return tuple(result)

    def ipv4(self, key: str, *, default: str | _Missing = MISSING) -> ipaddress.IPv4Address:
        """A single IPv4 address."""
        raw = self.text(key, default=default)
        try:
            return ipaddress.IPv4Address(raw)
        except ValueError:
            raise self.error(key, f"{raw!r} is not an IPv4 address") from None

    def absolute_path(self, key: str, *, default: str | _Missing = MISSING) -> PurePosixPath:
        """An absolute, normalized POSIX path without `..` components."""
        raw = self.text(key, default=default)
        path = PurePosixPath(raw)
        if not path.is_absolute() or ".." in path.parts or str(path) != raw:
            raise self.error(key, f"{raw!r} must be an absolute path without '..'")
        return path

    def section(self, key: str) -> "Section | None":
        """A nested table, or None when the key is absent."""
        if key not in self._data:
            self._used.add(key)
            return None
        value = self._get(key, MISSING)
        if not isinstance(value, dict):
            raise self.error(key, "must be a table")
        return Section(cast("dict[str, object]", value), self._where(key))

    def sections(self, key: str, *, minimum: int = 0) -> list["Section"]:
        """An array of tables (`[[key]]`)."""
        value = self._get(key, [])
        if not isinstance(value, list):
            raise self.error(key, "must be an array of tables")
        entries = cast("list[object]", value)
        if len(entries) < minimum:
            raise self.error(key, f"needs at least {minimum} entries")
        tables: list[Section] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise self.error(key, "must be an array of tables")
            tables.append(Section(cast("dict[str, object]", entry), f"{self._where(key)}[{index}]"))
        return tables

    def scalars(self, key: str, *, key_pattern: str) -> dict[str, str | int]:
        """An inline table of string or integer values with restricted keys."""
        section = self.section(key)
        if section is None:
            return {}
        result: dict[str, str | int] = {}
        for name, value in section.raw_items():
            if re.fullmatch(key_pattern, name) is None:
                raise section.error(name, f"is not an allowed key ({key_pattern})")
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise section.error(name, "must be a string or an integer")
            result[name] = value
        return result

    def raw_items(self) -> list[tuple[str, object]]:
        """All entries, marked as read; for tables whose keys are data, not schema."""
        self._used.update(self._data)
        return list(self._data.items())

    def done(self) -> None:
        """Rejects every key that was never read."""
        unknown = sorted(set(self._data) - self._used)
        if unknown:
            raise ConfigError(f"{self._path}: unknown key(s) {', '.join(unknown)}")

    def _get(self, key: str, default: object) -> object:
        self._used.add(key)
        if key in self._data:
            return self._data[key]
        if isinstance(default, _Missing):
            raise self.error(key, "is required")
        return default

    def _where(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key
