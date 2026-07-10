"""Tests for type-aware job loading in job.runner.load_job."""

import json

import pytest

pytest.importorskip("croniter")

from job.runner import load_job


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def test_accepts_command_job_without_prompt(tmp_path):
    path = _write(
        tmp_path,
        "cmd.json",
        {"schedule": "0 9 * * *", "type": "command", "command": "echo hi"},
    )
    job = load_job(path)
    assert job is not None
    assert job["command"] == "echo hi"


def test_accepts_prompt_job_without_command(tmp_path):
    path = _write(
        tmp_path,
        "prm.json",
        {"schedule": "0 9 * * *", "prompt": "do something"},
    )
    job = load_job(path)
    assert job is not None
    assert job["prompt"] == "do something"


def test_rejects_command_job_missing_command(tmp_path):
    path = _write(
        tmp_path,
        "bad.json",
        {"schedule": "0 9 * * *", "type": "command"},
    )
    assert load_job(path) is None


def test_rejects_prompt_job_missing_prompt(tmp_path):
    path = _write(
        tmp_path,
        "bad.json",
        {"schedule": "0 9 * * *", "type": "prompt"},
    )
    assert load_job(path) is None


def test_rejects_job_missing_schedule(tmp_path):
    path = _write(tmp_path, "bad.json", {"type": "command", "command": "echo hi"})
    assert load_job(path) is None
