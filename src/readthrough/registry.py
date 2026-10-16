"""Registries for the parts a user picks between: terminal launchers, study assistants, profile sources.

A plugin is a name, a one-line summary, an implementation, and a dataclass of options. The options a
user writes in ``config.toml`` (``[terminals.wt]``, say) are checked against that dataclass, so a typo in
an option name or a value of the wrong type is refused with a suggestion instead of being ignored. The
same goes for the plugin name itself.

Nothing here knows what a terminal or an assistant is; see ``terminals.py``, ``assistants.py``, and the
profile finders in ``profile.py``.
"""

from __future__ import annotations

import difflib
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar, cast, get_args, get_origin, get_type_hints

T = TypeVar("T")
Impl = TypeVar("Impl")  # the exact type of one registered implementation, so decorating a class keeps its type


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class NoOptions:
    """For plugins that take no options."""


@dataclass(frozen=True)
class Plugin(Generic[T]):
    name: str
    summary: str
    impl: T
    options: type[Any]


class Registry(Generic[T]):
    """An ordered, name-keyed set of plugins of one kind."""

    def __init__(self, what: str) -> None:
        self.what = what
        self._plugins: dict[str, Plugin[T]] = {}

    def add(self, name: str, summary: str, *, options: type[Any] = NoOptions) -> Callable[[Impl], Impl]:
        """Register the decorated implementation under ``name``. The decorated object comes back unchanged."""
        if not is_dataclass(options):
            raise TypeError(f"{self.what} {name!r}: options must be a dataclass")

        def register(impl: Impl) -> Impl:
            if name in self._plugins:
                raise RegistryError(f"{self.what} {name!r} is registered twice")
            self._plugins[name] = Plugin(name, summary, cast("T", impl), options)
            return impl

        return register

    def names(self) -> list[str]:
        return list(self._plugins)

    def __iter__(self) -> Iterator[Plugin[T]]:
        return iter(self._plugins.values())

    def __contains__(self, name: object) -> bool:
        return name in self._plugins

    def get(self, name: str) -> Plugin[T]:
        try:
            return self._plugins[name]
        except KeyError:
            raise RegistryError(f"unknown {self.what} {name!r}{_suggest(name, self._plugins)}") from None

    def options_for(self, name: str, raw: Mapping[str, Any] | None, *, where: str, base: Path | None = None) -> Any:
        """The plugin's options from a config table, validated. Missing options take the dataclass defaults.

        Relative paths are resolved against ``base`` (the config file's directory), so a shared config
        works from any working directory.
        """
        plugin = self.get(name)
        raw = dict(raw or {})
        declared = {f.name: f for f in fields(plugin.options)}
        unknown = set(raw) - set(declared)
        if unknown:
            key = min(unknown)
            raise RegistryError(f"{where}: unknown option {key!r} for {self.what} {name!r}{_suggest(key, declared)}")
        hints = get_type_hints(plugin.options)
        values = {key: _coerce(f"{where}.{key}", hints[key], value, base) for key, value in raw.items()}
        try:
            return plugin.options(**values)
        except TypeError as exc:  # a required option (no default) is missing
            raise RegistryError(f"{where}: {self.what} {name!r} needs {_missing(plugin.options, values)}") from exc

    def defaults_for(self, name: str) -> Any:
        """The plugin's options with nothing set."""
        return self.options_for(name, None, where=name)


def _missing(options: type[Any], values: dict[str, Any]) -> str:
    from dataclasses import MISSING

    needed = [f.name for f in fields(options) if f.default is MISSING and f.default_factory is MISSING]
    return ", ".join(n for n in needed if n not in values) or "a value"


def _suggest(name: str, options: Any) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _coerce(where: str, hint: Any, value: Any, base: Path | None) -> Any:
    """Check ``value`` against a simple type hint: str, int, float, bool, Path, list[...], or X | None."""
    if get_origin(hint) is types.UnionType or get_origin(hint) is _typing_union():
        members = [m for m in get_args(hint) if m is not type(None)]
        if value is None:
            return None
        return _coerce(where, members[0], value, base)
    if get_origin(hint) is list:
        (item,) = get_args(hint)
        if not isinstance(value, list):
            raise RegistryError(f"{where} must be a list, not {value!r}")
        return [_coerce(f"{where}[{i}]", item, v, base) for i, v in enumerate(value)]
    if hint is bool:
        if not isinstance(value, bool):
            raise RegistryError(f"{where} must be true or false, not {value!r}")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RegistryError(f"{where} must be a whole number, not {value!r}")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RegistryError(f"{where} must be a number, not {value!r}")
        return float(value)
    if hint is str:
        if not isinstance(value, str):
            raise RegistryError(f"{where} must be text, not {value!r}")
        return value
    if hint is Path:
        if not isinstance(value, str) or not value.strip():
            raise RegistryError(f"{where} must be a path, not {value!r}")
        path = Path(value).expanduser()
        return path if path.is_absolute() or base is None else (base / path).resolve()
    raise TypeError(f"{where}: unsupported option type {hint!r}")


def _typing_union() -> Any:
    import typing

    return typing.Union
