"""FastMCP's extensions to a backend config entry: what the proxy applies.

FastMCP defines three keys beyond a plain ``mcpServers`` entry, and applies
them through a nested in-process proxy around the real transport. The proxy
never builds that nested proxy (``client.py`` builds the transport from the
plain entry type), so the transport settings are the same with or without
them. It reads the keys here instead, at startup:

- ``tools`` is honoured in part. A tool's ``description`` and its arguments'
  ``description`` become text overrides (``replace``), applied as the overrides
  file's are. ``enabled: false`` becomes an exposure-control deny entry:
  FastMCP's own handling only drops the tool from listings and leaves it
  callable. Every other field changes a name, a schema or metadata, and is
  refused with an error naming it and why.
- ``include_tags`` and ``exclude_tags`` are refused, whatever their value.
  Exposure control already decides what is exposed, by exact name; tag
  filtering would be a second way to do the same, depending on metadata the
  backend controls.

Where the overrides file and the config both set a description, the file's
entry applies (``combine_overrides``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp.mcp_config import MCPConfig
from pydantic import BaseModel

from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.exposure import BACKEND_STATUS_TOOL_NAME
from mcp_call_orchestrator_proxy.overrides import ItemOverride, Overrides, TextOverride

_TAG_KEYS = ("include_tags", "exclude_tags")

_METADATA = (
    "it changes the tool's metadata, which the proxy passes on as the backend has it"
)
_REFUSED_TOOL_FIELDS = {
    "name": "it renames the tool; the proxy keeps the backend's names",
    "title": "only descriptions can be overridden; the title stays the backend's",
    "version": _METADATA,
    "tags": _METADATA,
    "meta": _METADATA,
}
_REFUSED_ARGUMENT_FIELDS = {
    "name": "it renames the argument, which changes the tool's schema",
    "hide": "it hides the argument, which changes the tool's schema",
    "default": "it gives the argument a default, which changes the tool's schema",
    "required": "it changes which arguments the tool's schema requires",
    "examples": "it adds examples to the tool's schema",
}


@dataclass(frozen=True, slots=True)
class ConfigExtensions:
    """What the backend config's ``tools`` key asks for, in the proxy's terms."""

    overrides: Overrides = field(default_factory=Overrides)
    disabled_tools: frozenset[str] = frozenset()

    def summary(self) -> str:
        """What the ``tools`` key applies, for the startup log."""
        tools = self.overrides.tools
        descriptions = sum(item.description is not None for item in tools.values())
        arguments = sum(len(item.arguments) for item in tools.values())
        disabled = ", ".join(sorted(self.disabled_tools)) or "none"
        return (
            f"tool descriptions {descriptions}, argument descriptions {arguments}, "
            f"disabled tools: {disabled}"
        )


def read_config_extensions(settings: ProxySettings) -> ConfigExtensions | None:
    """Translate the backend entry's FastMCP keys, or refuse the entry.

    ``None`` when there is no backend config or its entry has no ``tools`` key.
    Raises ``ValueError`` naming every key the proxy does not apply, and why.
    """
    if not settings.backend_config:
        return None
    path = settings.backend_config
    config = MCPConfig.from_file(Path(path))
    name, server = next(iter(config.mcpServers.items()))
    given = {key: getattr(server, key) for key in server.model_fields_set}
    given.update(server.model_extra or {})

    refusals = [
        f"{key}: not supported; use exposure control (MCP_PROXY_TOOL_ALLOW / "
        "MCP_PROXY_TOOL_DENY) to choose which tools are exposed"
        for key in _TAG_KEYS
        if key in given
    ]
    extensions: ConfigExtensions | None = None
    if "tools" in given:
        extensions = _translate_tools(given["tools"], refusals)
    if refusals:
        listed = "".join(f"\n- {refusal}" for refusal in refusals)
        raise ValueError(
            f"Backend config {path!r}: server {name!r} sets keys this proxy does "
            f"not apply:{listed}\nRemove them from the entry."
        )
    return extensions


def _translate_tools(tools: Any, refusals: list[str]) -> ConfigExtensions:
    if not isinstance(tools, Mapping):
        refusals.append("tools: must map tool names to their settings")
        return ConfigExtensions()
    items: dict[str, ItemOverride] = {}
    disabled: set[str] = set()
    for tool, settings in tools.items():
        where = f"tools.{tool}"
        values = _as_dict(settings)
        if values is None:
            refusals.append(f"{where}: must be an object")
            continue
        if tool == BACKEND_STATUS_TOOL_NAME:
            refusals.append(f"{where}: the proxy's own tool, not the backend's")
            continue
        description: TextOverride | None = None
        arguments: dict[str, TextOverride] = {}
        for key, value in values.items():
            if key == "description":
                description = _replace(f"{where}.description", value, refusals)
            elif key == "enabled":
                if not isinstance(value, bool):
                    refusals.append(f"{where}.enabled: must be true or false")
                elif not value:
                    disabled.add(tool)
            elif key == "arguments":
                arguments = _translate_arguments(where, value, refusals)
            else:
                reason = _REFUSED_TOOL_FIELDS.get(key, "not a field the proxy applies")
                refusals.append(f"{where}.{key}: {reason}")
        if description is not None or arguments:
            items[tool] = ItemOverride(description=description, arguments=arguments)
    return ConfigExtensions(
        overrides=Overrides(tools=items), disabled_tools=frozenset(disabled)
    )


def _translate_arguments(
    where: str, arguments: Any, refusals: list[str]
) -> dict[str, TextOverride]:
    if not isinstance(arguments, Mapping):
        refusals.append(f"{where}.arguments: must map argument names to settings")
        return {}
    translated: dict[str, TextOverride] = {}
    for argument, settings in arguments.items():
        at = f"{where}.arguments.{argument}"
        values = _as_dict(settings)
        if values is None:
            refusals.append(f"{at}: must be an object")
            continue
        for key, value in values.items():
            if key == "description":
                override = _replace(f"{at}.description", value, refusals)
                if override is not None:
                    translated[argument] = override
            else:
                reason = _REFUSED_ARGUMENT_FIELDS.get(
                    key, "not a field the proxy applies"
                )
                refusals.append(f"{at}.{key}: {reason}")
    return translated


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_unset=True)
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _replace(where: str, value: Any, refusals: list[str]) -> TextOverride | None:
    if value is None:
        return None
    if not isinstance(value, str):
        refusals.append(f"{where}: must be a string")
        return None
    return TextOverride(replace=value)


def combine_overrides(
    file: Overrides | None, config: Overrides
) -> tuple[Overrides | None, list[str]]:
    """The overrides in effect, and one conflict per text both sources set.

    For a text both set, the file's entry applies to the backend's own text and
    the config's is dropped. ``None`` when neither sets a text, so nothing is
    applied or checked.
    """
    if file is None:
        return (config if config.tools else None), []
    conflicts: list[str] = []
    tools = dict(file.tools)
    for name, from_config in config.tools.items():
        from_file = file.tools.get(name)
        if from_file is None:
            tools[name] = from_config
            continue
        description = from_file.description
        if from_config.description is not None:
            if description is not None:
                conflicts.append(f"tools.{name}.description")
            else:
                description = from_config.description
        arguments = dict(from_config.arguments)
        for argument, override in from_file.arguments.items():
            if argument in arguments:
                conflicts.append(f"tools.{name}.arguments.{argument}")
            arguments[argument] = override
        tools[name] = ItemOverride(description=description, arguments=arguments)
    return file.model_copy(update={"tools": tools}), conflicts
