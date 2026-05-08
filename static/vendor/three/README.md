# Vendored three.js (r160)

Sources (downloaded as-is, no modifications):

- `three.module.min.js` — https://unpkg.com/three@0.160.0/build/three.module.min.js
- `loaders/STLLoader.js` — https://unpkg.com/three@0.160.0/examples/jsm/loaders/STLLoader.js
- `loaders/OBJLoader.js` — https://unpkg.com/three@0.160.0/examples/jsm/loaders/OBJLoader.js
- `controls/OrbitControls.js` — https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js

The loaders and controls import from the bare specifier `'three'`, resolved
via the `<script type="importmap">` block in `files/templates/files.html`.

To upgrade: replace each file with the new version, bump the version above.
