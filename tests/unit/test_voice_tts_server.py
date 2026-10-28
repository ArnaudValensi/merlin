"""The startup of ``tools/kokoro-tts-server.py``: model download and preflight.

No network and no GPU: the model files are small fakes served by a local HTTP
server, and the synthesizer is a stand-in.
"""

import errno
import hashlib
import importlib.util
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SERVER_PATH = Path(__file__).parents[2] / "tools" / "kokoro-tts-server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("kokoro_tts_server_pf", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


srv = _load_server()
PreflightError = srv.PreflightError

MODEL = b"model bytes " * 5000
VOICES = b"voice bytes " * 300


class FileHost:
    """Serves byte strings by path and counts the requests."""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.hits: list[str] = []
        self.short: set[str] = set()  # paths answered with a truncated body
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                host.hits.append(self.path)
                body = host.files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                if self.path in host.short:
                    body = body[: len(body) // 2]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        ).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def spec(self, path: str, content: bytes):
        return srv.ModelFile(
            f"{self.base}{path}", len(content), hashlib.sha256(content).hexdigest()
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def host():
    h = FileHost({"/m.onnx": MODEL, "/v.bin": VOICES})
    yield h
    h.close()


@pytest.fixture
def files(host):
    return {
        "m.onnx": host.spec("/m.onnx", MODEL),
        "v.bin": host.spec("/v.bin", VOICES),
    }


def _ensure(models, files, **kw):
    logs: list[str] = []
    srv.ensure_models(models, files, log=logs.append, **kw)
    return logs


def _leftovers(models: Path):
    return sorted(p.name for p in models.iterdir() if p.name.endswith(".part"))


class TestPinnedFiles:
    def test_the_pins_match_the_founders_files(self):
        model = srv.MODEL_FILES["kokoro-v1.0.onnx"]
        voices = srv.MODEL_FILES["voices-v1.0.bin"]
        assert model.url.endswith("/model-files-v1.0/kokoro-v1.0.onnx")
        assert voices.url.endswith("/model-files-v1.0/voices-v1.0.bin")
        assert (model.size, voices.size) == (325532387, 28214398)
        assert model.sha256.startswith("7d5df8ec") and voices.sha256.startswith(
            "bca610b8"
        )


class TestDownload:
    def test_missing_files_are_downloaded_and_verified(self, tmp_path, host, files):
        models = tmp_path / "new" / "dir"
        logs = _ensure(models, files)
        assert (models / "m.onnx").read_bytes() == MODEL
        assert (models / "v.bin").read_bytes() == VOICES
        assert _leftovers(models) == []
        umask = os.umask(0)
        os.umask(umask)
        assert (models / "m.onnx").stat().st_mode & 0o777 == 0o644 & ~umask
        assert any("100%" in line for line in logs)
        assert any("verified" in line for line in logs)

    def test_present_and_right_files_are_not_downloaded(self, tmp_path, host, files):
        (tmp_path / "m.onnx").write_bytes(MODEL)
        (tmp_path / "v.bin").write_bytes(VOICES)
        _ensure(tmp_path, files)
        assert host.hits == []

    def test_only_the_missing_file_is_downloaded(self, tmp_path, host, files):
        (tmp_path / "m.onnx").write_bytes(MODEL)
        _ensure(tmp_path, files)
        assert host.hits == ["/v.bin"]

    def test_a_wrong_file_is_refused_and_left_untouched(self, tmp_path, host, files):
        (tmp_path / "v.bin").write_bytes(b"x" * len(VOICES))  # right size, wrong hash
        with pytest.raises(PreflightError, match="not the pinned file") as exc:
            _ensure(tmp_path, files)
        assert "move it aside" in str(exc.value)
        assert (tmp_path / "v.bin").read_bytes() == b"x" * len(VOICES)
        # Checked before any download: the missing model was not fetched.
        assert host.hits == []
        assert not (tmp_path / "m.onnx").exists()

    def test_a_wrong_size_is_refused(self, tmp_path, host, files):
        (tmp_path / "m.onnx").write_bytes(MODEL[:-1])
        with pytest.raises(PreflightError, match="not the pinned file"):
            _ensure(tmp_path, files)

    def test_a_download_with_the_wrong_hash_is_refused(self, tmp_path, host, files):
        host.files["/v.bin"] = b"y" * len(VOICES)
        (tmp_path / "m.onnx").write_bytes(MODEL)
        with pytest.raises(PreflightError, match="pinned sha256"):
            _ensure(tmp_path, files)
        assert not (tmp_path / "v.bin").exists()
        assert _leftovers(tmp_path) == []

    def test_a_truncated_download_is_refused(self, tmp_path, host, files):
        host.short.add("/v.bin")
        (tmp_path / "m.onnx").write_bytes(MODEL)
        with pytest.raises(PreflightError):
            _ensure(tmp_path, files)
        assert not (tmp_path / "v.bin").exists()
        assert _leftovers(tmp_path) == []

    def test_an_oversized_download_is_refused(self, tmp_path, host, files):
        host.files["/v.bin"] = VOICES + b"extra"
        (tmp_path / "m.onnx").write_bytes(MODEL)
        with pytest.raises(PreflightError, match="more than the expected"):
            _ensure(tmp_path, files)
        assert not (tmp_path / "v.bin").exists()

    def test_an_unreachable_host_says_so(self, tmp_path, files):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]  # closed once the block ends
        spec = files["v.bin"]._replace(url=f"http://127.0.0.1:{dead}/v.bin")
        with pytest.raises(PreflightError, match="Download of .* failed"):
            _ensure(tmp_path, {"v.bin": spec})
        assert _leftovers(tmp_path) == []

    def test_a_stale_part_from_a_killed_run_is_cleaned(self, tmp_path, host, files):
        (tmp_path / "m.onnx").write_bytes(MODEL)
        (tmp_path / ".v.bin.abc123.part").write_bytes(b"half")
        _ensure(tmp_path, files)
        assert _leftovers(tmp_path) == []
        assert (tmp_path / "v.bin").read_bytes() == VOICES

    def test_no_download_stops_with_the_urls(self, tmp_path, host, files):
        (tmp_path / "m.onnx").write_bytes(MODEL)
        with pytest.raises(PreflightError, match="--no-download") as exc:
            _ensure(tmp_path, files, download=False)
        assert files["v.bin"].url in str(exc.value)
        assert host.hits == []

    def test_no_download_is_fine_when_the_files_are_there(self, tmp_path, files):
        (tmp_path / "m.onnx").write_bytes(MODEL)
        (tmp_path / "v.bin").write_bytes(VOICES)
        _ensure(tmp_path, files, download=False)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_an_unwritable_directory_stops_before_downloading(
        self, tmp_path, host, files
    ):
        models = tmp_path / "ro"
        models.mkdir()
        models.chmod(0o500)
        try:
            with pytest.raises(PreflightError, match="not writable"):
                _ensure(models, files)
        finally:
            models.chmod(0o700)
        assert host.hits == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_a_read_only_directory_with_the_files_is_fine(self, tmp_path, files):
        models = tmp_path / "ro"
        models.mkdir()
        (models / "m.onnx").write_bytes(MODEL)
        (models / "v.bin").write_bytes(VOICES)
        models.chmod(0o500)
        try:
            _ensure(models, files)
        finally:
            models.chmod(0o700)

    def test_not_enough_space_stops_before_downloading(
        self, tmp_path, host, files, monkeypatch
    ):
        usage = srv.shutil.disk_usage(tmp_path)._replace(free=len(MODEL))
        monkeypatch.setattr(srv.shutil, "disk_usage", lambda path: usage)
        with pytest.raises(PreflightError, match="MB free"):
            _ensure(tmp_path, files)
        assert host.hits == []

    def test_a_full_disk_mid_download_says_so(self, tmp_path, host, files, monkeypatch):
        (tmp_path / "m.onnx").write_bytes(MODEL)
        real_fdopen = os.fdopen

        class Full:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

            def write(self, data):
                raise OSError(errno.EDQUOT, "Disk quota exceeded")

        monkeypatch.setattr(
            srv.os, "fdopen", lambda fd, mode: Full(real_fdopen(fd, mode))
        )
        with pytest.raises(PreflightError, match="Disk quota exceeded") as exc:
            _ensure(tmp_path, files)
        assert "free some space" in str(exc.value)
        assert "network" not in str(exc.value)
        assert _leftovers(tmp_path) == []

    def test_a_file_in_place_of_the_directory(self, tmp_path, files):
        (tmp_path / "models").write_text("not a dir")
        with pytest.raises(PreflightError, match="not a directory"):
            _ensure(tmp_path / "models", files)


class TestDevice:
    def test_cpu_is_forced(self):
        assert srv.choose_providers("cpu", [srv.CUDA, srv.CPU]) == ([srv.CPU], None)

    @pytest.mark.parametrize("device", ["auto", "cuda"])
    def test_cuda_first_when_available(self, device):
        providers, warning = srv.choose_providers(device, [srv.CUDA, srv.CPU])
        assert providers == [srv.CUDA, srv.CPU] and warning is None

    def test_cuda_unavailable_stops_with_cuda(self):
        with pytest.raises(PreflightError, match="no CUDA provider"):
            srv.choose_providers("cuda", [srv.CPU])

    def test_cuda_unavailable_falls_back_loudly_with_auto(self):
        providers, warning = srv.choose_providers("auto", [srv.CPU])
        assert providers == [srv.CPU]
        assert warning and warning.startswith("WARNING") and "CPU" in warning

    def test_a_session_that_fell_back_to_cpu_stops_with_cuda(self):
        with pytest.raises(PreflightError, match="CUDA could not be used"):
            srv.check_active_provider("cuda", srv.CPU)

    def test_a_session_that_fell_back_warns_with_auto(self):
        warning = srv.check_active_provider("auto", srv.CPU)
        assert warning and warning.startswith("WARNING")

    @pytest.mark.parametrize(
        ("device", "active"), [("cuda", srv.CUDA), ("auto", srv.CUDA), ("cpu", srv.CPU)]
    )
    def test_the_expected_provider_is_quiet(self, device, active):
        assert srv.check_active_provider(device, active) is None


class FakeSynth:
    voices = ["af_heart"]
    provider = "FakeProvider"

    def __init__(self, fail: Exception | None = None, audio: bytes = b"\x01\x00"):
        self.fail = fail
        self.audio = audio

    def synthesize(self, text, voice, speed):
        if self.fail:
            raise self.fail
        return self.audio


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            assert e.errno == errno.EADDRINUSE
            return False
        return True


@pytest.fixture
def models(tmp_path, monkeypatch, host, files):
    """A models directory that passes the file check against the test pins."""
    (tmp_path / "m.onnx").write_bytes(MODEL)
    (tmp_path / "v.bin").write_bytes(VOICES)
    monkeypatch.setattr(srv, "MODEL_FILES", files)
    return tmp_path


class TestStartup:
    def test_a_taken_port_fails_at_once_before_anything_slow(self, tmp_path, capsys):
        loaded = []
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            started = time.monotonic()
            with pytest.raises(SystemExit) as exc:
                srv.main(
                    ["--port", str(port), "--models", str(tmp_path / "none")],
                    synth_factory=lambda *a: loaded.append(a),
                )
            elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert "already in use" in str(exc.value.code)
        assert loaded == []
        assert not (tmp_path / "none").exists()  # no model step ran

    def test_a_bad_model_file_stops_before_the_model_loads(self, models):
        (models / "v.bin").write_bytes(b"bad")
        loaded = []
        port = _free_port()
        with pytest.raises(SystemExit) as exc:
            srv.main(
                ["--port", str(port), "--models", str(models)],
                synth_factory=lambda *a: loaded.append(a),
            )
        assert "not the pinned file" in str(exc.value.code)
        assert loaded == []
        assert _port_is_free(port)

    def test_cuda_unavailable_exits_non_zero_and_frees_the_port(self, models):
        def factory(path, device):
            assert device == "cuda"
            raise PreflightError("--device cuda, but onnxruntime offers no CUDA")

        port = _free_port()
        with pytest.raises(SystemExit) as exc:
            srv.main(
                ["--port", str(port), "--models", str(models), "--device", "cuda"],
                synth_factory=factory,
            )
        assert "no CUDA" in str(exc.value.code)
        assert _port_is_free(port)

    def test_a_model_that_fails_to_load_exits_non_zero(self, models):
        def factory(path, device):
            raise RuntimeError("bad onnx graph")

        with pytest.raises(SystemExit) as exc:
            srv.main(
                ["--port", str(_free_port()), "--models", str(models)],
                synth_factory=factory,
            )
        assert "Loading the model failed: bad onnx graph" in str(exc.value.code)

    @pytest.mark.parametrize(
        ("synth", "message"),
        [
            (FakeSynth(fail=RuntimeError("cudnn error")), "warm-up synthesis failed"),
            (FakeSynth(audio=b""), "no audio"),
        ],
    )
    def test_a_failed_warm_up_exits_non_zero(self, models, synth, message):
        port = _free_port()
        with pytest.raises(SystemExit) as exc:
            srv.main(
                ["--port", str(port), "--models", str(models)],
                synth_factory=lambda *a: synth,
            )
        assert message in str(exc.value.code)
        assert _port_is_free(port)

    def test_serves_only_once_ready(self, models, monkeypatch, capsys):
        served = []
        monkeypatch.setattr(
            ThreadingHTTPServer, "serve_forever", lambda self, *a: served.append(self)
        )
        seen = []

        def factory(path, device):
            seen.append((path, device))
            return FakeSynth()

        srv.main(
            ["--port", str(_free_port()), "--models", str(models), "--device", "cpu"],
            synth_factory=factory,
        )
        assert seen == [(models, "cpu")]
        assert len(served) == 1
        assert served[0].RequestHandlerClass is not BaseHTTPRequestHandler
        assert "Kokoro ready" in capsys.readouterr().out
