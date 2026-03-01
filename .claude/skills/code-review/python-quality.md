# Python Quality — Review Reference

## Type Hints (Modern Syntax)

### Use 3.10+ Syntax

```python
# GOOD — modern syntax
def process(value: int | str) -> list[str]: ...
def maybe(x: str | None = None) -> dict[str, int]: ...

# BAD — old typing imports (unnecessary on 3.10+)
from typing import Union, Optional, List, Dict
def process(value: Union[int, str]) -> List[str]: ...
```

### Accept Abstract, Return Concrete

```python
from collections.abc import Iterable, Mapping, Sequence

# GOOD — flexible input, specific output
def process_items(items: Iterable[str]) -> list[str]:
    return [item.upper() for item in items]

def merge(base: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    return {**base, **overrides}
```

### `object` Over `Any`

```python
# BAD — Any disables type checking entirely
def log_value(v: Any) -> None: ...

# GOOD — object accepts anything but preserves type safety
def log_value(v: object) -> None:
    print(str(v))
```

Use `Any` only when the type system genuinely cannot express the type. Use `object` when you accept any value but still want type safety.

### Protocol for Structural Typing

```python
from typing import Protocol

class Renderable(Protocol):
    def render(self) -> str: ...

def display(item: Renderable) -> None:
    print(item.render())

# Any class with render() -> str works — no inheritance needed
class Widget:
    def render(self) -> str:
        return "<widget/>"

display(Widget())  # works
```

Use `Protocol` when you don't control the implementing classes. Use ABC when you own the hierarchy.

## Pydantic v2

### Separate Request / Response Models

```python
# BAD — one model for everything
class User(BaseModel):
    id: int | None = None
    email: str
    password_hash: str  # accidentally exposed in responses!

# GOOD — separate schemas
class UserCreate(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=32)

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    username: str
```

### Use v2 API

```python
# DEPRECATED v1 methods — flag these
data = user.dict()           # → user.model_dump()
user = User.parse_obj(raw)   # → User.model_validate(raw)
text = user.json()           # → user.model_dump_json()

# DEPRECATED v1 decorators
@validator("name")            # → @field_validator("name") + @classmethod
@root_validator               # → @model_validator(mode="after")
class Config:                 # → model_config = ConfigDict(...)
```

### Annotated Types for Reusable Constraints

```python
from typing import Annotated
from pydantic import Field

Slug = Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[a-z0-9-]+$')]
PositiveInt = Annotated[int, Field(gt=0)]

class CreateProject(BaseModel):
    name: str
    slug: Slug
    max_users: PositiveInt
```

### Validators

```python
from pydantic import field_validator, model_validator

class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def end_after_start(self) -> "DateRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

class User(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name cannot be blank")
        return v.strip()
```

## PEP 723 / uv Inline Dependencies

### Correct Format

```python
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "fastapi>=0.115",
#   "uvicorn>=0.34",
#   "httpx>=0.28",
# ]
# ///
```

**Checklist:**
- [ ] `# /// script` and `# ///` delimiters present and exact
- [ ] `requires-python` specified
- [ ] `dependencies` field present (even if empty: `dependencies = []`)
- [ ] Version constraints on dependencies (not bare `"httpx"` without version)
- [ ] Dependencies match what's actually imported

**Anti-patterns:**
- Missing `requires-python` — script may fail on older Python
- Bare dependency without version constraint — builds aren't reproducible
- Dependency listed but not imported (dead dep) or imported but not listed (runtime crash)

### Shebang for Direct Execution

```python
#!/usr/bin/env -S uv run --script
```

The `-S` flag is required on Linux for `env` to split arguments.

### Lock Files for Reproducibility

```bash
uv lock --script server.py  # creates server.py.lock
```

For deployment-critical scripts, lock files ensure reproducible builds.

## Error Handling Patterns

### Specific Exceptions

```python
# BAD — catches everything including CancelledError, KeyboardInterrupt
try:
    await do_work()
except Exception:
    pass

# GOOD — specific exceptions
try:
    await do_work()
except asyncio.CancelledError:
    raise  # always re-raise
except (ValueError, KeyError) as e:
    logger.error(f"Invalid data: {e}")
    return default_value
```

### Don't Silence Errors

```python
# BAD — silent failure
try:
    result = process(data)
except Exception:
    pass  # what went wrong? we'll never know

# GOOD — log and handle
try:
    result = process(data)
except ProcessingError as e:
    logger.error(f"Processing failed: {e}")
    result = fallback_value
```

### Context Managers for Resource Cleanup

```python
# BAD — resource leak on exception
f = open("data.json")
data = json.load(f)
f.close()  # never reached if json.load raises

# GOOD — context manager guarantees cleanup
with open("data.json") as f:
    data = json.load(f)

# Async equivalent
async with aiofiles.open("data.json") as f:
    content = await f.read()
```

## String Formatting

```python
# GOOD — f-strings (fast, readable)
msg = f"User {user.name} created project {project.slug}"

# GOOD — logging with % formatting (lazy evaluation)
logger.debug("Processing %s items for user %s", len(items), user_id)

# BAD — f-string in logging (evaluated even if log level is too low)
logger.debug(f"Processing {len(items)} items for user {user_id}")
```

For logging: use `%` formatting (lazy) or structlog. For everything else: f-strings.

## Path Handling

```python
# GOOD — pathlib for all path operations
from pathlib import Path

config_dir = Path.home() / ".merlin"
config_file = config_dir / "config.env"

if config_file.exists():
    content = config_file.read_text()

# BAD — string concatenation for paths
config_file = os.environ["HOME"] + "/.merlin/config.env"
```

## Constants and Configuration

```python
# GOOD — module-level constants, uppercase
MAX_UPLOAD_SIZE = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 30
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".gif"})

# BAD — magic numbers scattered in code
if len(content) > 10485760:  # what is this number?
    raise ValueError("Too large")
```

## Mutable Default Arguments

```python
# BAD — shared mutable default (classic Python gotcha)
def add_item(item: str, items: list[str] = []) -> list[str]:
    items.append(item)  # mutates the default!
    return items

# GOOD — None sentinel
def add_item(item: str, items: list[str] | None = None) -> list[str]:
    if items is None:
        items = []
    items.append(item)
    return items
```
