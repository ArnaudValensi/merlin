# Vendored voice activity detection

Loaded lazily by the voice console (`voice/static/voice-console.js`) the first
time voice mode opens. Merlin is self-hosted and often reached through a tunnel,
so nothing is fetched from a CDN at runtime. Each directory carries its exact
version in its name: the server marks `/static/voice/vendor/` immutable, so a
phone downloads these files once (see `main.NoCacheStaticMiddleware`). A new
version goes in a new directory.

| Directory | Package | License | Files |
|---|---|---|---|
| `vad-web-0.0.30/` | [`@ricky0123/vad-web`](https://github.com/ricky0123/vad) 0.0.30 (npm) | ISC | `dist/bundle.min.js` (+ its license notice), `dist/vad.worklet.bundle.min.js`, `dist/silero_vad_legacy.onnx` (Silero VAD, MIT) |
| `onnxruntime-web-1.22.0/` | [`onnxruntime-web`](https://github.com/microsoft/onnxruntime) 1.22.0 (npm) | MIT | `dist/ort.wasm.min.js`, `dist/ort-wasm-simd-threaded.mjs`, `dist/ort-wasm-simd-threaded.wasm` |

Copied unmodified from the npm tarballs (`npm pack <name>@<version>`). sha256:

```
65e09376df69107e881b5c34d2d37aed333a366b6d941073ec518168e269b87d  onnxruntime-web-1.22.0/ort.wasm.min.js
30dd851d9c00622940500f71ddd2ff8820c5cb65270816080175b958705385a8  onnxruntime-web-1.22.0/ort-wasm-simd-threaded.mjs
71aef04959c5c1b6de461b6538e2058e306610034a85aad2742d0c7fd4533fe4  onnxruntime-web-1.22.0/ort-wasm-simd-threaded.wasm
206cf2ca0bacee64f115eba1769e33ce68bd979c9b53be31e6e0ec0edbab9ff8  vad-web-0.0.30/bundle.min.js
85d829adcdee0fe0a5caa95c238f9e4d49758f2b222944171f5d2e8f926e01b4  vad-web-0.0.30/bundle.min.js.LICENSE.txt
a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28  vad-web-0.0.30/silero_vad_legacy.onnx
8a48fdc7429948a2fde3d29a84bb1a64c1f67b4ba578ccaa7548b7f989f06a74  vad-web-0.0.30/vad.worklet.bundle.min.js
```
