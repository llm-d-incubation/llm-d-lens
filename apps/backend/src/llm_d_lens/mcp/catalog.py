"""Build MCP tools from the canonical API schema and explicit composite definitions."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path


def definitions() -> dict:
    return json.loads(Path(__file__).with_name("definitions.json").read_text())


def resolve(schema: dict, components: dict) -> dict:
    return components["schemas"][schema["$ref"].rsplit("/", 1)[-1]] if "$ref" in schema else schema


def input_schema(schema: dict, components: dict, seen=frozenset()) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return {}
        return input_schema(resolve(schema, components), components, seen | {name})
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        non_null = [item for item in alternatives if item.get("type") != "null"]
        if len(non_null) != 1:
            return {}
        result = input_schema(non_null[0], components, seen)
        return {"anyOf": [result, {"type": "null"}]} if len(non_null) != len(alternatives) else result
    kind = schema.get("type")
    result = {}
    if kind in {"string", "number", "integer", "boolean"}:
        result["type"] = kind
        if kind == "string" and "enum" in schema:
            result["enum"] = [str(value) for value in schema["enum"]]
        if kind == "integer":
            result.update(minimum=-(2**53 - 1), maximum=2**53 - 1)
    elif kind == "array":
        result = {"type": "array", "items": input_schema(schema.get("items", {}), components, seen)}
    elif kind == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        result = {"type": "object"}
        if properties:
            result["properties"] = {}
            for key, value in properties.items():
                field = input_schema(value, components, seen)
                description = resolve(value, components).get("description")
                if description:
                    field["description"] = description
                result["properties"][key] = field
            if schema.get("required"):
                result["required"] = schema["required"]
            result["additionalProperties"] = {}
        else:
            result.update(propertyNames={"type": "string"}, additionalProperties={})
    return {"anyOf": [result, {"type": "null"}]} if schema.get("nullable") else result


def strict_schema(schema: dict, components: dict, seen=frozenset()) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return {}
        return strict_schema(resolve(schema, components), components, seen | {name})
    return {
        key: (
            strict_schema(value, components, seen)
            if isinstance(value, dict)
            else [strict_schema(item, components, seen) if isinstance(item, dict) else item for item in value]
            if isinstance(value, list)
            else value
        )
        for key, value in schema.items()
    }


def generate_catalog(spec: dict) -> list[dict]:
    config = definitions()
    tools = deepcopy(config["specialTools"])
    names = {tool["name"] for tool in tools}
    components = spec.get("components", {})
    skipped = []
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method not in config["methodRiskTiers"]:
                continue
            name = operation.get("operationId", "")
            invalid = (
                not name
                or name.endswith("_" + method)
                or not operation.get("summary")
                or not operation.get("description")
                or name in names
            )
            if invalid:
                if f"{method.upper()} {path}" not in config["intentionalSkips"]:
                    skipped.append(f"{method.upper()} {path}: {name}")
                continue
            names.add(name)
            properties, required = {}, []
            parameters = [parameter for parameter in operation.get("parameters", []) if parameter["in"] == "path"]
            parameters += [parameter for parameter in operation.get("parameters", []) if parameter["in"] == "query"]
            for parameter in parameters:
                key = parameter["name"]
                properties[key] = input_schema(parameter.get("schema", {}), components)
                description = parameter.get("description") or parameter.get("schema", {}).get("description")
                if description:
                    properties[key]["description"] = description
                if parameter.get("required"):
                    required.append(key)
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            body = resolve(body, components) if body else None
            body_fields = []
            if body and body.get("properties"):
                for key, value in body["properties"].items():
                    if key not in properties:
                        properties[key] = input_schema(value, components)
                        description = resolve(value, components).get("description")
                        if description:
                            properties[key]["description"] = description
                        if key in body.get("required", []):
                            required.append(key)
                    body_fields.append(key)
            elif body:
                properties["body"] = {"description": "Request body for this operation."}
                required.append("body")
                body_fields.append("body")
            schema = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            strict = name == "query_public_benchmark_rag"
            response_schema = None
            if strict:
                if method != "post" or path != "/api/v1/benchmark-rag/query":
                    raise ValueError("query_public_benchmark_rag must be POST /api/v1/benchmark-rag/query")
                response_schema = (
                    operation.get("responses", {})
                    .get("200", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if not response_schema:
                    raise ValueError("query_public_benchmark_rag requires a JSON 200 response schema")
                if not body:
                    raise ValueError("query_public_benchmark_rag requires a JSON request schema")
                schema = strict_schema(body, components)
            tools.append(
                {
                    "name": name,
                    "description": operation["description"],
                    "riskTier": config["riskTierOverrides"].get(name, config["methodRiskTiers"][method]),
                    "inputSchema": schema,
                    "route": path,
                    "method": method.upper(),
                    "bodyFields": body_fields,
                    "queryFields": [parameter["name"] for parameter in parameters if parameter["in"] == "query"],
                    **({"responseSchema": strict_schema(response_schema, components)} if strict else {}),
                }
            )
    if skipped:
        raise ValueError("Unexpected MCP operations without explicit metadata/exclusions: " + "; ".join(skipped))
    return tools


def catalog(app) -> list[dict]:
    if not hasattr(app.state, "mcp_catalog"):
        app.state.mcp_catalog = generate_catalog(app.openapi())
    return app.state.mcp_catalog


def render_path(path: str, arguments: dict) -> str:
    from urllib.parse import quote

    return re.sub(r"\{([^}]+)\}", lambda match: quote(str(arguments[match[1]]), safe=""), path)


if __name__ == "__main__":
    from llm_d_lens.api.main import app

    print(json.dumps(catalog(app)))
