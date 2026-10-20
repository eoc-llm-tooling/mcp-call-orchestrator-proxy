"""The operator's description overrides: file schema, loading and text operations.

An overrides file (``MCP_PROXY_OVERRIDES``, JSON) changes what agents are told
about the backend's items and the server as a whole: the descriptions of tools,
tool arguments, prompts, prompt arguments, resources and resource templates,
and the server's ``instructions``. It never changes a name or the structure of
a schema. Each text is changed by ``replace``, or by ``prepend`` and/or
``append``, which join the operator's text to the backend's with a blank line.

Loading is pure apart from reading the file: a file the proxy cannot honour
raises ``ValueError`` at startup, and names the backend does not have are
reported once the backend has been listed (``DescriptionOverrides``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mcp_call_orchestrator_proxy.exposure import BACKEND_STATUS_TOOL_NAME

TEXT_SEPARATOR = "\n\n"


class TextOverride(BaseModel):
    """One change to one text: ``replace`` it, or ``prepend`` and/or ``append``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replace: str | None = None
    prepend: str | None = None
    append: str | None = None

    @model_validator(mode="after")
    def _one_operation(self) -> TextOverride:
        if self.replace is not None and (
            self.prepend is not None or self.append is not None
        ):
            raise ValueError("'replace' cannot be combined with 'prepend' or 'append'")
        if self.replace is None and self.prepend is None and self.append is None:
            raise ValueError("give 'replace', 'prepend' or 'append'")
        return self

    def apply(self, text: str | None) -> str:
        """The text a client sees in place of ``text``, the backend's own.

        Where the backend has no text, a ``prepend`` or ``append`` stands alone.
        """
        if self.replace is not None:
            return self.replace
        parts = (self.prepend, text, self.append)
        return TEXT_SEPARATOR.join(part for part in parts if part)


class ItemOverride(BaseModel):
    """Overrides for a tool or a prompt: its description and its arguments'."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: TextOverride | None = None
    arguments: dict[str, TextOverride] = Field(default_factory=dict)


class ResourceOverride(BaseModel):
    """Overrides for a resource or a resource template: its description."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: TextOverride | None = None


class Overrides(BaseModel):
    """The whole overrides file. Each section is keyed by the backend's own name.

    ``resources`` covers resources (by URI) and resource templates (by
    ``uriTemplate``) in one section, as exposure control does.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: TextOverride | None = None
    tools: dict[str, ItemOverride] = Field(default_factory=dict)
    prompts: dict[str, ItemOverride] = Field(default_factory=dict)
    resources: dict[str, ResourceOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _backend_items_only(self) -> Overrides:
        if BACKEND_STATUS_TOOL_NAME in self.tools:
            raise ValueError(
                f"tools.{BACKEND_STATUS_TOOL_NAME}: the proxy's own tool, not the "
                "backend's; overrides apply to the backend's items only"
            )
        return self

    def summary(self) -> str:
        """What the file overrides, counted per kind of item, for the startup log."""

        def arguments(items: Mapping[str, ItemOverride]) -> int:
            return sum(len(item.arguments) for item in items.values())

        instructions = "yes" if self.instructions is not None else "no"
        return (
            f"tools {len(self.tools)} (arguments {arguments(self.tools)}), "
            f"prompts {len(self.prompts)} (arguments {arguments(self.prompts)}), "
            f"resources {len(self.resources)}, instructions {instructions}"
        )


@dataclass(frozen=True, slots=True)
class LoadedOverrides:
    """An overrides file as loaded, with the SHA-256 of its bytes."""

    overrides: Overrides
    sha256: str


def load_overrides(path: str) -> LoadedOverrides:
    """Read and validate the overrides file at ``path``.

    Raises ``ValueError`` naming the file for one that cannot be read, is not
    JSON or does not match the schema: the proxy cannot honour it, so it does
    not start on it.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"Overrides file {path!r} cannot be read: {error}") from error
    try:
        overrides = Overrides.model_validate_json(raw)
    except ValidationError as error:
        raise ValueError(f"Overrides file {path!r} is not valid: {error}") from error
    return LoadedOverrides(overrides=overrides, sha256=hashlib.sha256(raw).hexdigest())
