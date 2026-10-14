"""A tool the assistant can call: a plain function plus the description the model reads.

`@tool` reads everything from the function itself. The parameters and their types become the
input schema, the docstring's first part the description, and its "Args:" lines the parameter
descriptions. Arguments from the model are checked against the types before the function runs.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, create_model


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., str]
    args_model: type[BaseModel]

    def call(self, args: dict[str, Any]) -> str:
        """Run with arguments as the model sent them. Raises ValueError when they do not fit."""
        checked = self.args_model(**args)
        return self.fn(**{name: getattr(checked, name) for name in type(checked).model_fields})


def _split_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """("what it does", {parameter: description}) from a docstring with an "Args:" section."""
    head, _, tail = inspect.cleandoc(doc).partition("\nArgs:")
    params: dict[str, str] = {}
    for line in tail.splitlines():
        name, sep, text = line.strip().partition(":")
        if sep and name.isidentifier():
            params[name] = text.strip()
    return head.strip(), params


def tool(fn: Callable[..., str]) -> Tool:
    description, param_docs = _split_docstring(fn.__doc__ or "")
    hints = get_type_hints(fn)
    fields: dict[str, Any] = {
        p.name: (hints[p.name], ... if p.default is inspect.Parameter.empty else p.default)
        for p in inspect.signature(fn).parameters.values()
    }
    args_model = create_model(fn.__name__, **fields)
    schema = args_model.model_json_schema()
    schema.pop("title", None)
    for name, prop in schema.get("properties", {}).items():
        prop.pop("title", None)
        if name in param_docs:
            prop["description"] = param_docs[name]
    return Tool(fn.__name__, description, schema, fn, args_model)
