"""Applies the operator's description overrides to the backend's items.

``DescriptionOverrides`` is a FastMCP transform on the ``ProxyProvider``: it sees
each of the backend's listings, and each lookup a client request makes, and
hands on copies whose description texts the overrides file changes. Names,
schemas and everything else pass through as the backend has them; within a
tool's input schema only an argument's ``description`` changes.

A ``prompts/get`` result carries the prompt's description too;
``PromptResultDescriptions`` applies the prompt's override to it, so a client
reads the same text in the listing and in the result. Neither adds a backend
call of its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp.prompts import Prompt, PromptResult
from fastmcp.prompts.base import InputRequiredPromptResult
from fastmcp.resources import Resource, ResourceTemplate
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms import (
    GetPromptNext,
    GetResourceNext,
    GetResourceTemplateNext,
    GetToolNext,
    Transform,
)
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.overrides import Overrides, TextOverride


class DescriptionOverrides(Transform):
    """Changes description texts as the overrides file says."""

    def __init__(self, overrides: Overrides) -> None:
        self._overrides = overrides

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._tool(tool) for tool in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return None if tool is None else self._tool(tool)

    async def list_resources(self, resources: Sequence[Resource]) -> Sequence[Resource]:
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


class PromptResultDescriptions(Middleware):
    """Applies a prompt's description override to its ``prompts/get`` result."""

    def __init__(self, overrides: Overrides) -> None:
        self._overrides = overrides

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mcp_types.GetPromptRequestParams],
        call_next: CallNext[mcp_types.GetPromptRequestParams, PromptResult],
    ) -> PromptResult:
        result = await call_next(context)
        override = self._overrides.prompts.get(context.message.name)
        if (
            override is None
            or override.description is None
            or isinstance(result, InputRequiredPromptResult)
        ):
            return result
        description = override.description.apply(result.description)
        return result.model_copy(update={"description": description})


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
