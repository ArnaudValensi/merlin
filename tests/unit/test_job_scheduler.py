"""Tests for the job scheduler (job/__init__.py)."""

import asyncio
from unittest.mock import AsyncMock, patch


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
    """_run_job_runner logs to events.jsonl on crash."""
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
                if True:
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

        created: list = []

        def mock_create_task(coro):
            # The scheduler hands the runner coroutine to the loop: close
            # it here, so the fake creates no task and awaits nothing.
            created.append(coro.__name__)
            coro.close()

        with patch("job._run_job_runner", mock_run_job_runner):
            with patch("job.asyncio.sleep", mock_sleep):
                with patch("job.asyncio.create_task", mock_create_task):
                    try:
                        await job._job_scheduler()
                    except asyncio.CancelledError:
                        pass

        # Should have created a task for _run_job_runner
        assert created == ["mock_run_job_runner"]

    asyncio.run(_test())
