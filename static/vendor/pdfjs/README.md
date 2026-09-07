# Vendored pdf.js (pdfjs-dist 4.10.38, modern ESM build)

Mozilla's PDF renderer, used for inline PDF preview in the file browser
(`files/static/files-pdf.js`). Rendered client-side to `<canvas>` so it works
identically on desktop Chrome, Android Chrome, and iOS Safari (the native
`<embed>`/`<iframe>` path is unreliable on iOS).

Sources (downloaded as-is from the npm tarball
`https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-4.10.38.tgz`, no modifications):

- `pdf.min.mjs` — main library, from `build/pdf.min.mjs`. Resolved via the
  `pdfjs-dist` bare specifier in the `<script type="importmap">` block in
  `files/templates/files.html`.
- `pdf.worker.min.mjs` — the worker (parsing/rendering off the main thread),
  from `build/pdf.worker.min.mjs`. Wired via
  `pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs'`
  in `files-pdf.js`. Without it pdf.js tries to fetch the worker from a CDN and
  fails offline.
- `cmaps/` — character maps for CJK / non-Latin text. Passed to `getDocument`
  as `cMapUrl: '/static/vendor/pdfjs/cmaps/'`, `cMapPacked: true`.
- `standard_fonts/` — the standard PDF fonts, for PDFs that reference but don't
  embed them. Passed as
  `standardFontDataUrl: '/static/vendor/pdfjs/standard_fonts/'`.

## Why the modern build (not `legacy`)

The modern ESM build is smaller, slightly faster, and matches how Merlin already
loads three.js as an ES module via the importmap. pdf.js also ships a `legacy`
transpiled build for very old browsers; the target device (a current iPhone)
does not need it, so we do not vendor it.

## Why v4.x (not v5/v6)

The v4 line renders without a WebAssembly dependency. v5+ pulls in a `wasm/`
asset directory (JPEG2000 / image decoding) that would need serving and wiring
via `wasmUrl`, and v6 requires a Node >=22 build toolchain. v4.10.38 is the last
v4 release: broad iOS Safari support, no extra asset plumbing.

## To upgrade

Replace `pdf.min.mjs`, `pdf.worker.min.mjs`, `cmaps/`, and `standard_fonts/`
with the files from the new tarball's `build/`, `cmaps/`, and `standard_fonts/`,
then bump the version above. If moving to v5+, also vendor the `wasm/` directory
and set `getDocument({ wasmUrl: '/static/vendor/pdfjs/wasm/' })`.

## License

pdf.js is licensed under the Apache License 2.0 (Mozilla Foundation). See
https://github.com/mozilla/pdf.js/blob/master/LICENSE.
