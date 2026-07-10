"""Tests for the job scheduler (job/__init__.py)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch


def test_start_creates_task():
    """start() creates a background asyncio task."""
    import job

    async def _test():
        # Track tasks created
        created_tasks = []
        original_create_task = asyncio.create_task

        def tracking_create_task(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task

        with patch("job.asyncio.create_task", side_effect=tracking_create_task):
            await job.start()

        assert len(created_tasks) == 1
        # Clean up — cancel the scheduler task so it doesn't run forever
        created_tasks[0].cancel()
        try:
            await created_tasks[0]
        except asyncio.CancelledError:
            pass

    asyncio.run(_test())


def test_run_job_runner_logs_crash():
    """_run_job_runner logs to engine-log.jsonl on crash."""
    import job

    async def _test():
        # Mock subprocess that fails
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"some error"))

        with patch("job.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("job.log_event") as mock_log:
                await job._run_job_runner()

                # Should have logged the crash
                mock_log.assert_called_once()
                call_kwargs = mock_log.call_args
                assert call_kwargs[0][0] == "job_runner_crash"
                assert call_kwargs[1]["exit_code"] == 1
                assert "some error" in call_kwargs[1]["stderr"]

    asyncio.run(_test())


def test_run_job_runner_no_log_on_success():
    """_run_job_runner does NOT log crash on successful exit."""
    import job

    async def _test():
        # Mock subprocess that succeeds
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("job.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("job.log_event") as mock_log:
                with patch("job._process_runner_output"):
                    await job._run_job_runner()

                    # Should NOT have logged a crash
                    mock_log.assert_not_called()

    asyncio.run(_test())


def test_scheduler_fires_runner():
    """_job_scheduler fires _run_job_runner at minute boundary."""
    import job

    async def _test():
        # Patch sleep to return immediately and only loop once
        sleep_calls = 0

        async def mock_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError()

        async def mock_run_job_runner():
            pass

        with patch("job._run_job_runner", mock_run_job_runner):
            with patch("job.asyncio.sleep", mock_sleep):
                with patch("job.asyncio.create_task") as mock_create_task:
                    try:
                        await job._job_scheduler()
                    except asyncio.CancelledError:
                        pass

        # Should have created a task for _run_job_runner
        assert mock_create_task.call_count >= 1

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Notification wiring: runner stdout → _process_runner_output → notify
# ---------------------------------------------------------------------------


def test_process_runner_output_calls_notify():
    """_process_runner_output parses job_complete JSON and calls notify."""
    import job

    job_data = {"description": "Test job", "schedule": "0 * * * *"}
    result_data = {
        "exit_code": 0,
        "duration_seconds": 10,
        "cost_usd": 0.01,
        "output": "done",
    }

    stdout_line = json.dumps(
        {
            "type": "job_complete",
            "job_id": "test-job",
            "job": job_data,
            "result": result_data,
        }
    )

    mock_registry = {"merlin-bot": MagicMock(loaded=True, module=MagicMock())}

    with patch("job.notify.notify_job_result") as mock_notify:
        with patch("main.extension_registry", mock_registry):
            job._process_runner_output(stdout_line.encode())

    mock_notify.assert_called_once_with(
        job_id="test-job",
        job=job_data,
        result=result_data,
        extension_registry=mock_registry,
    )


def test_process_runner_output_skips_non_json():
    """Non-JSON lines in stdout are silently skipped."""
    import job

    stdout = b"INFO: some log message\nnot json\n"

    with patch("job.notify.notify_job_result") as mock_notify:
        with patch("main.extension_registry", {}):
            job._process_runner_output(stdout)

    mock_notify.assert_not_called()


def test_process_runner_output_skips_non_job_complete():
    """JSON lines without type=job_complete are ignored."""
    import job

    stdout = json.dumps({"type": "other_event", "data": "stuff"}).encode()

    with patch("job.notify.notify_job_result") as mock_notify:
        with patch("main.extension_registry", {}):
            job._process_runner_output(stdout)

    mock_notify.assert_not_called()


def test_process_runner_output_handles_multiple_jobs():
    """Multiple job_complete lines trigger multiple notifications."""
    import job

    lines = []
    for i in range(3):
        lines.append(
            json.dumps(
                {
                    "type": "job_complete",
                    "job_id": f"job-{i}",
                    "job": {"description": f"Job {i}"},
                    "result": {"exit_code": 0},
                }
            )
        )
    stdout = "\n".join(lines).encode()

    mock_registry = {"merlin-bot": MagicMock(loaded=True, module=MagicMock())}

    with patch("job.notify.notify_job_result") as mock_notify:
        with patch("main.extension_registry", mock_registry):
            job._process_runner_output(stdout)

    assert mock_notify.call_count == 3


def test_process_runner_output_notify_failure_doesnt_crash():
    """If notify raises, other jobs still get processed."""
    import job

    lines = []
    for i in range(2):
        lines.append(
            json.dumps(
                {
                    "type": "job_complete",
                    "job_id": f"job-{i}",
                    "job": {},
                    "result": {"exit_code": 0},
                }
            )
        )
    stdout = "\n".join(lines).encode()

    call_count = 0

    def failing_notify(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Discord down")

    with patch("job.notify.notify_job_result", side_effect=failing_notify):
        with patch("main.extension_registry", {}):
            job._process_runner_output(stdout)

    # Both jobs were attempted despite first failing
    assert call_count == 2


def test_run_job_runner_calls_process_output():
    """_run_job_runner calls _process_runner_output with stdout on success."""
    import job

    stdout_data = b'{"type": "job_complete", "job_id": "x", "job": {}, "result": {}}\n'

    async def _test():
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(stdout_data, b""))

        with patch("job.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("job._process_runner_output") as mock_process:
                await job._run_job_runner()

        mock_process.assert_called_once_with(stdout_data)

    asyncio.run(_test())
