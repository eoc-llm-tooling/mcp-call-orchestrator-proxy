"""Applies the operator's description overrides to the backend's items.

``DescriptionOverrides`` is a FastMCP transform on the ``ProxyProvider``: it sees
each of the backend's listings, and each lookup a client request makes, and
hands on copies whose description texts the overrides file changes. Names,
schemas and everything else pass through as the backend has them; within a
tool's input schema only an argument's ``description`` changes.

On the first successful listing of each surface it reports, once per process,
each name in the file the backend does not have, at ``WARNING``: only the
operator can correct the file. Resources are checked against the union of
resource URIs and template ``uriTemplate``s, once both listings have succeeded.
It adds no backend call of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastmcp.prompts import Prompt
from fastmcp.resources import Resource, ResourceTemplate
from fastmcp.server.transforms import (
    GetPromptNext,
    GetResourceNext,
    GetResourceTemplateNext,
    GetToolNext,
    Transform,
)
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec

from mcp_call_orchestrator_proxy.overrides import Overrides, TextOverride

logger = logging.getLogger(__name__)


class DescriptionOverrides(Transform):
    """Changes description texts as the overrides file says, and audits its names."""

    def __init__(self, overrides: Overrides) -> None:
        self._overrides = overrides
        self._reported: set[str] = set()
        # None = that listing has not succeeded yet (the resource check waits for both).
        self._resource_uris: set[str] | None = None
        self._template_uris: set[str] | None = None

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        listed = {tool.name: _argument_names(tool.parameters) for tool in tools}
        self._audit_items("tool", self._overrides.tools, listed)
        return [self._tool(tool) for tool in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return None if tool is None else self._tool(tool)

    async def list_resources(self, resources: Sequence[Resource]) -> Sequence[Resource]:
        self._resource_uris = {str(resource.uri) for resource in resources}
        self._audit_resources()
        return [self._resource(resource, str(resource.uri)) for resource in resources]

    async def get_resource(
        self,
        uri: str,
        call_next: GetResourceNext,
        *,
        version: VersionSpec | None = None,
    ) -> Resource | None:
        resource = await call_next(uri, version=version)
        return None if resource is None else self._resource(resource, str(resource.uri))

    async def list_resource_templates(
        self, templates: Sequence[ResourceTemplate]
    ) -> Sequence[ResourceTemplate]:
        self._template_uris = {template.uri_template for template in templates}
        self._audit_resources()
        return [
            self._resource(template, template.uri_template) for template in templates
        ]

    async def get_resource_template(
        self,
        uri: str,
        call_next: GetResourceTemplateNext,
        *,
        version: VersionSpec | None = None,
    ) -> ResourceTemplate | None:
        template = await call_next(uri, version=version)
        if template is None:
            return None
        return self._resource(template, template.uri_template)

    async def list_prompts(self, prompts: Sequence[Prompt]) -> Sequence[Prompt]:
        listed = {
            prompt.name: {argument.name for argument in prompt.arguments or ()}
            for prompt in prompts
        }
        self._audit_items("prompt", self._overrides.prompts, listed)
        return [self._prompt(prompt) for prompt in prompts]

    async def get_prompt(
        self, name: str, call_next: GetPromptNext, *, version: VersionSpec | None = None
    ) -> Prompt | None:
        prompt = await call_next(name, version=version)
        return None if prompt is None else self._prompt(prompt)

    def _tool(self, tool: Tool) -> Tool:
        override = self._overrides.tools.get(tool.name)
        if override is None:
            return tool
        update: dict[str, Any] = {}
        if override.description is not None:
            update["description"] = override.description.apply(tool.description)
        if override.arguments:
            update["parameters"] = _with_argument_descriptions(
                tool.parameters, override.arguments
            )
        return tool.model_copy(update=update)

    def _prompt(self, prompt: Prompt) -> Prompt:
        override = self._overrides.prompts.get(prompt.name)
        if override is None:
            return prompt
        update: dict[str, Any] = {}
        if override.description is not None:
            update["description"] = override.description.apply(prompt.description)
        if override.arguments and prompt.arguments:
            update["arguments"] = [
                argument.model_copy(
                    update={
                        "description": override.arguments[argument.name].apply(
                            argument.description
                        )
                    }
                )
                if argument.name in override.arguments
                else argument
                for argument in prompt.arguments
            ]
        return prompt.model_copy(update=update)

    def _resource[R: (Resource, ResourceTemplate)](self, item: R, key: str) -> R:
        override = self._overrides.resources.get(key)
        if override is None or override.description is None:
            return item
        description = override.description.apply(item.description)
        return item.model_copy(update={"description": description})

    def _audit_items(
        self,
        kind: str,
        overrides: Mapping[str, Any],
        listed: Mapping[str, set[str]],
    ) -> None:
        for name in sorted(overrides):
            if name not in listed:
                self._report(f"{kind} {name!r} not found on backend")
                continue
            arguments: Iterable[str] = overrides[name].arguments
            for argument in sorted(set(arguments) - listed[name]):
                self._report(f"{kind} {name!r} has no argument {argument!r} on backend")

    def _audit_resources(self) -> None:
        if self._resource_uris is None or self._template_uris is None:
            return
        listed = self._resource_uris | self._template_uris
        for name in sorted(set(self._overrides.resources) - listed):
            self._report(f"resource {name!r} not found on backend")

    def _report(self, finding: str) -> None:
        if finding in self._reported:
            return
        self._reported.add(finding)
        logger.warning("overrides: %s", finding)


def _argument_names(schema: Mapping[str, Any]) -> set[str]:
    properties = schema.get("properties")
    return set(properties) if isinstance(properties, Mapping) else set()


def _with_argument_descriptions(
    schema: dict[str, Any], arguments: Mapping[str, TextOverride]
) -> dict[str, Any]:
    """``schema`` with each named top-level property's ``description`` changed.

    A copy: the schema the provider caches stays the backend's own. Only the
    ``description`` key of a property the schema has is set; nothing is added
    or removed elsewhere.
    """
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return schema
    changed = dict(properties)
    for name, override in arguments.items():
        prop = properties.get(name)
        if isinstance(prop, Mapping):
            changed[name] = {
                **prop,
                "description": override.apply(prop.get("description")),
            }
    return {**schema, "properties": changed}
