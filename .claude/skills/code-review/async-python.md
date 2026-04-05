# Async Python — Review Reference

## Blocking the Event Loop

The cardinal sin of async Python. The event loop is single-threaded — if any coroutine does synchronous I/O or CPU-heavy work without yielding, ALL other coroutines are blocked.

### Common Blockers to Flag

| Blocking Call | Async Alternative |
|---|---|
| `time.sleep(n)` | `asyncio.sleep(n)` |
| `requests.get(url)` | `await httpx_client.get(url)` |
| `open(f).read()` | `aiofiles.open(f)` or `asyncio.to_thread(...)` |
| `subprocess.run(cmd)` | `asyncio.create_subprocess_exec(...)` |
| `os.listdir(path)` | `asyncio.to_thread(os.listdir, path)` |
| `json.loads(huge_string)` | `asyncio.to_thread(json.loads, huge_string)` |
| `hashlib.pbkdf2_hmac(...)` | `asyncio.to_thread(hashlib.pbkdf2_hmac, ...)` |

```python
# BAD — freezes ALL concurrent requests
async def get_data():
    time.sleep(5)           # blocks event loop
    data = requests.get(url) # blocks event loop
    return data.json()

# GOOD — properly async
async def get_data():
    await asyncio.sleep(5)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
    return resp.json()
```

### FastAPI's Dual Execution Model

- `async def` handlers run directly on the event loop — blocking calls freeze the server
- `def` (sync) handlers run in a threadpool automatically — blocking calls are safe but limited

```python
# OK — sync handler, runs in threadpool
@app.get("/data")
def get_data():
    time.sleep(5)  # only blocks this thread, not the event loop
    return requests.get(url).json()

# DANGEROUS — async handler with blocking call
@app.get("/data")
async def get_data():
    time.sleep(5)  # blocks the ENTIRE event loop
```

## gather vs TaskGroup

### Sequential vs Concurrent

```python
# BAD — sequential when independent (6 seconds total)
result_a = await fetch_a()  # 3s
result_b = await fetch_b()  # 2s
result_c = await fetch_c()  # 1s

# GOOD — concurrent with gather (3 seconds total)
result_a, result_b, result_c = await asyncio.gather(
    fetch_a(), fetch_b(), fetch_c()
)
```

### gather Error Handling

```python
# Default: first exception propagates, other tasks keep running
results = await asyncio.gather(task_a(), task_b())

# With return_exceptions: exceptions become return values
results = await asyncio.gather(task_a(), task_b(), return_exceptions=True)
for r in results:
    if isinstance(r, Exception):
        logger.error(f"Task failed: {r}")
```

### TaskGroup (Python 3.11+, Preferred)

```python
# BETTER — cancels remaining tasks on first failure
async with asyncio.TaskGroup() as tg:
    task_a = tg.create_task(fetch_a())
    task_b = tg.create_task(fetch_b())
# If fetch_a() raises, fetch_b() is cancelled automatically
results = (task_a.result(), task_b.result())
```

**Why TaskGroup > gather:** `gather()` does not cancel other tasks when one fails. `TaskGroup` provides stronger safety by cancelling siblings on failure.

## Task Cancellation

```python
# GOOD — handle CancelledError, clean up, re-raise
async def monitor_loop(session_id: str):
    try:
        while True:
            await check_status(session_id)
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        logger.info(f"Monitor {session_id} cancelled, cleaning up")
        await cleanup_session(session_id)
        raise  # ALWAYS re-raise CancelledError
```

**Anti-pattern: Swallowing CancelledError**

```python
# BAD — prevents proper cancellation
async def worker():
    try:
        await do_work()
    except Exception:  # catches CancelledError too!
        pass  # task can never be cancelled

# GOOD — be specific
async def worker():
    try:
        await do_work()
    except asyncio.CancelledError:
        raise  # always re-raise
    except Exception as e:
        logger.error(f"Worker error: {e}")
```

## Background Tasks

### Fire-and-forget with lifecycle management

```python
# BAD — task can be garbage collected, no error handling
asyncio.create_task(long_running_work())

# GOOD — track tasks, handle errors
active_tasks: dict[str, asyncio.Task] = {}

async def start_task(task_id: str, coro):
    task = asyncio.create_task(coro)
    active_tasks[task_id] = task
    task.add_done_callback(lambda t: _handle_done(task_id, t))

def _handle_done(task_id: str, task: asyncio.Task):
    active_tasks.pop(task_id, None)
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error(f"Task {task_id} failed: {exc}")
```

### FastAPI BackgroundTasks

```python
# GOOD — for post-response work
@app.post("/action")
async def do_action(background_tasks: BackgroundTasks):
    result = await perform_action()
    background_tasks.add_task(send_notification_safe, result)
    return result

# Always wrap with error handling
async def send_notification_safe(result):
    try:
        await send_notification(result)
    except Exception as e:
        logger.error(f"Notification failed: {e}")
```

## Async File I/O

Python has no true async file I/O at the OS level. `aiofiles` uses a threadpool. Still useful because it doesn't block the event loop.

```python
# BAD — blocks event loop
async def read_config():
    with open("config.json") as f:
        return json.load(f)

# GOOD — aiofiles
async def read_config():
    async with aiofiles.open("config.json") as f:
        content = await f.read()
    return json.loads(content)

# ALSO GOOD — explicit threadpool
async def read_config():
    return await asyncio.to_thread(load_config_sync)
```

## httpx Client Lifecycle

```python
# BAD — new client per request (no connection pooling)
async def fetch():
    async with httpx.AsyncClient() as client:
        return await client.get(url)

# GOOD — shared client with connection pooling
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=httpx.Limits(max_keepalive_connections=20),
    )
    yield
    await app.state.http_client.aclose()
```

## Timeout Configuration

```python
client = httpx.AsyncClient(
    timeout=httpx.Timeout(
        connect=5.0,   # establish connection
        read=30.0,     # wait for response
        write=10.0,    # send request
        pool=5.0,      # wait for pool slot
    )
)

# Per-request override for slow endpoints
resp = await client.get("/export", timeout=httpx.Timeout(read=120.0))
```

Always configure timeouts. The default is 5s for everything, which may be too short for some operations but too generous for connection establishment.
