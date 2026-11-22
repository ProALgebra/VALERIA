"""Code-only VFlow node registry.

Only handlers shipped with VALERIA can be registered. The registry is also the
source of truth for the editor palette and inspector schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .capabilities import Capability, validate_capabilities


@dataclass(frozen=True)
class NodeSpec:
    type: str
    version: str
    title: str
    category: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    config_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    # DSL call names which compile to this type/version. Call names are pure
    # metadata: adding a block name never requires a parser change.
    call_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Capability declarations are part of the trusted code contract and
        # are validated before a spec can enter any registry or manifest.
        object.__setattr__(
            self,
            "capabilities",
            validate_capabilities(self.capabilities, type_id=self.type),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "version": self.version,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "config_schema": self.config_schema,
            "capabilities": list(self.capabilities),
            "call_names": list(self.call_names),
        }


TASK = "valeria.task"
TRANSFORM = "core.transform"
IF = "core.if"
FORK = "core.fork"
MERGE = "core.merge"

BUILTIN_SPECS = (
    NodeSpec(
        type=TASK,
        version="1.0",
        title="Задача VALERIA",
        category="task",
        description="Ставит существующую задачу VALERIA в общую очередь и ждёт результат.",
        inputs=("in",),
        outputs=("out",),
        call_names=("task.run",),
        config_schema={
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "input_path": {"type": "string", "default": "$"},
                "result_mode": {
                    "type": "string",
                    "enum": ["auto", "strict_json", "none"],
                    "default": "auto",
                },
                "queue_timeout_seconds": {"type": "integer", "minimum": 1},
                "execution_timeout_seconds": {"type": "integer", "minimum": 1},
                "retries": {"type": "integer", "minimum": 0, "maximum": 10, "default": 0},
                "json_schema": {
                    "type": "object",
                    "title": "Требуемый JSON",
                    "description": (
                        "JSON Schema для ответа при result_mode=strict_json. "
                        "Поддерживаются type, required, properties, items, enum, "
                        "minimum, maximum, minLength, maxLength, pattern и "
                        "additionalProperties: false."
                    ),
                },
                "max_json_repairs": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                    "default": 1,
                    "title": "Попыток исправления",
                    "description": (
                        "Сколько раз попросить задачу исправить невалидный JSON "
                        "в том же чате (0–3)."
                    ),
                },
            },
        },
        capabilities=(
            Capability.QUEUE,
            Capability.EXTERNAL_IO,
            Capability.WORKSPACE,
        ),
    ),
    NodeSpec(
        type=TRANSFORM,
        version="1.0",
        title="Transform",
        category="operator",
        description="Детерминированно изменяет JSON ограниченным набором операций.",
        inputs=("in",),
        outputs=("out",),
        config_schema={
            "type": "object",
            "required": ["operations"],
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {"type": "object"},
                    "minItems": 1,
                },
                "create_missing": {"type": "boolean", "default": False},
            },
        },
        capabilities=(Capability.PURE, Capability.DETERMINISTIC),
        call_names=("transform",),
    ),
    NodeSpec(
        type=IF,
        version="1.0",
        title="If",
        category="operator",
        description="Выбирает ровно один выход, не изменяя JSON.",
        inputs=("in",),
        outputs=("true", "false"),
        config_schema={
            "type": "object",
            "required": ["path", "operator"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "operator": {
                    "type": "string",
                    "enum": [
                        "eq", "ne", "gt", "ge", "lt", "le",
                        "contains", "in", "exists", "is_null",
                    ],
                },
                "value": {},
                "missing": {
                    "type": "string",
                    "enum": ["error", "false", "true"],
                    "default": "error",
                },
            },
        },
        capabilities=(Capability.PURE, Capability.DETERMINISTIC),
        call_names=("if",),
    ),
    NodeSpec(
        type=FORK,
        version="1.0",
        title="Fork N",
        category="operator",
        description="Атомарно создаёт N независимых веток и связывает их с Merge.",
        inputs=("in",),
        outputs=("branch", "all"),
        config_schema={
            "type": "object",
            "required": ["count", "join"],
            "properties": {
                "count": {"type": "integer", "minimum": 2, "maximum": 32},
                "join": {"type": "string", "minLength": 1},
            },
        },
        capabilities=(Capability.PURE, Capability.DETERMINISTIC),
        call_names=("fork",),
    ),
    NodeSpec(
        type=MERGE,
        version="1.0",
        title="Merge",
        category="operator",
        description="Ждёт все ветки конкретного Fork и создаёт одну продолжающую точку.",
        inputs=("in",),
        outputs=("out",),
        config_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["all"], "default": "all"}
            },
        },
        capabilities=(Capability.PURE, Capability.DETERMINISTIC),
        call_names=("merge",),
    ),
)


class NodeRegistry:
    def __init__(self, specs: tuple[NodeSpec, ...] = BUILTIN_SPECS):
        self._specs: dict[tuple[str, str], NodeSpec] = {}
        self._call_forms: dict[str, NodeSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: NodeSpec) -> None:
        key = (spec.type, spec.version)
        if key in self._specs:
            raise ValueError(f"VFlow handler already registered: {spec.type}@{spec.version}")
        call_names = tuple(spec.call_names)
        if len(set(call_names)) != len(call_names):
            raise ValueError(
                f"VFlow call forms contain duplicates for {spec.type}@{spec.version}"
            )
        for call_name in call_names:
            if not isinstance(call_name, str) or not call_name:
                raise ValueError("VFlow call form must be a non-empty string")
            existing = self._call_forms.get(call_name)
            if existing is not None:
                raise ValueError(
                    "VFlow call form already registered: "
                    f"{call_name} ({existing.type}@{existing.version})"
                )
        # Only mutate after every call name has been validated, so a failed
        # registration cannot leave a partially registered handler behind.
        for call_name in call_names:
            self._call_forms[call_name] = spec
        self._specs[key] = spec

    def register_call_form(
        self,
        call_name: str,
        type_id: str | NodeSpec,
        version: str = "1.0",
    ) -> NodeSpec:
        """Add a DSL call name for an already registered type.

        The target is either a registered ``type_id`` or a ``NodeSpec`` which
        is registered on demand. This keeps new block names metadata-only:
        neither the parser nor a hardcoded compiler map needs to change.
        """
        if not isinstance(call_name, str) or not call_name:
            raise ValueError("VFlow call form must be a non-empty string")
        existing = self._call_forms.get(call_name)
        if existing is not None:
            raise ValueError(
                "VFlow call form already registered: "
                f"{call_name} ({existing.type}@{existing.version})"
            )

        if isinstance(type_id, NodeSpec):
            if call_name in type_id.call_names:
                raise ValueError(
                    f"VFlow call form already registered: {call_name} "
                    f"({type_id.type}@{type_id.version})"
                )
            spec = self.get(type_id.type, type_id.version)
            if spec is None:
                self.register(type_id)
                spec = type_id
        else:
            spec = self.require(type_id, version)

        if call_name in spec.call_names:
            raise ValueError(
                f"VFlow call form already registered: {call_name} "
                f"({spec.type}@{spec.version})"
            )
        updated = replace(spec, call_names=(*spec.call_names, call_name))
        self._specs[(updated.type, updated.version)] = updated
        for form in updated.call_names:
            self._call_forms[form] = updated
        return updated

    def get(self, type_id: str, version: str = "1.0") -> NodeSpec | None:
        return self._specs.get((type_id, version))

    def resolve_call(self, call_name: str) -> NodeSpec | None:
        """Return the handler registered for a DSL call name, if any."""
        return self._call_forms.get(call_name)

    def require(self, type_id: str, version: str = "1.0") -> NodeSpec:
        spec = self.get(type_id, version)
        if spec is None:
            raise KeyError(f"Unknown VFlow node type: {type_id}@{version}")
        return spec

    def all(self) -> tuple[NodeSpec, ...]:
        return tuple(self._specs.values())


DEFAULT_REGISTRY = NodeRegistry()
