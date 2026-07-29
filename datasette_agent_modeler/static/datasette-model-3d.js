const THREE_URL = "https://esm.sh/three@0.185.0";
const CONTROLS_URL =
  "https://esm.sh/three@0.185.0/examples/jsm/controls/OrbitControls.js";

let threePromise = null;
function loadThree() {
  if (!threePromise) {
    threePromise = Promise.all([import(THREE_URL), import(CONTROLS_URL)]).then(
      ([THREE, controls]) => ({ THREE, OrbitControls: controls.OrbitControls })
    );
  }
  return threePromise;
}

const DEG = Math.PI / 180;

function buildGeometry(THREE, obj) {
  const p = obj.params || {};
  switch (obj.type) {
    case "box":
      return new THREE.BoxGeometry(p.width, p.height, p.depth);
    case "sphere":
      return new THREE.SphereGeometry(p.radius, 48, 24);
    case "cylinder":
      return new THREE.CylinderGeometry(
        p.radius_top,
        p.radius_bottom,
        p.height,
        48
      );
    case "cone":
      return new THREE.ConeGeometry(p.radius, p.height, 48);
    case "torus":
      return new THREE.TorusGeometry(p.radius, p.tube, 24, 64);
    case "capsule":
      return new THREE.CapsuleGeometry(p.radius, p.length, 8, 24);
    case "plane":
      return new THREE.PlaneGeometry(p.width, p.height);
    default:
      throw new Error(`Unknown object type: ${obj.type}`);
  }
}

function buildScene(THREE, doc) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(doc.background || "#eef1f5");

  scene.add(new THREE.HemisphereLight(0xffffff, 0x555566, 1.2));
  const sun = new THREE.DirectionalLight(0xffffff, 1.8);
  sun.position.set(5, 10, 7);
  scene.add(sun);

  const group = new THREE.Group();
  for (const obj of doc.objects || []) {
    const material = new THREE.MeshStandardMaterial({
      color: obj.color || "#8899aa",
      roughness: 0.55,
      metalness: 0.1,
    });
    if (obj.type === "plane") {
      material.side = THREE.DoubleSide;
    }
    if (typeof obj.opacity === "number" && obj.opacity < 1) {
      material.transparent = true;
      material.opacity = obj.opacity;
    }
    const mesh = new THREE.Mesh(buildGeometry(THREE, obj), material);
    const [px, py, pz] = obj.position || [0, 0, 0];
    const [rx, ry, rz] = obj.rotation || [0, 0, 0];
    const [sx, sy, sz] = obj.scale || [1, 1, 1];
    mesh.position.set(px, py, pz);
    mesh.rotation.set(rx * DEG, ry * DEG, rz * DEG);
    mesh.scale.set(sx, sy, sz);
    mesh.userData.modelObjectId = obj.id;
    group.add(mesh);
  }
  scene.add(group);

  // Size the ground grid to comfortably contain the model
  const bounds = new THREE.Box3().setFromObject(group);
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  bounds.getSize(size);
  bounds.getCenter(center);
  const extent = Math.max(size.x, size.z, 1);
  const gridSize = Math.ceil(extent * 1.6) + 2;
  const grid = new THREE.GridHelper(gridSize, gridSize, 0x99a2ad, 0xcdd3da);
  grid.position.set(Math.round(center.x), 0, Math.round(center.z));
  scene.add(grid);

  return { scene, bounds, center, size };
}

class DatasetteModel3D extends HTMLElement {
  async connectedCallback() {
    let scriptEl = this.querySelector('script[type="application/json"]');
    if (!scriptEl) {
      // Wait a frame in case children aren't parsed yet (innerHTML insertion)
      await new Promise((r) => requestAnimationFrame(r));
      scriptEl = this.querySelector('script[type="application/json"]');
    }

    this.textContent = "Loading 3D model…";
    this.style.display = "block";

    if (!scriptEl) {
      this.textContent = "Error: no model configuration found";
      return;
    }

    let config;
    try {
      config = JSON.parse(scriptEl.textContent);
    } catch (e) {
      this.textContent = "Error parsing model config: " + e.message;
      return;
    }
    const doc = config.doc;
    if (!doc || !Array.isArray(doc.objects)) {
      this.textContent = "Error: model document has no objects";
      return;
    }

    let THREE, OrbitControls;
    try {
      ({ THREE, OrbitControls } = await loadThree());
    } catch (e) {
      this.textContent = "Error loading three.js: " + e.message;
      return;
    }

    try {
      this.render(THREE, OrbitControls, config);
    } catch (e) {
      this.textContent = "Error rendering model: " + e.message;
    }
  }

  render(THREE, OrbitControls, config) {
    const doc = config.doc;
    const { scene, bounds, center, size } = buildScene(THREE, doc);

    const height = 420;
    const width = this.clientWidth || 640;

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 5000);
    const maxDim = Math.max(size.x, size.y, size.z, 1);
    const distance = maxDim * 2.1;
    camera.position
      .copy(center)
      .add(new THREE.Vector3(1, 0.65, 1).normalize().multiplyScalar(distance));

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(width, height);

    this.textContent = "";
    const container = document.createElement("div");
    container.style.cssText =
      "border: 1px solid #d0d5da; border-radius: 4px; overflow: hidden;";
    renderer.domElement.style.cssText =
      "display: block; width: 100%; height: " + height + "px; cursor: grab;";
    container.appendChild(renderer.domElement);
    this.appendChild(container);

    const caption = document.createElement("p");
    caption.style.cssText = "font-size: 0.85em; color: #666; margin: 0.3em 0 0;";
    const objectCount = doc.objects.length;
    caption.textContent =
      `${doc.name || "Untitled model"} · revision ${config.revision ?? "?"} ` +
      `· ${objectCount} object${objectCount === 1 ? "" : "s"} ` +
      `· drag to orbit, scroll to zoom, right-drag to pan`;
    this.appendChild(caption);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.copy(center);
    controls.update();

    const renderFrame = () => renderer.render(scene, camera);
    controls.addEventListener("change", renderFrame);

    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(() => {
        const w = this.clientWidth;
        if (w > 0 && Math.abs(w - renderer.domElement.clientWidth) > 1) {
          camera.aspect = w / height;
          camera.updateProjectionMatrix();
          renderer.setSize(w, height);
          renderer.domElement.style.width = "100%";
          renderFrame();
        }
      });
      observer.observe(this);
    }

    renderFrame();
  }
}

if (!customElements.get("datasette-model-3d")) {
  customElements.define("datasette-model-3d", DatasetteModel3D);
}
