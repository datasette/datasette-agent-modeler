import datetime
import json
import secrets

from datasette import hookimpl
from datasette.permissions import Action

from .modeling import PRIMITIVES, ModelError, apply_operations, validate_document
from .schema import ensure_tables

VIEWER_SCRIPT_TAG = (
    '<script src="/-/static-plugins/datasette-agent-modeler/datasette-model-3d.js"'
    ' type="module"></script>'
)

PARAMS_DESCRIPTION = (
    "Dimensions per type - box: width, height, depth; sphere: radius; "
    "cylinder: radius_top, radius_bottom, height; cone: radius, height; "
    "torus: radius (ring), tube (thickness); capsule: radius, length; "
    "plane: width, height. All positive numbers with sensible defaults."
)

OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "Unique id for this object within the model, used to address "
                "it in later edit_model calls. Auto-generated if omitted."
            ),
        },
        "type": {
            "type": "string",
            "enum": sorted(PRIMITIVES),
            "description": "Primitive shape type",
        },
        "params": {
            "type": "object",
            "description": PARAMS_DESCRIPTION,
        },
        "position": {
            "type": "array",
            "items": {"type": "number"},
            "description": (
                "[x, y, z] position of the object's center, default [0, 0, 0]. "
                "Y is up and the ground grid is at y=0, so raise objects by "
                "half their height to sit on the ground."
            ),
        },
        "rotation": {
            "type": "array",
            "items": {"type": "number"},
            "description": "[x, y, z] Euler rotation in degrees, default [0, 0, 0]",
        },
        "scale": {
            "type": "array",
            "items": {"type": "number"},
            "description": "[x, y, z] scale factors, default [1, 1, 1]",
        },
        "color": {
            "type": "string",
            "description": "Hex color like '#cc3344'",
        },
        "opacity": {
            "type": "number",
            "description": "0 (invisible) to 1 (opaque), default 1",
        },
    },
    "required": ["type"],
}

OPERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "add_object",
                "update_object",
                "remove_object",
                "set_name",
                "set_background",
            ],
        },
        "object": {
            "type": "object",
            "description": "For add_object: the object definition to add",
        },
        "id": {
            "type": "string",
            "description": "For update_object / remove_object: the target object id",
        },
        "changes": {
            "type": "object",
            "description": (
                "For update_object: fields to change. Top-level fields "
                "(position, rotation, scale, color, opacity, type) replace the "
                "old value; params are merged so you only need to send the "
                "params that changed."
            ),
        },
        "name": {
            "type": "string",
            "description": "For set_name: the new model name",
        },
        "color": {
            "type": "string",
            "description": "For set_background: hex background color",
        },
    },
    "required": ["action"],
}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _actor_id(actor):
    return (actor or {}).get("id")


def _build_html(model_id, revision, doc):
    config_json = json.dumps({"modelId": model_id, "revision": revision, "doc": doc})
    return (
        f"{VIEWER_SCRIPT_TAG}\n"
        f"<datasette-model-3d>\n"
        f'<script type="application/json">{config_json}</script>\n'
        f"</datasette-model-3d>"
    )


def _summary(model_id, revision, doc):
    return {
        "model_id": model_id,
        "revision": revision,
        "name": doc["name"],
        "object_ids": [obj["id"] for obj in doc["objects"]],
    }


def _error(message):
    return json.dumps({"error": message})


async def _load_current_revision(db, model_id):
    """Return (doc, revision) for a model's latest revision."""
    result = await db.execute(
        """
        select doc_json, revision from agent_modeler_revisions
        where model_id = ? order by revision desc limit 1
        """,
        [model_id],
    )
    row = result.first()
    if row is None:
        raise ModelError(
            f"No model found with id {model_id!r} - use list_models to see "
            "available models"
        )
    return json.loads(row["doc_json"]), row["revision"]


async def _write_revision(db, model_id, revision, doc, operations=None):
    await db.execute_write(
        """
        insert into agent_modeler_revisions
            (model_id, revision, doc_json, operations_json, created_at)
        values (?, ?, ?, ?, ?)
        """,
        [
            model_id,
            revision,
            json.dumps(doc),
            json.dumps(operations) if operations is not None else None,
            _now(),
        ],
    )


async def _create_model(datasette, actor, name, objects, background=None):
    db = datasette.get_internal_database()
    await ensure_tables(db)
    try:
        doc = validate_document(name, objects, background)
    except ModelError as e:
        return _error(str(e))
    model_id = secrets.token_hex(4)
    now = _now()
    await db.execute_write(
        """
        insert into agent_modeler_models (id, actor_id, name, created_at, updated_at)
        values (?, ?, ?, ?, ?)
        """,
        [model_id, _actor_id(actor), doc["name"], now, now],
    )
    await _write_revision(db, model_id, 1, doc)
    return json.dumps(
        {
            "_html": _build_html(model_id, 1, doc),
            **_summary(model_id, 1, doc),
        }
    )


async def _edit_model(datasette, actor, model_id, operations):
    db = datasette.get_internal_database()
    await ensure_tables(db)
    try:
        doc, revision = await _load_current_revision(db, model_id)
        new_doc = apply_operations(doc, operations)
    except ModelError as e:
        return _error(str(e))
    new_revision = revision + 1
    await _write_revision(db, model_id, new_revision, new_doc, operations)
    await db.execute_write(
        "update agent_modeler_models set name = ?, updated_at = ? where id = ?",
        [new_doc["name"], _now(), model_id],
    )
    return json.dumps(
        {
            "_html": _build_html(model_id, new_revision, new_doc),
            **_summary(model_id, new_revision, new_doc),
        }
    )


async def _get_model(datasette, actor, model_id):
    db = datasette.get_internal_database()
    await ensure_tables(db)
    try:
        doc, revision = await _load_current_revision(db, model_id)
    except ModelError as e:
        return _error(str(e))
    return json.dumps({"model_id": model_id, "revision": revision, "document": doc})


async def _list_models(datasette, actor):
    db = datasette.get_internal_database()
    await ensure_tables(db)
    result = await db.execute(
        """
        select m.id, m.name, m.updated_at, max(r.revision) as revision
        from agent_modeler_models m
        join agent_modeler_revisions r on r.model_id = m.id
        group by m.id order by m.updated_at desc
        """
    )
    return json.dumps(
        {
            "models": [
                {
                    "model_id": row["id"],
                    "name": row["name"],
                    "revision": row["revision"],
                    "updated_at": row["updated_at"],
                }
                for row in result.rows
            ]
        }
    )


@hookimpl
def register_actions():
    return [
        Action(
            name="datasette-agent-modeler",
            description="Create and edit 3D models with the agent",
        ),
    ]


@hookimpl
def register_agent_tools(datasette):
    from datasette_agent.tools import AgentTool

    return [
        AgentTool(
            name="create_model",
            description=(
                "Create a new 3D model from a list of primitive shapes and "
                "display it to the user in an interactive 3D viewer. Returns a "
                "model_id for use with edit_model. Compose shapes (box, sphere, "
                "cylinder, cone, torus, capsule, plane) with positions, "
                "rotations (degrees), scales and colors. Y is up; the ground "
                "grid is at y=0."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name for the model",
                    },
                    "objects": {
                        "type": "array",
                        "items": OBJECT_SCHEMA,
                        "description": "The shapes making up the model",
                    },
                    "background": {
                        "type": "string",
                        "description": "Optional hex background color for the viewer",
                    },
                },
                "required": ["name", "objects"],
            },
            fn=_create_model,
            required_permission="datasette-agent-modeler",
        ),
        AgentTool(
            name="edit_model",
            description=(
                "Edit an existing 3D model by applying an ordered list of "
                "operations, then display the updated model in an interactive "
                "3D viewer. Operations: add_object (provide 'object'), "
                "update_object (provide 'id' and 'changes' - params are merged, "
                "other fields replaced), remove_object (provide 'id'), set_name "
                "(provide 'name'), set_background (provide 'color'). All "
                "operations are applied atomically - if any fails, nothing "
                "changes. Use get_model first if unsure of the current state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model_id": {
                        "type": "string",
                        "description": "The model to edit, from create_model or list_models",
                    },
                    "operations": {
                        "type": "array",
                        "items": OPERATION_SCHEMA,
                        "description": "Ordered edit operations to apply",
                    },
                },
                "required": ["model_id", "operations"],
            },
            fn=_edit_model,
            required_permission="datasette-agent-modeler",
        ),
        AgentTool(
            name="get_model",
            description=(
                "Get the current JSON document and revision number for a 3D "
                "model previously created with create_model."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model_id": {
                        "type": "string",
                        "description": "The model to fetch",
                    },
                },
                "required": ["model_id"],
            },
            fn=_get_model,
            required_permission="datasette-agent-modeler",
        ),
        AgentTool(
            name="list_models",
            description="List stored 3D models with their ids, names and revisions.",
            input_schema={"type": "object", "properties": {}},
            fn=_list_models,
            required_permission="datasette-agent-modeler",
        ),
    ]
