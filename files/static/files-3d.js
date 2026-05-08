/* 3D model preview (STL + OBJ) — module entry, exposes window.merlin3D
 *
 * Loaded as <script type="module"> after files.js. Uses the importmap declared
 * in files.html to resolve the `three` bare specifier to the vendored module.
 */

import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// Test-mode handle: enables window.__merlin3DTest for Playwright assertions.
// We initialise the slot to null so tests can `=== null` whether or not a
// 3D scene has ever mounted on this page.
if (new URLSearchParams(window.location.search).get('test') === '1') {
    window.__merlinTestMode = true;
    window.__merlin3DTest = null;
}

let ctx = null; // active scene state, see mountModel()

async function render3DPreview(info, container) {
    disposeThreeContext();

    const wrapper = document.createElement('div');
    wrapper.className = 'model3d-preview';
    container.appendChild(wrapper);

    const dimsPill = document.createElement('div');
    dimsPill.className = 'model3d-dims';
    dimsPill.textContent = '…';
    wrapper.appendChild(dimsPill);

    const ext = info.name.toLowerCase().split('.').pop();
    let geometry;
    let objectFromLoader = null;

    try {
        const resp = await fetch(
            '/api/files/raw?path=' + encodeURIComponent(info.path),
        );
        if (!resp.ok) {
            throw new Error('HTTP ' + resp.status);
        }

        if (ext === 'stl') {
            const buf = await resp.arrayBuffer();
            geometry = new STLLoader().parse(buf);
        } else if (ext === 'obj') {
            const text = await resp.text();
            objectFromLoader = new OBJLoader().parse(text);
        } else {
            throw new Error('Unsupported 3D format: ' + ext);
        }
    } catch (err) {
        wrapper.remove();
        // Surface to caller so files.js can fall back to renderBinaryInfo
        throw err;
    }

    mountModel(wrapper, dimsPill, geometry, objectFromLoader);
}

function mountModel(wrapper, dimsPill, geometry, objectFromLoader) {
    const scene = new THREE.Scene();
    const bgColor = readCssColor('--bg-page', '#0d1117');
    scene.background = new THREE.Color(bgColor);

    // Up-vector convention: +Z (slicer-friendly: bed = XY, height = Z)
    THREE.Object3D.DEFAULT_UP = new THREE.Vector3(0, 0, 1);

    const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 10000);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    wrapper.appendChild(renderer.domElement);

    const material = new THREE.MeshStandardMaterial({
        color: 0xb8b8b8,
        metalness: 0.05,
        roughness: 0.6,
        flatShading: false,
    });

    let object3d;
    if (geometry) {
        // STL → BufferGeometry → wrap in a Mesh
        geometry.computeVertexNormals();
        object3d = new THREE.Mesh(geometry, material);
    } else {
        // OBJ → Group of meshes; replace materials with our default
        object3d = objectFromLoader;
        object3d.traverse((child) => {
            if (child.isMesh) {
                child.material = material;
                if (child.geometry) {
                    child.geometry.computeVertexNormals();
                }
            }
        });
    }
    scene.add(object3d);

    // Lights — hemisphere fill + directional key
    const hemi = new THREE.HemisphereLight(0xffffff, 0x444444, 0.9);
    scene.add(hemi);
    const key = new THREE.DirectionalLight(0xffffff, 0.7);
    key.position.set(1, 1.2, 1).normalize();
    scene.add(key);

    // Bounding box → dimensions (mm) + camera fit
    const box = new THREE.Box3().setFromObject(object3d);
    const size = new THREE.Vector3();
    const center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);

    dimsPill.textContent =
        size.x.toFixed(2) + ' × ' + size.y.toFixed(2) + ' × ' + size.z.toFixed(2) +
        ' mm';

    // Center the model at the origin
    object3d.position.sub(center);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, 0);

    fitCameraToBox(camera, controls, size);

    const render = () => renderer.render(scene, camera);

    // Resize observer keeps the canvas matched to wrapper size
    const resize = () => {
        const w = wrapper.clientWidth || 1;
        const h = wrapper.clientHeight || 1;
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        render();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(wrapper);
    resize();

    // OrbitControls fires "change" on user interaction and on damping ticks
    controls.addEventListener('change', render);

    // Damping needs a small rAF loop — only while damping is mid-flight.
    // We use the "start"/"end" events to bound it.
    let raf = null;
    const animate = () => {
        controls.update();
        raf = requestAnimationFrame(animate);
    };
    controls.addEventListener('start', () => {
        if (raf === null) animate();
    });
    controls.addEventListener('end', () => {
        // Let damping drift a few frames, then stop
        setTimeout(() => {
            if (raf !== null) {
                cancelAnimationFrame(raf);
                raf = null;
                render();
            }
        }, 600);
    });

    ctx = {
        wrapper,
        renderer,
        scene,
        camera,
        controls,
        observer,
        material,
        object3d,
        get raf() {
            return raf;
        },
        stop() {
            if (raf !== null) {
                cancelAnimationFrame(raf);
                raf = null;
            }
        },
    };

    if (window.__merlinTestMode) {
        window.__merlin3DTest = {
            get camera() { return camera; },
            get controls() { return controls; },
            get scene() { return scene; },
            get renderer() { return renderer; },
            get dims() { return { x: size.x, y: size.y, z: size.z }; },
            get canvas() { return renderer.domElement; },
        };
    }
}

function fitCameraToBox(camera, controls, size) {
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const fov = (camera.fov * Math.PI) / 180;
    // Place camera so the largest axis fills ~80% of the viewport
    const distance = (maxDim / 2 / Math.tan(fov / 2)) / 0.8;
    // Iso-ish viewing angle
    camera.position.set(distance, -distance, distance * 0.6);
    camera.lookAt(0, 0, 0);
    controls.update();
}

function disposeThreeContext() {
    if (!ctx) return;
    try {
        ctx.stop();
        ctx.observer.disconnect();
        ctx.controls.dispose();
        // Free GPU resources
        ctx.scene.traverse((child) => {
            if (child.geometry) child.geometry.dispose();
            if (child.material) {
                if (Array.isArray(child.material)) {
                    child.material.forEach((m) => m.dispose());
                } else {
                    child.material.dispose();
                }
            }
        });
        ctx.renderer.dispose();
        ctx.renderer.forceContextLoss?.();
        if (ctx.renderer.domElement && ctx.renderer.domElement.parentNode) {
            ctx.renderer.domElement.parentNode.removeChild(ctx.renderer.domElement);
        }
    } finally {
        ctx = null;
        if (window.__merlinTestMode) {
            window.__merlin3DTest = null;
        }
    }
}

function readCssColor(varName, fallback) {
    const v = getComputedStyle(document.documentElement)
        .getPropertyValue(varName)
        .trim();
    return v || fallback;
}

// Expose to non-module files.js
window.merlin3D = {
    render3DPreview,
    disposeThreeContext,
};
