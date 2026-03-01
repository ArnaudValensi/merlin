# FastAPI Patterns — Review Reference

## Dependency Injection

### Composable Auth Dependencies

```python
# GOOD — composable, reusable
async def get_current_user(request: Request) -> User:
    session = verify_cookie(request)
    if not session:
        raise HTTPException(401, "Not authenticated")
    return session.user

async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Not an admin")
    return user

@app.get("/admin/stats")
async def admin_stats(user: User = Depends(require_admin)):
    ...
```

**Anti-pattern: Auth logic inline in handlers**

```python
# BAD — duplicated in every route
@app.get("/dashboard")
async def dashboard(request: Request):
    session = verify_cookie(request)
    if not session:
        return RedirectResponse("/login")
    user = session.user
    ...

@app.get("/settings")
async def settings(request: Request):
    session = verify_cookie(request)  # same code repeated
    if not session:
        return RedirectResponse("/login")
    ...
```

### Sync vs Async Dependencies

```python
# BAD — sync dependency runs in threadpool unnecessarily
def get_settings():
    return Settings()  # no I/O, pure computation

# GOOD — async, runs on event loop (no threadpool overhead)
async def get_settings():
    return Settings()
```

Rule: use `async def` for dependencies unless they do actual blocking I/O (in which case `def` runs them in a threadpool safely).

## Lifespan Events

```python
# GOOD — modern lifespan context manager
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    app.state.http_client = httpx.AsyncClient()
    yield
    # SHUTDOWN
    await app.state.http_client.aclose()

app = FastAPI(lifespan=lifespan)
```

**Anti-pattern: Deprecated event handlers**

```python
# DEPRECATED — will be removed, silently ignored when lifespan is used
@app.on_event("startup")
async def startup():
    app.state.client = httpx.AsyncClient()
```

**Why:** Lifespan co-locates init and cleanup — impossible to forget cleanup. `on_event` separates them, making leaks easy.

## Jinja2 Templates

### Correct Pattern

```python
templates = Jinja2Templates(directory="templates")

@app.get("/dashboard")
async def dashboard(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "stats": await get_stats()}
    )
```

**Rules:**
- Always pass `request` in the context dict
- User input goes in context variables, never in template source
- Use `url_for()` in templates for routes and static paths
- Jinja2 autoescaping is enabled by default in FastAPI — prevents XSS

### Template Organization

```
templates/
    base.html           # Layout shell (nav, sidebar, scripts)
    login.html          # Standalone (doesn't extend base)
    dashboard.html      # {% extends "base.html" %}
    partials/
        _stats.html     # Reusable fragments ({% include %})
```

## Error Handling

### Exception Handlers (Preferred)

```python
class AppError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "error": exc.detail},
        status_code=exc.status_code,
    )

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return templates.TemplateResponse(
        "404.html", {"request": request}, status_code=404
    )
```

**Anti-pattern: Scattered try/except in every handler**

```python
# BAD — repeated error handling
@app.get("/users/{id}")
async def get_user(id: str):
    try:
        user = await find_user(id)
        if not user:
            return templates.TemplateResponse("404.html", ...)
    except DatabaseError:
        return templates.TemplateResponse("error.html", ...)
    except ValidationError:
        return templates.TemplateResponse("error.html", ...)
```

### Error Messages in SSR vs API

For SSR (Merlin dashboard): render an error template with user-friendly message.
For internal errors: log details server-side, show generic message to user.

```python
# BAD — leaks internals
raise HTTPException(500, f"Database query failed: {query} for user {user_id}")

# GOOD — generic to user, detailed in logs
logger.error(f"Database query failed: {query} for user {user_id}")
raise AppError(500, "Something went wrong. Please try again.")
```

## Router Organization

```python
# routes/dashboard.py
router = APIRouter(prefix="", tags=["dashboard"])

@router.get("/")
async def dashboard(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("dashboard.html", {...})

# main.py
app.include_router(dashboard_router)
app.include_router(files_router)
app.include_router(terminal_router)
```

**Pattern:** Each module (files, terminal, commits, notes) owns its router, templates, and static files. `main.py` composes them.

## Static Files

```python
# More specific mounts first, catch-all last
app.mount("/static/files", StaticFiles(directory="files/static"))
app.mount("/static/terminal", StaticFiles(directory="terminal/static"))
app.mount("/static", StaticFiles(directory="static"))  # catch-all last
```

**Why order matters:** FastAPI/Starlette tries mounts in order. A catch-all `/static` mount first would intercept `/static/files/` requests.

## CORS Configuration

```python
# BAD — wildcard with credentials
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,  # browsers reject this combination
)

# GOOD — specific origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://dashboard.example.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
)
```

For Merlin: CORS may not be needed since the dashboard is served by the same FastAPI app. Only needed if external services (portal, Discord) call the API.

## WebSocket Patterns

```python
@app.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket, user: User = Depends(get_ws_user)):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await process_terminal_input(data)
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    finally:
        await cleanup_session()  # always clean up
```

**Anti-pattern: No cleanup on disconnect**

```python
# BAD — resources leak on disconnect
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()  # raises on disconnect
        # cleanup never runs
```
