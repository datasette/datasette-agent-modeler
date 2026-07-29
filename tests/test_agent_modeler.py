import json

from datasette.app import Datasette
import pytest
import pytest_asyncio

from datasette_agent_modeler import (
    VIEWER_SCRIPT_TAG,
    _create_model,
    _edit_model,
    _get_model,
    _list_models,
    register_agent_tools,
)
from datasette_agent_modeler.modeling import (
    ModelError,
    apply_operations,
    validate_document,
    validate_object,
)

ROCKET_OBJECTS = [
    {
        "id": "body",
        "type": "cylinder",
        "params": {"radius_top": 1, "radius_bottom": 1, "height": 4},
        "position": [0, 2, 0],
        "color": "#CC3344",
    },
    {
        "id": "nose",
        "type": "cone",
        "params": {"radius": 1, "height": 1.5},
        "position": [0, 4.75, 0],
    },
]


@pytest_asyncio.fixture
async def datasette():
    ds = Datasette(memory=True)
    await ds.invoke_startup()
    return ds


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-agent-modeler" in installed_plugins


def test_tools_are_registered_and_gated():
    tools = register_agent_tools(Datasette(memory=True))
    assert [t.name for t in tools] == [
        "create_model",
        "edit_model",
        "get_model",
        "list_models",
    ]
    assert all(t.required_permission == "datasette-agent-modeler" for t in tools)


@pytest.mark.asyncio
async def test_tools_filtered_by_permission():
    from datasette_agent.tools import filter_tools_for_actor, get_agent_tools

    ds = Datasette(memory=True)
    ds.root_enabled = True
    await ds.invoke_startup()
    tools = await get_agent_tools(ds)
    modeler = [t for t in tools if t.required_permission == "datasette-agent-modeler"]
    assert len(modeler) == 4
    root_tools = await filter_tools_for_actor(ds, {"id": "root"}, modeler)
    assert [t.name for t in root_tools] == [
        "create_model",
        "edit_model",
        "get_model",
        "list_models",
    ]
    other_tools = await filter_tools_for_actor(ds, {"id": "someone"}, modeler)
    assert other_tools == []


# --- validation ---


def test_validate_object_defaults():
    obj = validate_object({"type": "sphere"}, set())
    assert obj["id"] == "sphere-1"
    assert obj["params"] == {"radius": 0.5}
    assert obj["position"] == [0, 0, 0]
    assert obj["rotation"] == [0, 0, 0]
    assert obj["scale"] == [1, 1, 1]
    assert obj["opacity"] == 1.0


def test_auto_ids_do_not_collide():
    doc = validate_document(
        "Spheres",
        [{"type": "sphere"}, {"type": "sphere"}, {"id": "sphere-3", "type": "sphere"}],
    )
    assert [o["id"] for o in doc["objects"]] == ["sphere-1", "sphere-2", "sphere-3"]


@pytest.mark.parametrize(
    "objects,message",
    (
        ([{"type": "dodecahedron"}], "type must be one of"),
        ([{"type": "box", "shininess": 1}], "unknown fields"),
        ([{"type": "box", "params": {"radius": 1}}], "unknown params"),
        ([{"type": "box", "params": {"width": -1}}], "positive number"),
        ([{"type": "box", "position": [1, 2]}], "three numbers"),
        ([{"type": "box", "color": "red"}], "hex string"),
        ([{"type": "box", "opacity": 2}], "between 0 and 1"),
        ([{"id": "a", "type": "box"}, {"id": "a", "type": "box"}], "Duplicate object id"),
        ([], "non-empty list"),
    ),
)
def test_validate_document_errors(objects, message):
    with pytest.raises(ModelError, match=message):
        validate_document("Test", objects)


def test_colors_are_normalized_lowercase():
    doc = validate_document("Test", ROCKET_OBJECTS)
    assert doc["objects"][0]["color"] == "#cc3344"


# --- operations ---


def test_apply_operations():
    doc = validate_document("Rocket", ROCKET_OBJECTS)
    new_doc = apply_operations(
        doc,
        [
            {"action": "add_object", "object": {"id": "fin", "type": "box"}},
            {
                "action": "update_object",
                "id": "body",
                "changes": {"color": "#8844cc", "params": {"height": 5}},
            },
            {"action": "remove_object", "id": "nose"},
            {"action": "set_name", "name": "Better rocket"},
            {"action": "set_background", "color": "#101018"},
        ],
    )
    assert new_doc["name"] == "Better rocket"
    assert new_doc["background"] == "#101018"
    assert [o["id"] for o in new_doc["objects"]] == ["body", "fin"]
    body = new_doc["objects"][0]
    assert body["color"] == "#8844cc"
    # params merge: height changed, radii preserved
    assert body["params"] == {"radius_top": 1, "radius_bottom": 1, "height": 5}
    # Original document untouched
    assert doc["name"] == "Rocket"
    assert len(doc["objects"]) == 2


def test_update_object_type_change_resets_params():
    doc = validate_document("Test", [{"id": "a", "type": "box"}])
    new_doc = apply_operations(
        doc,
        [{"action": "update_object", "id": "a", "changes": {"type": "sphere"}}],
    )
    assert new_doc["objects"][0]["params"] == {"radius": 0.5}


@pytest.mark.parametrize(
    "operations,message",
    (
        ([{"action": "explode"}], "Unknown action"),
        ([{"action": "remove_object", "id": "nope"}], "No object with id"),
        ([{"action": "update_object", "id": "body"}], "non-empty 'changes'"),
        (
            [{"action": "update_object", "id": "body", "changes": {"id": "b2"}}],
            "cannot change",
        ),
        ([], "non-empty list"),
        (["remove_object"], "must be an object"),
    ),
)
def test_apply_operations_errors(operations, message):
    doc = validate_document("Rocket", ROCKET_OBJECTS)
    with pytest.raises(ModelError, match=message):
        apply_operations(doc, operations)


def test_cannot_remove_last_object():
    doc = validate_document("Test", [{"id": "a", "type": "box"}])
    with pytest.raises(ModelError, match="last object"):
        apply_operations(doc, [{"action": "remove_object", "id": "a"}])


# --- tool handlers end to end ---


@pytest.mark.asyncio
async def test_create_edit_get_list_round_trip(datasette):
    actor = {"id": "tester"}
    created = json.loads(
        await _create_model(
            datasette=datasette, actor=actor, name="Rocket", objects=ROCKET_OBJECTS
        )
    )
    assert "error" not in created
    model_id = created["model_id"]
    assert created["revision"] == 1
    assert created["object_ids"] == ["body", "nose"]
    assert VIEWER_SCRIPT_TAG in created["_html"]
    assert "<datasette-model-3d>" in created["_html"]
    # The full document is embedded inline for the viewer
    embedded = json.loads(
        created["_html"]
        .split('<script type="application/json">')[1]
        .split("</script>")[0]
    )
    assert embedded["modelId"] == model_id
    assert embedded["doc"]["name"] == "Rocket"

    edited = json.loads(
        await _edit_model(
            datasette=datasette,
            actor=actor,
            model_id=model_id,
            operations=[
                {"action": "add_object", "object": {"id": "fin", "type": "box"}}
            ],
        )
    )
    assert edited["revision"] == 2
    assert edited["object_ids"] == ["body", "nose", "fin"]

    fetched = json.loads(
        await _get_model(datasette=datasette, actor=actor, model_id=model_id)
    )
    assert fetched["revision"] == 2
    assert [o["id"] for o in fetched["document"]["objects"]] == ["body", "nose", "fin"]

    listed = json.loads(await _list_models(datasette=datasette, actor=actor))
    assert listed["models"] == [
        {
            "model_id": model_id,
            "name": "Rocket",
            "revision": 2,
            "updated_at": listed["models"][0]["updated_at"],
        }
    ]


@pytest.mark.asyncio
async def test_revision_history_is_preserved(datasette):
    created = json.loads(
        await _create_model(
            datasette=datasette, actor=None, name="Test", objects=[{"type": "box"}]
        )
    )
    model_id = created["model_id"]
    await _edit_model(
        datasette=datasette,
        actor=None,
        model_id=model_id,
        operations=[{"action": "set_name", "name": "Renamed"}],
    )
    db = datasette.get_internal_database()
    result = await db.execute(
        "select revision, operations_json from agent_modeler_revisions "
        "where model_id = ? order by revision",
        [model_id],
    )
    rows = result.rows
    assert [row["revision"] for row in rows] == [1, 2]
    assert rows[0]["operations_json"] is None
    assert json.loads(rows[1]["operations_json"]) == [
        {"action": "set_name", "name": "Renamed"}
    ]


@pytest.mark.asyncio
async def test_tool_errors_returned_as_json(datasette):
    invalid = json.loads(
        await _create_model(
            datasette=datasette,
            actor=None,
            name="Bad",
            objects=[{"type": "hypercube"}],
        )
    )
    assert "type must be one of" in invalid["error"]

    missing = json.loads(
        await _edit_model(
            datasette=datasette,
            actor=None,
            model_id="nope",
            operations=[{"action": "set_name", "name": "x"}],
        )
    )
    assert "No model found" in missing["error"]

    missing_get = json.loads(
        await _get_model(datasette=datasette, actor=None, model_id="nope")
    )
    assert "No model found" in missing_get["error"]


@pytest.mark.asyncio
async def test_failed_edit_writes_nothing(datasette):
    created = json.loads(
        await _create_model(
            datasette=datasette,
            actor=None,
            name="Test",
            objects=[{"id": "a", "type": "box"}],
        )
    )
    model_id = created["model_id"]
    result = json.loads(
        await _edit_model(
            datasette=datasette,
            actor=None,
            model_id=model_id,
            operations=[
                {"action": "set_name", "name": "Changed"},
                {"action": "remove_object", "id": "missing"},
            ],
        )
    )
    assert "error" in result
    fetched = json.loads(
        await _get_model(datasette=datasette, actor=None, model_id=model_id)
    )
    # The valid first operation was not applied either - edits are atomic
    assert fetched["revision"] == 1
    assert fetched["document"]["name"] == "Test"


@pytest.mark.asyncio
async def test_list_models_empty(datasette):
    listed = json.loads(await _list_models(datasette=datasette, actor=None))
    assert listed == {"models": []}
