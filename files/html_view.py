"""Sandboxed HTML preview: capability tokens and the public serving route.

An HTML file previewed in Files must never run with the dashboard's
privileges: same-origin with the dashboard, any script in it could drive
``/api/terminal`` (a shell) just because the user stepped onto the file. So the
page runs in an opaque origin, twice over: the viewer's iframe is
``sandbox``-ed without ``allow-same-origin``, and every response here carries
``Content-Security-Policy: sandbox ...`` so a page opened in its own tab is
sandboxed too.

An opaque-origin page sends no session cookie on its subresource requests, so
this route cannot sit behind ``require_auth``. It authenticates by a token in
the URL instead: ``/files-view/<token>/<relative path>``. The token grants
read access to one root directory (the HTML file's folder) until it expires,
and relative URLs in the page resolve under it naturally. Hidden path segments
are refused so ``.git``, ``.env`` or ``.ssh`` never leak even when the root is
a home directory. The token is signed with a per-process secret: a restart
invalidates every preview link, and the viewer mints a fresh one on each open.

Accepted residual risk (a product decision): a hostile page can read the
non-hidden files in its own folder tree and send them out, since outbound
network stays open so CDNs keep working.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response

from .fs_helpers import HTML_EXTENSIONS, validate_path

TOKEN_TTL_SECONDS = 12 * 3600

_SECRET = secrets.token_bytes(32)

# Granted to the page: everything an app needs except same-origin (which would
# hand it the dashboard) and top navigation (which would let it replace the
# dashboard). The viewer's iframe uses the same list.
SANDBOX_FLAGS = (
    "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox "
    "allow-modals allow-downloads allow-pointer-lock"
)

VIEW_HEADERS = {
    "Content-Security-Policy": f"sandbox {SANDBOX_FLAGS}",
    # The page's origin is "null": module scripts and fetch() are CORS
    # requests. The token in the URL is the gate, not the origin.
    "Access-Control-Allow-Origin": "*",
    # The token must not ride the Referer to every CDN the page loads.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

# Opaque origins throw on localStorage, sessionStorage and document.cookie.
# Swap in in-memory versions so apps run instead of crashing; state is lost on
# reload. Runs first in the document, before any page script.
STORAGE_SHIM = """<script>(function(){
function mem(){var d=Object.create(null);return{
getItem:function(k){k=String(k);return k in d?d[k]:null},
setItem:function(k,v){d[String(k)]=String(v)},
removeItem:function(k){delete d[String(k)]},
clear:function(){d=Object.create(null)},
key:function(i){var ks=Object.keys(d);return i<ks.length?ks[i]:null},
get length(){return Object.keys(d).length}}}
["localStorage","sessionStorage"].forEach(function(n){
try{window[n];return}catch(e){}
try{Object.defineProperty(window,n,{value:mem(),configurable:true})}catch(e){}});
try{document.cookie}catch(e){var jar=Object.create(null);
try{Object.defineProperty(document,"cookie",{configurable:true,
get:function(){return Object.keys(jar).map(function(k){return k+"="+jar[k]}).join("; ")},
set:function(v){var p=String(v).split(";")[0],i=p.indexOf("=");
if(i>0)jar[p.slice(0,i).trim()]=p.slice(i+1).trim()}})}catch(e){}}
})();</script>"""

_HEAD_RE = re.compile(rb"<head(\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_token(root: Path, now: float | None = None) -> str:
    """Token granting read access under ``root`` for ``TOKEN_TTL_SECONDS``."""
    expiry = int((now if now is not None else time.time()) + TOKEN_TTL_SECONDS)
    encoded = base64.urlsafe_b64encode(str(root).encode()).decode().rstrip("=")
    payload = f"{encoded}.{expiry}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str, now: float | None = None) -> Path | None:
    """The root a valid, unexpired token grants, else ``None``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    encoded, expiry_s, signature = parts
    payload = f"{encoded}.{expiry_s}"
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        expiry = int(expiry_s)
        root = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except ValueError:
        return None
    if expiry < (now if now is not None else time.time()):
        return None
    return Path(root)


def view_url(html_path: Path) -> str:
    """Preview URL for an HTML file, scoped to its folder."""
    return f"/files-view/{make_token(html_path.parent)}/{html_path.name}"


def resolve_in_root(root: Path, rel: str) -> Path | None:
    """The file ``rel`` names under ``root``, or ``None`` if it is out of
    scope: hidden segment, escapes the root (``..`` or a symlink), blocked by
    ``validate_path``, missing, or not a regular file."""
    segments = [s for s in rel.split("/") if s]
    if not segments or any(s.startswith(".") for s in segments):
        return None
    try:
        root_resolved = validate_path(str(root))
        target = validate_path(str(root_resolved.joinpath(*segments)))
    except ValueError:
        return None
    if not target.is_relative_to(root_resolved) or not target.is_file():
        return None
    return target


def inject_shim(document: bytes) -> bytes:
    """Insert the storage shim before any page script: after ``<head>``, else
    after the doctype (prepending before it would force quirks mode), else at
    the very start."""
    shim = STORAGE_SHIM.encode()
    for pattern in (_HEAD_RE, _DOCTYPE_RE):
        match = pattern.search(document)
        if match:
            return document[: match.end()] + shim + document[match.end() :]
    return shim + document


def register_routes(app: FastAPI) -> None:
    """Mount the public, token-gated preview route (outside ``/api``, like
    ``/webhooks``: the self-authenticating boundary stays legible by URL)."""

    @app.api_route("/files-view/{token}/{rel:path}", methods=["GET", "HEAD"])
    def files_view(token: str, rel: str) -> Response:
        root = verify_token(token)
        if root is None:
            raise HTTPException(
                status_code=403,
                detail="This preview link has expired. Reopen the file from Files.",
                headers=VIEW_HEADERS,
            )
        target = resolve_in_root(root, rel)
        if target is None:
            return PlainTextResponse("Not found", status_code=404, headers=VIEW_HEADERS)
        if target.suffix.lower() in HTML_EXTENSIONS:
            try:
                document = target.read_bytes()
            except PermissionError:
                return PlainTextResponse(
                    "Permission denied", status_code=403, headers=VIEW_HEADERS
                )
            return Response(
                inject_shim(document), media_type="text/html", headers=VIEW_HEADERS
            )
        return FileResponse(path=str(target), headers=VIEW_HEADERS)
