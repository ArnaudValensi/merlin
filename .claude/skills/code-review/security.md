# Security — Review Reference

## Template Injection (SSTI) — Critical

Jinja2 autoescaping prevents XSS (user input in context is escaped). But SSTI happens when user input becomes part of the template **source code** itself.

```python
# CRITICAL — RCE vulnerability
from jinja2 import Template
template = Template(f"Hello {user_input}!")  # user_input IS the template
result = template.render()
# Attacker: user_input = "{{config}}" → leaks app config
# Attacker: user_input = "{{''.__class__.__mro__[1].__subclasses__()}}" → code execution

# ALSO BAD — string formatting into template source
template_str = f"<h1>{title}</h1>"
Template(template_str).render()

# GOOD — user input in context only
templates.TemplateResponse("page.html", {"request": request, "title": title})
# Template file: <h1>{{ title }}</h1> — auto-escaped
```

**Review pattern:** Search for `Template(` with any variable in the constructor. Every instance is a potential SSTI.

## Command Injection

```python
# BAD — shell injection
subprocess.run(f"convert {user_file} output.png", shell=True)
os.system(f"git log {branch}")
os.popen(f"cat {filename}")

# BAD — even without shell=True, string splitting is dangerous
subprocess.run(f"convert {user_file} output.png".split())

# GOOD — argument list, no shell
subprocess.run(["convert", user_file, "output.png"], check=True, timeout=30)

# GOOD — async equivalent
proc = await asyncio.create_subprocess_exec(
    "convert", user_file, "output.png",
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
```

**Dangerous functions to flag:**
- `os.system()`
- `os.popen()`
- `subprocess.*(shell=True)`
- `asyncio.create_subprocess_shell()`
- Any string formatting/concatenation into command strings

## Path Traversal

```python
# BAD — user controls file path directly
@app.get("/files/{filename}")
async def get_file(filename: str):
    return FileResponse(f"/app/uploads/{filename}")  # ../../../etc/passwd

# BAD — os.path.join doesn't prevent traversal
path = os.path.join("/app/uploads", user_input)  # "../../etc/passwd" works

# BAD — simple string filtering is bypassable
if ".." in filename:  # URL encoding, double encoding, etc. bypass this
    raise ValueError("Invalid")

# GOOD — resolve and verify containment
from pathlib import Path

BASE_DIR = Path("/app/uploads").resolve()

def safe_path(filename: str) -> Path:
    requested = (BASE_DIR / filename).resolve()
    if not requested.is_relative_to(BASE_DIR):
        raise ValueError("Path traversal detected")
    return requested
```

**Review pattern:** Search for `FileResponse`, `open()`, `Path()` where the path includes user input. Verify containment check.

## Cookie Security

```python
# GOOD — all security flags set
response.set_cookie(
    key="session_id",
    value=session_token,
    httponly=True,    # prevents JavaScript access (XSS mitigation)
    secure=True,      # HTTPS only
    samesite="lax",   # CSRF mitigation
    max_age=1800,     # 30-minute expiry
    path="/",
)

# BAD — no flags
response.set_cookie("session", token)  # JS-readable, sent over HTTP, no CSRF protection
```

**Checklist for every `set_cookie` call:**
- [ ] `httponly=True` (unless JavaScript legitimately needs to read it)
- [ ] `secure=True` (may need to be False in dev without HTTPS)
- [ ] `samesite="lax"` or `"strict"`
- [ ] `max_age` set (not infinite sessions)

## Secrets Management

```python
# BAD — hardcoded secrets
DISCORD_TOKEN = "MTQ2ODY2..."
API_KEY = "sk-abc123..."

# BAD — secrets in code comments
# Old token: MTQ2ODY2NTUxNTk3NDIwMTQxMA.GA3tsL...

# GOOD — from environment / config file
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
```

**Review pattern:** Search for patterns that look like tokens/keys: strings starting with `sk-`, `MTQ`, long base64 strings, anything that looks like a credential.

**Config file permissions:**

```python
# GOOD — restrictive permissions on config
os.chmod(config_path, 0o600)  # owner read/write only
```

## File Upload Security

**Checklist:**
1. Validate file type by **magic bytes** (not extension or Content-Type — both user-controlled)
2. Enforce **size limits** before reading entire upload into memory
3. **Rename uploaded files** — never use original filename (use UUID)
4. Store uploads **outside web root**
5. **Sanitize filenames** if you must preserve them

```python
import magic
import uuid

ALLOWED_MIMES = {"image/png", "image/jpeg", "image/gif"}
MAX_SIZE = 10 * 1024 * 1024  # 10 MB

@app.post("/upload")
async def upload(file: UploadFile):
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File too large")

    mime = magic.from_buffer(content, mime=True)
    if mime not in ALLOWED_MIMES:
        raise HTTPException(415, f"Unsupported type: {mime}")

    safe_name = f"{uuid.uuid4()}{mimetypes.guess_extension(mime) or ''}"
    dest = UPLOAD_DIR / safe_name
    async with aiofiles.open(dest, "wb") as f:
        await f.write(content)
    return {"filename": safe_name}
```

## Auth Patterns for SSR

For Merlin's cookie-based auth (HMAC-signed session cookies):

```python
# GOOD — auth as a dependency
async def get_current_user(request: Request) -> User:
    cookie = request.cookies.get("session")
    if not cookie:
        raise HTTPException(status_code=401)
    user = verify_hmac_cookie(cookie)
    if not user:
        raise HTTPException(status_code=401)
    return user

# Apply to routes
@app.get("/dashboard")
async def dashboard(request: Request, user: User = Depends(get_current_user)):
    ...
```

**Anti-pattern: Auth check missing on routes**

Review all route handlers — each one should either:
- Have `Depends(get_current_user)` or similar auth dependency
- Be explicitly public (login page, health check, static files)

## CSRF for SSR Apps

FastAPI has no built-in CSRF protection. For cookie-based auth SSR apps:

- **SameSite=Lax cookies** provide baseline protection (blocks cross-site POST from foreign sites)
- **Custom header check** adds defense-in-depth (verify `X-Requested-With` header on mutations)
- **CSRF tokens** for maximum protection (double-submit cookie pattern)

API endpoints using bearer tokens in `Authorization` header are naturally CSRF-resistant.

## Error Information Leakage

```python
# BAD — leaks internals
raise HTTPException(500, f"Query failed: SELECT * FROM users WHERE id={user_id}")

# GOOD — generic error, log details
logger.error(f"Query failed for user {user_id}: {traceback.format_exc()}")
raise HTTPException(500, "Internal error")
```

Don't expose: table names, query strings, file paths, stack traces, internal IDs.
