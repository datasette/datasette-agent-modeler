"""Validation and edit operations for 3D model documents.

A model document looks like:

    {
        "name": "Rocket ship",
        "background": "#e8ecf1",
        "objects": [
            {
                "id": "body",
                "type": "cylinder",
                "params": {"radius_top": 1, "radius_bottom": 1, "height": 4},
                "position": [0, 2, 0],
                "rotation": [0, 0, 0],
                "scale": [1, 1, 1],
                "color": "#cc3344",
                "opacity": 1.0
            }
        ]
    }

Rotation is Euler XYZ in degrees. Y is up.
"""

import copy
import re

PRIMITIVES = {
    "box": {"width": 1.0, "height": 1.0, "depth": 1.0},
    "sphere": {"radius": 0.5},
    "cylinder": {"radius_top": 0.5, "radius_bottom": 0.5, "height": 1.0},
    "cone": {"radius": 0.5, "height": 1.0},
    "torus": {"radius": 0.5, "tube": 0.2},
    "capsule": {"radius": 0.5, "length": 1.0},
    "plane": {"width": 1.0, "height": 1.0},
}

OBJECT_FIELDS = {"id", "type", "params", "position", "rotation", "scale", "color", "opacity"}

DEFAULT_COLOR = "#8899aa"

_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ModelError(Exception):
    """Raised for any invalid document or operation - message is LLM-facing."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_vector(value, field, object_id, default):
    if value is None:
        return list(default)
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or not all(_is_number(v) for v in value)
    ):
        raise ModelError(
            f"Object '{object_id}': {field} must be a list of three numbers [x, y, z]"
        )
    return [float(v) for v in value]


def _validate_color(value, context):
    if not isinstance(value, str) or not _COLOR_RE.match(value):
        raise ModelError(
            f"{context}: color must be a hex string like '#cc3344', got {value!r}"
        )
    return value.lower()


def validate_object(obj, existing_ids, auto_id_counts=None):
    """Validate and normalize a single object definition.

    existing_ids is the set of ids already taken by *other* objects.
    auto_id_counts tracks per-type counters for generating missing ids.
    """
    if not isinstance(obj, dict):
        raise ModelError(f"Each object must be a JSON object, got {obj!r}")

    unknown = set(obj) - OBJECT_FIELDS
    if unknown:
        raise ModelError(
            f"Object {obj.get('id', '?')!r}: unknown fields {sorted(unknown)}. "
            f"Allowed fields: {sorted(OBJECT_FIELDS)}"
        )

    type_ = obj.get("type")
    if type_ not in PRIMITIVES:
        raise ModelError(
            f"Object {obj.get('id', '?')!r}: type must be one of "
            f"{sorted(PRIMITIVES)}, got {type_!r}"
        )

    object_id = obj.get("id")
    if object_id is None:
        counts = auto_id_counts if auto_id_counts is not None else {}
        n = counts.get(type_, 0)
        while True:
            n += 1
            object_id = f"{type_}-{n}"
            if object_id not in existing_ids:
                break
        counts[type_] = n
    if not isinstance(object_id, str) or not object_id:
        raise ModelError(f"Object id must be a non-empty string, got {object_id!r}")
    if object_id in existing_ids:
        raise ModelError(f"Duplicate object id '{object_id}' - ids must be unique")

    defaults = PRIMITIVES[type_]
    params = obj.get("params") or {}
    if not isinstance(params, dict):
        raise ModelError(f"Object '{object_id}': params must be an object")
    unknown_params = set(params) - set(defaults)
    if unknown_params:
        raise ModelError(
            f"Object '{object_id}': unknown params {sorted(unknown_params)} for "
            f"type '{type_}'. Allowed params: {sorted(defaults)}"
        )
    normalized_params = {}
    for key, default in defaults.items():
        value = params.get(key, default)
        if not _is_number(value) or value <= 0:
            raise ModelError(
                f"Object '{object_id}': param '{key}' must be a positive number, "
                f"got {value!r}"
            )
        normalized_params[key] = float(value)

    opacity = obj.get("opacity", 1.0)
    if not _is_number(opacity) or not (0 <= opacity <= 1):
        raise ModelError(
            f"Object '{object_id}': opacity must be a number between 0 and 1"
        )

    return {
        "id": object_id,
        "type": type_,
        "params": normalized_params,
        "position": _validate_vector(obj.get("position"), "position", object_id, (0, 0, 0)),
        "rotation": _validate_vector(obj.get("rotation"), "rotation", object_id, (0, 0, 0)),
        "scale": _validate_vector(obj.get("scale"), "scale", object_id, (1, 1, 1)),
        "color": _validate_color(obj.get("color", DEFAULT_COLOR), f"Object '{object_id}'"),
        "opacity": float(opacity),
    }


def validate_document(name, objects, background=None):
    """Validate a full document, returning the normalized form."""
    if not isinstance(name, str) or not name.strip():
        raise ModelError("Model name must be a non-empty string")
    if not isinstance(objects, list) or not objects:
        raise ModelError("objects must be a non-empty list of object definitions")
    doc = {"name": name.strip(), "objects": []}
    if background is not None:
        doc["background"] = _validate_color(background, "background")
    ids = set()
    auto_id_counts = {}
    for obj in objects:
        normalized = validate_object(obj, ids, auto_id_counts)
        ids.add(normalized["id"])
        doc["objects"].append(normalized)
    return doc


def _find_object(doc, object_id):
    for i, obj in enumerate(doc["objects"]):
        if obj["id"] == object_id:
            return i
    raise ModelError(
        f"No object with id '{object_id}'. Current ids: "
        f"{[o['id'] for o in doc['objects']]}"
    )


def apply_operations(doc, operations):
    """Apply an ordered list of edit operations to a document.

    Returns a new normalized document; raises ModelError without side
    effects if any operation is invalid.
    """
    if not isinstance(operations, list) or not operations:
        raise ModelError("operations must be a non-empty list")
    doc = copy.deepcopy(doc)
    for op in operations:
        if not isinstance(op, dict) or "action" not in op:
            raise ModelError(
                f"Each operation must be an object with an 'action' field, got {op!r}"
            )
        action = op["action"]
        if action == "add_object":
            obj = op.get("object")
            if obj is None:
                raise ModelError("add_object requires an 'object' field")
            ids = {o["id"] for o in doc["objects"]}
            doc["objects"].append(validate_object(obj, ids))
        elif action == "update_object":
            object_id = op.get("id")
            changes = op.get("changes")
            if object_id is None or not isinstance(changes, dict) or not changes:
                raise ModelError(
                    "update_object requires 'id' and a non-empty 'changes' object"
                )
            if "id" in changes and changes["id"] != object_id:
                raise ModelError("update_object cannot change an object's id")
            index = _find_object(doc, object_id)
            old = doc["objects"][index]
            merged = {**old, **{k: v for k, v in changes.items() if k != "params"}}
            if "type" in changes and changes["type"] != old["type"]:
                # New primitive type: old params no longer apply
                merged["params"] = changes.get("params", {})
            elif "params" in changes:
                if not isinstance(changes["params"], dict):
                    raise ModelError(
                        f"Object '{object_id}': params must be an object"
                    )
                merged["params"] = {**old["params"], **changes["params"]}
            other_ids = {o["id"] for o in doc["objects"] if o["id"] != object_id}
            doc["objects"][index] = validate_object(merged, other_ids)
        elif action == "remove_object":
            object_id = op.get("id")
            if object_id is None:
                raise ModelError("remove_object requires an 'id' field")
            index = _find_object(doc, object_id)
            if len(doc["objects"]) == 1:
                raise ModelError(
                    "Cannot remove the last object - a model must contain at "
                    "least one object"
                )
            doc["objects"].pop(index)
        elif action == "set_name":
            name = op.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ModelError("set_name requires a non-empty 'name' string")
            doc["name"] = name.strip()
        elif action == "set_background":
            doc["background"] = _validate_color(op.get("color"), "set_background")
        else:
            raise ModelError(
                f"Unknown action {action!r}. Supported actions: add_object, "
                f"update_object, remove_object, set_name, set_background"
            )
    return doc
