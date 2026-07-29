# datasette-agent-modeler — design proposal

A [Datasette Agent](https://github.com/datasette/datasette-agent) plugin that gives the
agent tools for **creating a 3D model and then iteratively editing it**, with an
interactive 3D viewer rendered inline in the chat conversation.

## Research

### The datasette-agent plugin API

`datasette-agent` exposes a `register_agent_tools(datasette)` plugin hook returning
`AgentTool` instances (`name`, `description`, `input_schema`, async `fn`, optional
`required_permission`). Tool handlers are async functions taking `datasette` and `actor`
keyword arguments plus the schema parameters, returning a JSON string. Two conventions
matter for this plugin:

- **Rich output**: a top-level `_html` key in the returned JSON is rendered as raw HTML
  in the chat UI and stripped before the result is sent back to the LLM. The
  [datasette-agent-charts](https://github.com/datasette/datasette-agent-charts) plugin
  establishes the pattern: a custom element (`<datasette-chart>`) with an inline
  `<script type="application/json">` config block, plus a `<script type="module">` tag
  loading the element implementation from `/-/static-plugins/<plugin>/<file>.js`, which
  itself imports its rendering library from a pinned esm.sh URL.
- **Errors as data**: tools return `{"error": "..."}` rather than raising, so the LLM
  can read the message and self-correct.

For persistence, `datasette-agent` itself uses Datasette's **internal database**
(`datasette.get_internal_database()`) with `CREATE TABLE IF NOT EXISTS` schema applied
lazily at each entry point (`ensure_tables`). This plugin follows the same convention —
the internal database is the right home for plugin state that should not show up in the
user's browsable databases, and it works even when every user database is immutable.

### JavaScript 3D library options

Requirements: render a scene assembled from primitive shapes, interactive orbit/zoom/pan
browsing, loadable as an ES module from a CDN (no build step — Datasette plugins ship
plain static files), reasonable size, active maintenance.

| Library | Verdict |
|---|---|
| **three.js** (r185, July 2026) | **Chosen.** The de facto standard web 3D library — 14+ years of development, by far the largest community and market share among JS 3D libraries. Ships every primitive we need (`BoxGeometry`, `SphereGeometry`, `CylinderGeometry`, `ConeGeometry`, `TorusGeometry`, `CapsuleGeometry`, `PlaneGeometry`), `OrbitControls` for the browse UI, works as an ES module from esm.sh with no build step. Low-level, but our scene-graph needs are simple. |
| **Babylon.js** | Full game engine, Microsoft-backed. Excellent but much heavier than needed for a primitive-scene viewer, and the single-bundle ES module story from a CDN is clunkier. |
| **`<model-viewer>`** (Google) | Beautiful drop-in orbit viewer, but it only displays **glTF/GLB assets**. We would have to generate glTF server-side on every edit rather than describing scenes declaratively. Wrong shape for incremental JSON edits. |
| **A-Frame** | Declarative HTML entities over three.js — tempting for an LLM to emit directly, but it is VR/WebXR-focused, heavyweight, and registers global custom elements that could clash with the host page. |
| **x3dom / X3D** | Declarative, but aging ecosystem and much smaller community. |
| **JSCAD (OpenJSCAD)** | Code-driven CSG modelling — a good fit conceptually for "the agent writes a program that is the model", but the viewer/ESM-from-CDN story is weak and debugging generated code is harder for an LLM than editing declarative JSON. CSG booleans are deliberately out of scope for v1 (see Future work). |

**Decision**: three.js pinned at `three@0.185.0` from esm.sh (matching the charts
plugin's esm.sh precedent), with `OrbitControls` from the same package.

## Design

### The model document

A model is a declarative JSON scene graph of primitives. Declarative JSON (rather than
generated code or mesh data) is the key design decision: the LLM can reliably produce
and *surgically edit* it via addressable object ids, the server can validate it
strictly, and the viewer can render any revision of it without executing anything.

```json
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
    },
    {
      "id": "nose",
      "type": "cone",
      "params": {"radius": 1, "height": 1.5},
      "position": [0, 4.75, 0],
      "color": "#dddddd"
    }
  ]
}
```

- **`id`** — unique per model, chosen by the LLM (auto-generated `type-N` if omitted).
  Ids are what make edits addressable.
- **`type` + `params`** — one of `box` (width/height/depth), `sphere` (radius),
  `cylinder` (radius_top/radius_bottom/height), `cone` (radius/height), `torus`
  (radius/tube), `capsule` (radius/length), `plane` (width/height). All params have
  sensible defaults.
- **`position` / `rotation` / `scale`** — `[x, y, z]`; rotation is **Euler XYZ in
  degrees** (far more natural for an LLM than radians; converted in the viewer).
  Y is up; the ground grid sits at y=0.
- **`color`** — `#rgb`/`#rrggbb` hex; **`opacity`** — 0–1 (values below 1 get a
  transparent material).

Everything is validated server-side; validation failures name the object id and field so
the model can fix its call.

### Tool surface

Four tools, mirroring a create → inspect → edit loop:

| Tool | Purpose |
|---|---|
| `create_model(name, objects, background?)` | Validate and store revision 1, return `model_id`, render the viewer inline. |
| `edit_model(model_id, operations)` | Apply an ordered list of operations **atomically** (validate everything, then write one new revision — no partial edits). Renders the updated viewer. |
| `get_model(model_id)` | Return the current document + revision number so the agent can re-inspect state instead of guessing. |
| `list_models()` | List stored models (id, name, revision, updated_at). |

`edit_model` operations:

```json
[
  {"action": "add_object", "object": {"id": "fin1", "type": "box", "...": "..."}},
  {"action": "update_object", "id": "body", "changes": {"color": "#8844cc", "params": {"height": 5}}},
  {"action": "remove_object", "id": "nose"},
  {"action": "set_name", "name": "Better rocket"},
  {"action": "set_background", "color": "#101018"}
]
```

`update_object.changes` is a merge: top-level fields replace, `params` shallow-merges,
so the LLM only sends what changed.

### Storage

Internal database, `ensure_tables` lazily on each tool call (same convention as
datasette-agent's own schema):

```sql
CREATE TABLE IF NOT EXISTS agent_modeler_models (
    id TEXT PRIMARY KEY,          -- short random hex
    actor_id TEXT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_modeler_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL REFERENCES agent_modeler_models(id),
    revision INTEGER NOT NULL,
    doc_json TEXT NOT NULL,       -- full document snapshot
    operations_json TEXT,         -- the ops that produced it (NULL for revision 1)
    created_at TEXT NOT NULL
);
```

Every edit stores a **full document snapshot** as a new revision. Snapshots (not
deltas) keep reads trivial, give free history, and set up future revert/undo. Models
record the creating `actor_id` but are not access-restricted between actors in v1 —
access to the tools is already gated by the `datasette-agent` chat permission, and
models are not sensitive per se.

### Viewer

`static/datasette-model-3d.js` defines a `<datasette-model-3d>` custom element. The
`_html` returned by `create_model`/`edit_model` embeds the **full document JSON inline**
(charts-plugin pattern) — no fetch endpoint needed, and because chat replays stored
HTML, each tool result in the transcript permanently shows the model *as it was at that
revision*, giving a visual edit history for free.

Viewer behaviour:

- three.js `WebGLRenderer`, perspective camera **auto-framed** from the scene's bounding
  box, `OrbitControls` (orbit / zoom / pan)
- hemisphere + directional lighting, ground grid at y=0
- damped render-on-demand loop, `ResizeObserver` for responsive width
- caption showing model name, revision and object count
- graceful text fallback on any error (no WebGL, CDN unreachable, bad JSON)

### Permissions

Registers a `datasette-agent-modeler` action (via `register_actions`) and gates all four
tools with `required_permission="datasette-agent-modeler"` — the documented
datasette-agent pattern (its own background tools work this way). Actors without the
permission never see the tools. `--root` (and `datasette agent chat --root`) holds it.

### Out of scope / future work

- **CSG booleans** (union/subtract/intersect) — would enable real part modelling;
  three-bvh-csg is the likely route.
- **Export** — STL/GLTF download button via three.js exporters.
- **Revert tool** — restore a previous revision (data model already supports it).
- **Groups/hierarchy** — nested transforms for articulated models.
- A browsable `/-/modeler` index page of saved models.
