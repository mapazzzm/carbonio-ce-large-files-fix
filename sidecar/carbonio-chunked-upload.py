#!/usr/bin/env python3
"""
Carbonio Files chunked upload sidecar.

Accepts parallel chunks via POST /upload-chunked?session=<uuid>&part=N&total=M
and, once all parts arrive, concatenates them and forwards the complete payload
to the Java carbonio-files endpoint at http://127.78.0.2:10000/upload.

The session-id is opaque to the server; the client chooses any unique string.
Parts can arrive in any order and concurrently — there is one asyncio.Lock per
session so only one task finalises.
"""
import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import struct
import time
import zlib
from pathlib import Path

import aiofiles
import asyncpg
from aiohttp import ClientSession, ClientTimeout, web

# --- Config ---------------------------------------------------------------
CHUNK_DIR = Path("/opt/zextras/chunked-upload-tmp")
JAVA_UPLOAD_URL = "http://127.78.0.2:10000/upload"
# Java public-link endpoint reached through envoy (same upstream nginx uses for
# the normal /services/files/ path). Used to transparently delegate access-code
# protected links, whose code-entry/redirect flow we don't replicate here.
JAVA_PUBLIC_LINK_URL = "http://127.78.0.1:20000/public/link/download"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 5795

# Single chunk size limit (must comfortably exceed configured client chunk size)
MAX_CHUNK_BYTES = 200 * 1024 * 1024  # 200 MB

# Total upload size limit (single file via this path)
MAX_TOTAL_BYTES = 100 * 1024 * 1024 * 1024  # 100 GB

# Java upload timeout for the consolidated request — disk read + LAN POST.
JAVA_TIMEOUT_SECONDS = 7200  # 2 hours, matches IIS settings

# Tunables
DISK_READ_BUF = 1 * 1024 * 1024   # 1 MiB while concatenating chunks
HTTP_READ_BUF = 256 * 1024        # 256 KiB while streaming the body in

# Reaper: drop session bookkeeping for sessions inactive longer than the TTL,
# and sweep orphan dirs (e.g. left behind by a sidecar crash mid-upload).
SESSION_TTL_SECONDS = 24 * 3600
# Abandoned staging dirs (no matching in-memory entry — never finalised) are
# swept far sooner so a flood of half-finished uploads can't sit on disk for a
# full day. An active upload keeps the dir's mtime fresh, so this never touches
# a transfer still in progress.
ORPHAN_DIR_TTL_SECONDS = 6 * 3600
REAPER_INTERVAL_SECONDS = 600

# Cap concurrent staging dirs. The nginx gate only checks for the *presence* of
# a ZM_AUTH_TOKEN cookie (real auth happens at the final POST to Java), so a
# forged cookie could otherwise open unbounded sessions and fill the disk. This
# bound is far above any legitimate concurrency; new sessions past it get 503.
MAX_ACTIVE_SESSIONS = 256

# Orphan DB nodes: Java creates a node row before writing the revision/blob.
# If the upload is interrupted (page refresh, browser close, network drop),
# the node stays in the DB with size=0 and no revision row — causing the
# "this version cannot be found" error in the Files UI. Clean up after 1 hour.
ORPHAN_DB_TTL_SECONDS = 3600

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Defensive: strip CR/LF/NUL and cap length on header values we relay to Java.
# aiohttp's parser would normally reject CRLF in incoming headers, but we do
# our own sanitisation so a malformed client (or future parser change) can't
# inject extra headers via filename/parentid. Java has its own validation;
# this is just belt-and-braces in our hop.
_HEADER_BAD_RE = re.compile(r"[\r\n\x00]")
_MAX_RELAY_HEADER_LEN = 1024


def sanitize_relay_header(val: str) -> str:
    return _HEADER_BAD_RE.sub("", val)[:_MAX_RELAY_HEADER_LEN]

# Public-link download proxy ----------------------------------------------
# Java BlobController streams blobs via OkHttp and crashes with EOFException
# on large files (storages closes its idle TCP connection mid-stream). We
# bypass Java entirely: resolve token -> node_id via the DB, then send the
# blob directly from disk with sendfile. Works for any file size.
PUBLIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{32,128}$")
NODE_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
BLOBS_ROOT = Path("/opt/zextras/carbonio-storages/blobs")
DB_NAME = "carbonio-files-db"   # postgres database (peer-auth as zextras)
_db_pool = None
# --------------------------------------------------------------------------

# Public folder ZIP download (POST /services/files/public/download-multiple) ---
# Carbonio's Java endpoint streams a ZIP by fetching each member blob from
# storages over OkHttp — the same path that EOFs/OOMs on large blobs. On a
# folder of several GB the ZIP is truncated (~512 KiB) and the browser saves
# nothing. We rebuild the ZIP here: validate the public link + access code and
# subtree membership against the DB, then stream a ZIP64 archive assembled
# straight from the blobs on disk. Any size / file count is fine.
# Public link ids are user-visible tokens (varchar); accept a permissive set.
PUBLIC_LINK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,255}$")
# Guard rails so a crafted request can't explode memory / walk forever.
MAX_ZIP_SELECTED = 1000        # top-level nodeIds accepted in one request
MAX_ZIP_ENTRIES = 100_000      # total files+dirs emitted into one archive
ZIP_READ_BUF = 1024 * 1024     # blob read chunk while zipping
# Strip path separators / control chars from a node name used as one path
# component, so a name like "A / B" can't create phantom directories or escape.
_ZIP_NAME_BAD_RE = re.compile(r"[/\\\r\n\x00]")


def zip_component(name: str) -> str:
    s = _ZIP_NAME_BAD_RE.sub("_", name or "").strip().strip(".")
    return s or "unnamed"


# ZIP64, store-only, data-descriptor streaming writer. No seeking, so the whole
# archive can be produced as a forward-only byte stream of unbounded size.
_LFH_SIG = 0x04034b50
_DD_SIG = 0x08074b50
_CDH_SIG = 0x02014b50
_EOCD_SIG = 0x06054b50
_Z64_EOCD_SIG = 0x06064b50
_Z64_LOC_SIG = 0x07064b50
_Z64_EXTRA = 0x0001
_ZIP_FLAGS = 0x0808            # bit3 data-descriptor + bit11 UTF-8 names
_ZIP_VERSION = 45             # 4.5 → ZIP64
_U32 = 0xFFFFFFFF


def _dos_datetime(ts: float) -> tuple[int, int]:
    t = time.localtime(ts)
    if t.tm_year < 1980:
        return 0, 0x21
    dt = ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)) & 0xFFFF
    dd = (((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday) & 0xFFFF
    return dt, dd


async def stream_zip(entries, mtime=None):
    """Yield a ZIP64 archive. `entries` is a list of dicts:
        {"name": <arcname str>, "is_dir": bool, "path": Path|None}
    Directories carry no data. Files are read from `path` in chunks; CRC and
    size are computed on the fly and written in a trailing data descriptor.
    """
    if mtime is None:
        mtime = time.time()
    dt, dd = _dos_datetime(mtime)
    pos = 0
    cds = []
    for e in entries:
        name = e["name"]
        is_dir = e["is_dir"]
        if is_dir and not name.endswith("/"):
            name += "/"
        name_b = name.encode("utf-8")
        offset = pos
        lfh = struct.pack(
            "<IHHHHHIIIHH",
            _LFH_SIG, _ZIP_VERSION, _ZIP_FLAGS, 0, dt, dd, 0, 0, 0, len(name_b), 0,
        ) + name_b
        yield lfh
        pos += len(lfh)
        crc = 0
        size = 0
        if not is_dir:
            async with aiofiles.open(e["path"], "rb") as f:
                while True:
                    buf = await f.read(ZIP_READ_BUF)
                    if not buf:
                        break
                    crc = zlib.crc32(buf, crc)
                    size += len(buf)
                    yield buf
                    pos += len(buf)
        crc &= 0xFFFFFFFF
        dd_rec = struct.pack("<IIQQ", _DD_SIG, crc, size, size)
        yield dd_rec
        pos += len(dd_rec)
        cds.append((name_b, is_dir, crc, size, offset, dt, dd))

    cd_start = pos
    cd = bytearray()
    for name_b, is_dir, crc, size, offset, edt, edd in cds:
        z64 = b""
        if size >= _U32:
            z64 += struct.pack("<Q", size)   # uncompressed
        if size >= _U32:
            z64 += struct.pack("<Q", size)   # compressed (store)
        if offset >= _U32:
            z64 += struct.pack("<Q", offset)
        extra = struct.pack("<HH", _Z64_EXTRA, len(z64)) + z64 if z64 else b""
        size32 = _U32 if size >= _U32 else size
        off32 = _U32 if offset >= _U32 else offset
        ext_attr = (0o040755 << 16) if is_dir else (0o100644 << 16)
        cd += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            _CDH_SIG, (3 << 8) | _ZIP_VERSION, _ZIP_VERSION, _ZIP_FLAGS, 0,
            edt, edd, crc, size32, size32, len(name_b), len(extra), 0, 0, 0,
            ext_attr, off32,
        ) + name_b + extra
    yield bytes(cd)
    pos += len(cd)
    n = len(cds)
    cd_size = len(cd)

    z64_eocd_off = pos
    z64_eocd = struct.pack(
        "<IQHHIIQQQQ",
        _Z64_EOCD_SIG, 44, (3 << 8) | _ZIP_VERSION, _ZIP_VERSION, 0, 0,
        n, n, cd_size, cd_start,
    )
    yield z64_eocd
    pos += len(z64_eocd)
    yield struct.pack("<IIQI", _Z64_LOC_SIG, 0, z64_eocd_off, 1)
    yield struct.pack(
        "<IHHHHIIH",
        _EOCD_SIG, 0, 0, min(n, 0xFFFF), min(n, 0xFFFF),
        _U32 if cd_size >= _U32 else cd_size,
        _U32 if cd_start >= _U32 else cd_start, 0,
    )
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("chunked-upload")

# Per-session asyncio.Lock so only one task finalises a given session.
session_locks: dict[str, asyncio.Lock] = {}
# After finalisation, cached response for any late-arriving stragglers.
session_results: dict[str, tuple[int, str, str | None]] = {}  # session -> (status, body, content_type)


def session_dir(session_id: str) -> Path:
    return CHUNK_DIR / session_id


def parts_received(d: Path) -> list[Path]:
    return sorted(d.glob("*.bin"))


def validate_session(s: str | None) -> bool:
    return bool(s and SESSION_RE.match(s))


async def write_chunk(stream, dest: Path) -> int:
    """Stream the request body into dest, return bytes written.

    Writes to dest.with_suffix('.partial') first, then atomically renames to
    dest. This is essential: parts_received() uses glob('*.bin') and would
    otherwise see still-being-written files as if they were finished,
    triggering a premature finalisation with truncated parts.
    """
    written = 0
    tmp = dest.with_suffix(".partial")
    async with aiofiles.open(tmp, "wb") as f:
        async for blob in stream.iter_chunked(HTTP_READ_BUF):
            written += len(blob)
            if written > MAX_CHUNK_BYTES:
                # Truncate then bail.
                await f.write(blob[: MAX_CHUNK_BYTES - (written - len(blob))])
                # Best effort cleanup of partial.
                try: tmp.unlink()
                except OSError: pass
                raise web.HTTPRequestEntityTooLarge(
                    max_size=MAX_CHUNK_BYTES, actual_size=written
                )
            await f.write(blob)
        # Ensure all bytes hit the disk before the rename so the post-rename
        # file size matches what we just wrote.
        await f.flush()
    # Atomic rename — only now does dest appear to glob('*.bin').
    os.replace(tmp, dest)
    return written


async def stream_concat(files: list[Path]):
    """Async generator yielding bytes from each file in order."""
    for fp in files:
        async with aiofiles.open(fp, "rb") as src:
            while True:
                buf = await src.read(DISK_READ_BUF)
                if not buf:
                    break
                yield buf


async def forward_to_java(headers: dict, files: list[Path]) -> tuple[int, str, str | None]:
    """Concatenate files in order and POST to Java carbonio-files.

    Returns (status, body_text, content_type).
    """
    total_size = sum(fp.stat().st_size for fp in files)
    if total_size > MAX_TOTAL_BYTES:
        return (
            413,
            f'{{"error":"total size {total_size} exceeds limit {MAX_TOTAL_BYTES}"}}',
            "application/json",
        )

    fwd_headers = {
        # Forward only the headers Java carbonio-files needs.
        "Content-Type": headers.get("Content-Type", "application/octet-stream"),
        "Content-Length": str(total_size),
    }
    for key in ("Cookie", "filename", "parentid", "X-ZM-AUTH"):
        if val := headers.get(key):
            if key in ("filename", "parentid"):
                val = sanitize_relay_header(val)
                if not val:
                    continue
            fwd_headers[key] = val

    timeout = ClientTimeout(total=JAVA_TIMEOUT_SECONDS, sock_connect=10)
    async with ClientSession(timeout=timeout) as http:
        async with http.post(
            JAVA_UPLOAD_URL, headers=fwd_headers, data=stream_concat(files)
        ) as resp:
            body = await resp.text()
            return resp.status, body, resp.headers.get("Content-Type")


def cleanup_session(d: Path) -> None:
    """Best-effort delete of the session staging dir."""
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception as exc:
        log.warning("cleanup failed for %s: %s", d, exc)


async def handle_upload_chunked(request: web.Request) -> web.Response:
    qs = request.query
    session_id = qs.get("session")
    if not validate_session(session_id):
        return web.json_response(
            {"error": "bad session id"}, status=400
        )

    try:
        part = int(qs.get("part", ""))
        total = int(qs.get("total", ""))
    except ValueError:
        return web.json_response(
            {"error": "part/total must be integers"}, status=400
        )
    if part < 0 or total < 1 or part >= total:
        return web.json_response(
            {"error": "part must be in [0, total) and total >= 1"}, status=400
        )

    # If session was already finalised — short-circuit (idempotent retries).
    if session_id in session_results:
        status, body, ctype = session_results[session_id]
        return web.Response(status=status, text=body, content_type=ctype)

    d = session_dir(session_id)
    if not d.exists():
        # New session — enforce the global concurrency cap before creating a dir.
        try:
            active = sum(1 for _ in CHUNK_DIR.iterdir()) if CHUNK_DIR.exists() else 0
        except OSError:
            active = 0
        if active >= MAX_ACTIVE_SESSIONS:
            log.warning("rejecting new session %s: %d active sessions (cap %d)",
                        session_id, active, MAX_ACTIVE_SESSIONS)
            return web.json_response(
                {"error": "too many active upload sessions, retry later"},
                status=503,
            )
    d.mkdir(parents=True, exist_ok=True)

    dest = d / f"{part:06d}.bin"
    if dest.exists():
        # Idempotency: same part uploaded twice. Replace with the new body.
        dest.unlink()

    try:
        written = await write_chunk(request.content, dest)
    except web.HTTPException:
        # propagate 413 etc.
        raise
    except Exception as exc:
        log.exception("chunk write failed for %s part=%s: %s", session_id, part, exc)
        return web.json_response(
            {"error": f"chunk write failed: {exc}"}, status=500
        )

    log.info("session=%s part=%s wrote %d bytes", session_id, part, written)

    # Quick check before taking the lock.
    if len(parts_received(d)) < total:
        return web.json_response(
            {
                "status": "received",
                "part": part,
                "bytes": written,
                "received_count": len(parts_received(d)),
                "total": total,
            }
        )

    # All parts present — finalise under per-session lock.
    lock = session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        # Re-check after lock — another task may have finished while we waited.
        if session_id in session_results:
            status, body, ctype = session_results[session_id]
            return web.Response(status=status, text=body, content_type=ctype)

        files = parts_received(d)
        # Defensive: ensure exactly [0..total).
        expected = [d / f"{i:06d}.bin" for i in range(total)]
        if files != expected:
            missing = [i for i in range(total) if not (d / f"{i:06d}.bin").exists()]
            return web.json_response(
                {"error": "missing parts", "missing": missing}, status=400
            )

        log.info(
            "session=%s finalising: %d parts, total=%d bytes",
            session_id,
            total,
            sum(fp.stat().st_size for fp in files),
        )
        try:
            status, body, ctype = await forward_to_java(dict(request.headers), files)
        except Exception as exc:
            log.exception("forward to java failed for %s: %s", session_id, exc)
            return web.json_response(
                {"error": f"forward to java failed: {exc}"}, status=502
            )

        # Cache for stragglers (e.g. retries from clients that didn't see our reply)
        session_results[session_id] = (status, body, ctype)
        cleanup_session(d)
        log.info("session=%s done: java status=%d", session_id, status)
        return web.Response(status=status, text=body, content_type=ctype)


async def handle_status(request: web.Request) -> web.Response:
    session_id = request.query.get("session")
    if not validate_session(session_id):
        return web.json_response({"error": "bad session id"}, status=400)
    d = session_dir(session_id)
    if not d.exists():
        return web.json_response({"received": [], "exists": False})
    files = parts_received(d)
    received_parts = [int(fp.stem) for fp in files]
    return web.json_response(
        {
            "received": received_parts,
            "received_count": len(received_parts),
            "exists": True,
            "finalised": session_id in session_results,
        }
    )


async def handle_abort(request: web.Request) -> web.Response:
    session_id = request.query.get("session")
    if not validate_session(session_id):
        return web.json_response({"error": "bad session id"}, status=400)
    cleanup_session(session_dir(session_id))
    session_results.pop(session_id, None)
    session_locks.pop(session_id, None)
    return web.json_response({"aborted": session_id})


async def get_db_pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(
            database=DB_NAME,
            min_size=1,
            max_size=4,
            # peer auth via unix socket — no host/port/user/password needed
            # when the OS user (zextras) matches the postgres role.
        )
    return _db_pool


def quote_filename_for_header(name: str) -> str:
    """RFC 5987 filename* + ASCII fallback so Content-Disposition is robust."""
    from urllib.parse import quote
    safe_ascii = name.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{quote(name)}'


# Hop-by-hop headers that must not be forwarded across a proxy (RFC 7230 §6.1),
# plus length/encoding which we let the streaming layer recompute.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-connection", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "content-length",
})


async def java_access_validated(request: web.Request, token: str) -> bool:
    """Cheap HEAD probe to Java (via envoy) forwarding the client's cookies, to
    check whether an access-code session is validated. HEAD returns no body, so
    it never streams the (possibly huge) blob. 200 → validated; a 307 to the
    code-entry page / 404 / anything else → not (yet) validated."""
    url = JAVA_PUBLIC_LINK_URL + "/" + token
    if request.query_string:
        url += "?" + request.query_string
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _HOP_BY_HOP and k.lower() not in ("host", "range")}
    timeout = ClientTimeout(total=30, sock_connect=10)
    try:
        async with ClientSession(timeout=timeout, auto_decompress=False) as http:
            async with http.head(url, headers=fwd, allow_redirects=False) as up:
                return up.status == 200
    except Exception as exc:
        log.warning("access-code HEAD probe failed for token=%s: %s", token[:8], exc)
        return False


async def proxy_public_to_java(request: web.Request, token: str) -> web.StreamResponse:
    """Transparently reverse-proxy an access-code public link to the Java
    endpoint via envoy, streaming the response back verbatim (including a 307
    to the code-entry page). Cookies/Range/query are forwarded so the validated
    access-code session is honoured and range/resume keeps working."""
    url = JAVA_PUBLIC_LINK_URL + "/" + token
    if request.query_string:
        url += "?" + request.query_string
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}
    timeout = ClientTimeout(total=JAVA_TIMEOUT_SECONDS, sock_connect=10)
    try:
        async with ClientSession(timeout=timeout, auto_decompress=False) as http:
            async with http.get(url, headers=fwd, allow_redirects=False) as up:
                resp = web.StreamResponse(status=up.status)
                for k, v in up.headers.items():
                    if k.lower() not in _HOP_BY_HOP:
                        resp.headers[k] = v
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
                log.info("public-download access-code token=%s..%s -> java status=%s",
                         token[:8], token[-8:], up.status)
                return resp
    except Exception as exc:
        log.exception("access-code proxy failed for token=%s: %s", token[:8], exc)
        return web.json_response({"error": "upstream error"}, status=502)


async def handle_public_download(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    if not PUBLIC_TOKEN_RE.match(token):
        return web.json_response({"error": "bad token"}, status=400)

    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT l.node_id, l.expire_at, l.access_code,
                       n.name, n.current_version,
                       r.size, r.mime_type
                FROM link l
                JOIN node n ON n.node_id = l.node_id
                JOIN revision r ON r.node_id = n.node_id AND r.version = n.current_version
                WHERE l.public_id = $1
                LIMIT 1
                """,
                token,
            )
    except Exception as exc:
        log.exception("db lookup failed for token=%s: %s", token[:8], exc)
        return web.json_response({"error": "db error"}, status=502)

    if not row:
        return web.json_response({"error": "link not found"}, status=404)
    if row["expire_at"] and row["expire_at"] < int(time.time() * 1000):
        return web.json_response({"error": "link expired"}, status=410)
    if row["access_code"]:
        # Access-code-protected link. Carbonio validates the code on the
        # code-entry page and marks the session with a cookie; the download
        # request itself carries no code, only that cookie. So probe Java with a
        # cheap HEAD (no body) forwarding the client's cookies: 200 means the
        # session is validated → serve the blob via the fast sendfile path below
        # (streaming the bytes through Java would hit its large-blob
        # EOFException/OOM bug — the reason this sidecar exists). Anything else
        # (307 to the code-entry page, 404) → reverse-proxy so the browser gets
        # that response verbatim.
        if not await java_access_validated(request, token):
            return await proxy_public_to_java(request, token)
        # Validated → fall through to the direct sendfile path.

    node_id = row["node_id"]
    version = row["current_version"]
    blob_path = BLOBS_ROOT / node_id[:2] / f"{node_id}-{version}"
    # Defense-in-depth: even though node_id is a server-side UUID, resolve
    # symlinks and confirm the result stays inside BLOBS_ROOT.
    try:
        if not str(blob_path.resolve()).startswith(str(BLOBS_ROOT.resolve()) + os.sep):
            log.warning("blob path escapes BLOBS_ROOT: node=%s path=%s", node_id, blob_path)
            return web.json_response({"error": "bad path"}, status=403)
    except OSError:
        return web.json_response({"error": "bad path"}, status=400)
    if not blob_path.exists():
        log.error("blob missing for node=%s version=%s path=%s", node_id, version, blob_path)
        return web.json_response({"error": "blob missing"}, status=404)

    mime = row["mime_type"] or "application/octet-stream"
    name = row["name"] or "download"
    response = web.FileResponse(
        path=blob_path,
        headers={
            "Content-Type": mime,
            "Content-Disposition": quote_filename_for_header(name),
            # Cache control: short, since the link may be revoked. The blob is
            # immutable per (node_id, version) so etag/last-modified are fine.
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )
    log.info("public-download token=%s..%s node=%s size=%s",
             token[:8], token[-8:], node_id, row["size"])
    return response


async def handle_ui_download_check(request: web.Request) -> web.Response:
    """Carbonio's Files UI calls /download/<UUID>/check before the actual
    download to validate access and pre-load metadata. The /check call returns
    a tiny JSON payload (no blob bytes), so we just forward it to Java which
    handles it without the OkHttp/storages EOF risk.
    """
    node_id = request.match_info.get("node_id", "")
    if not NODE_UUID_RE.match(node_id):
        return web.json_response({"error": "bad node id"}, status=400)
    cookie = request.headers.get("Cookie", "")
    if "ZM_AUTH_TOKEN=" not in cookie:
        return web.Response(status=307, headers={"Location": "/static/login/"})
    try:
        async with ClientSession(timeout=ClientTimeout(total=10, sock_connect=5)) as http:
            async with http.get(
                f"http://127.78.0.2:10000/download/{node_id}/check",
                headers={"Cookie": cookie},
            ) as resp:
                body = await resp.read()
                return web.Response(
                    status=resp.status,
                    body=body,
                    content_type=resp.headers.get("Content-Type"),
                )
    except Exception as exc:
        log.exception("ui-download check forward failed: %s", exc)
        return web.json_response({"error": "check forward error"}, status=502)


async def handle_ui_download(request: web.Request) -> web.StreamResponse:
    """Authenticated UI download bypass for large files. Java's BlobController
    streams blobs via OkHttp from storages and EOFs on files > ~3 GB. We
    validate the session cookie against Java's GraphQL `getNode` query (which
    doesn't touch the blob), then serve the blob from disk with sendfile —
    same approach as public-link, but with auth via Carbonio cookie.
    """
    node_id = request.match_info.get("node_id", "")
    if not NODE_UUID_RE.match(node_id):
        return web.json_response({"error": "bad node id"}, status=400)

    cookie = request.headers.get("Cookie", "")
    if "ZM_AUTH_TOKEN=" not in cookie:
        return web.Response(status=307, headers={"Location": "/static/login/"})

    # 1. Auth + access check via Java's GraphQL. getNode returns the node only
    #    if the calling user has access (via ownership, share, or shared
    #    parent). No blob bytes are fetched here.
    gql_body = (
        '{"query":"query GN($id:ID!){getNode(node_id:$id){... on File{name '
        'extension mime_type}... on Folder{name}}}","variables":{"id":"'
        + node_id + '"}}'
    )
    timeout = ClientTimeout(total=10, sock_connect=5)
    try:
        async with ClientSession(timeout=timeout) as http:
            async with http.post(
                "http://127.78.0.2:10000/graphql",
                headers={"Cookie": cookie, "Content-Type": "application/json"},
                data=gql_body,
            ) as resp:
                if resp.status == 401:
                    return web.Response(status=307, headers={"Location": "/static/login/"})
                if resp.status != 200:
                    log.warning("ui-download auth check returned %s for node=%s",
                                resp.status, node_id)
                    return web.json_response({"error": "auth check failed"}, status=resp.status)
                gql = await resp.json()
                node = (gql.get("data") or {}).get("getNode")
                if not node:
                    return web.json_response({"error": "node not found or no access"}, status=404)
                name = node.get("name") or "download"
                # Carbonio stores File.name WITHOUT the extension; extension
                # is a separate field. Re-attach so the browser saves
                # `filename.iso` not `filename`.
                ext = node.get("extension") or ""
                if ext and not name.lower().endswith("." + ext.lower()):
                    name = f"{name}.{ext}"
                mime_from_node = node.get("mime_type")  # only present on File
    except Exception as exc:
        log.exception("ui-download auth check exception for node=%s: %s", node_id, exc)
        return web.json_response({"error": "auth check error"}, status=502)

    # 2. Lookup blob location from DB.
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT n.current_version, r.mime_type, r.size "
                "FROM node n JOIN revision r ON r.node_id = n.node_id "
                "AND r.version = n.current_version WHERE n.node_id = $1",
                node_id,
            )
    except Exception as exc:
        log.exception("ui-download db lookup failed for node=%s: %s", node_id, exc)
        return web.json_response({"error": "db error"}, status=502)

    if not row:
        return web.json_response({"error": "blob metadata missing"}, status=404)

    version = row["current_version"]
    blob_path = BLOBS_ROOT / node_id[:2] / f"{node_id}-{version}"
    # Defense-in-depth: keep blob path inside BLOBS_ROOT (same as public-link).
    try:
        if not str(blob_path.resolve()).startswith(str(BLOBS_ROOT.resolve()) + os.sep):
            log.warning("ui-download path escapes BLOBS_ROOT: node=%s path=%s", node_id, blob_path)
            return web.json_response({"error": "bad path"}, status=403)
    except OSError:
        return web.json_response({"error": "bad path"}, status=400)
    if not blob_path.exists():
        log.error("ui-download blob missing: node=%s version=%s path=%s", node_id, version, blob_path)
        return web.json_response({"error": "blob missing"}, status=404)

    mime = mime_from_node or row["mime_type"] or "application/octet-stream"
    log.info("ui-download node=%s size=%s name=%s", node_id, row["size"], name)
    return web.FileResponse(
        path=blob_path,
        headers={
            "Content-Type": mime,
            "Content-Disposition": quote_filename_for_header(name),
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


def _build_zip_entries(shared_folder, by_id, children, selected_ids):
    """DFS from each selected node, producing zip entries with paths rooted at
    the selected node's own name. Every node is guaranteed (by the caller) to
    live inside the shared subtree, so paths never escape it."""
    entries = []

    def walk(node_id, prefix):
        node = by_id[node_id]
        comp = zip_component(node["name"])
        path = f"{prefix}{comp}" if prefix else comp
        if node["node_type"] == "FOLDER" or node["node_type"] == "ROOT":
            entries.append({"name": path, "is_dir": True, "path": None})
            for child in children.get(node_id, ()):
                if len(entries) >= MAX_ZIP_ENTRIES:
                    return
                walk(child["node_id"], path + "/")
        else:
            version = node["current_version"]
            if version is None:
                return
            blob = BLOBS_ROOT / node_id[:2] / f"{node_id}-{version}"
            try:
                if not str(blob.resolve()).startswith(str(BLOBS_ROOT.resolve()) + os.sep):
                    return
            except OSError:
                return
            if not blob.exists():
                log.warning("zip: blob missing node=%s ver=%s", node_id, version)
                return
            entries.append({"name": path, "is_dir": False, "path": blob})

    for nid in selected_ids:
        if len(entries) >= MAX_ZIP_ENTRIES:
            break
        walk(nid, "")
    return entries


async def handle_public_download_multiple(request: web.Request) -> web.StreamResponse:
    """Rebuild the folder ZIP that Java can't stream for large blobs.

    The public-folder UI submits a hidden form (urlencoded) with nodeIds (a JSON
    array), nodeLinkId and optional accessCode, targeting a sandboxed iframe, so
    the response must carry Content-Disposition: attachment. We authorise
    against the DB (link validity, access code, subtree membership) and stream a
    ZIP assembled from blobs on disk.
    """
    try:
        form = await request.post()
    except Exception:
        return web.json_response({"error": "bad form"}, status=400)
    node_link_id = (form.get("nodeLinkId") or "").strip()
    access_code = form.get("accessCode")
    raw_ids = form.get("nodeIds") or "[]"
    if not PUBLIC_LINK_ID_RE.match(node_link_id):
        return web.json_response({"error": "bad link id"}, status=400)
    try:
        node_ids = json.loads(raw_ids)
        if not isinstance(node_ids, list):
            raise ValueError
    except Exception:
        return web.json_response({"error": "bad nodeIds"}, status=400)
    # De-dup, validate shape, cap count.
    seen = set()
    node_ids = [x for x in node_ids
                if isinstance(x, str) and NODE_UUID_RE.match(x)
                and not (x in seen or seen.add(x))]
    if not node_ids:
        return web.json_response({"error": "no valid nodeIds"}, status=400)
    if len(node_ids) > MAX_ZIP_SELECTED:
        return web.json_response({"error": "too many nodes"}, status=413)

    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            link = await conn.fetchrow(
                "SELECT node_id, access_code, expire_at FROM link WHERE public_id = $1",
                node_link_id,
            )
            if not link:
                return web.json_response({"error": "link not found"}, status=404)
            if link["expire_at"] and link["expire_at"] < int(time.time() * 1000):
                return web.json_response({"error": "link expired"}, status=410)
            # Access code is stored plaintext; compare in constant time.
            stored_code = link["access_code"]
            if stored_code:
                if not access_code or not hmac.compare_digest(str(access_code), str(stored_code)):
                    return web.json_response({"error": "wrong access code"}, status=403)
            shared_folder = link["node_id"]

            # Fetch the whole shared subtree once (folder itself + descendants).
            rows = await conn.fetch(
                """
                SELECT node_id, name, node_type, folder_id, current_version, ancestor_ids
                FROM node
                WHERE node_id = $1
                   OR $1 = ANY(string_to_array(ancestor_ids, ','))
                """,
                shared_folder,
            )
    except Exception as exc:
        log.exception("zip: db lookup failed link=%s: %s", node_link_id[:8], exc)
        return web.json_response({"error": "db error"}, status=502)

    by_id = {r["node_id"]: dict(r) for r in rows}
    # Subtree membership: every requested node must be inside the shared folder.
    for nid in node_ids:
        if nid not in by_id:
            log.warning("zip: node %s not in shared subtree of link=%s",
                        nid, node_link_id[:8])
            return web.json_response({"error": "node not in share"}, status=403)

    children: dict[str, list] = {}
    for r in by_id.values():
        children.setdefault(r["folder_id"], []).append(r)
    for lst in children.values():
        lst.sort(key=lambda x: (x["node_type"] != "FOLDER", (x["name"] or "").lower()))

    entries = _build_zip_entries(shared_folder, by_id, children, node_ids)
    file_count = sum(1 for e in entries if not e["is_dir"])
    if file_count == 0:
        return web.json_response({"error": "no downloadable files"}, status=404)

    # ZIP name: the selected folder if exactly one, else the shared folder.
    if len(node_ids) == 1:
        zip_name = zip_component(by_id[node_ids[0]]["name"])
    else:
        zip_name = zip_component((by_id.get(shared_folder) or {}).get("name") or "download")

    resp = web.StreamResponse(status=200)
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = quote_filename_for_header(zip_name + ".zip")
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    await resp.prepare(request)
    log.info("zip: link=%s.. selected=%d files=%d entries=%d name=%s",
             node_link_id[:8], len(node_ids), file_count, len(entries), zip_name)
    try:
        async for chunk in stream_zip(entries):
            await resp.write(chunk)
        await resp.write_eof()
    except (ConnectionError, asyncio.CancelledError) as exc:
        # ConnectionError covers both ConnectionResetError and aiohttp's
        # "Connection lost" — a normal client/proxy disconnect, not an error.
        log.info("zip: client disconnected link=%s.. (%s)", node_link_id[:8], exc)
    except Exception as exc:
        # Headers already sent — can't switch to an error status; just log and
        # drop the connection so the client sees a truncated (invalid) archive.
        log.exception("zip: stream failed link=%s..: %s", node_link_id[:8], exc)
    return resp


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _reap_once() -> None:
    """Drop in-memory entries for sessions older than SESSION_TTL_SECONDS, sweep
    orphan chunk dirs on disk, and delete orphan DB nodes left by interrupted
    direct (non-chunked) uploads. Errors during sweeping are logged but never
    propagate.
    """
    threshold = time.time() - SESSION_TTL_SECONDS
    orphan_dir_threshold = time.time() - ORPHAN_DIR_TTL_SECONDS
    # 1. In-memory bookkeeping: drop entries whose staging dir is gone or stale.
    for sid in list(session_results.keys()):
        d = session_dir(sid)
        stale = not d.exists()
        if not stale:
            try:
                stale = d.stat().st_mtime < threshold
            except OSError:
                stale = True
        if stale:
            session_results.pop(sid, None)
            session_locks.pop(sid, None)
            cleanup_session(d)
            log.info("reaper: removed stale session %s", sid)
    # 2. Orphan dirs on disk without a matching in-memory entry.
    if CHUNK_DIR.exists():
        for d in CHUNK_DIR.iterdir():
            if not d.is_dir():
                continue
            sid = d.name
            if sid in session_locks or sid in session_results:
                continue
            try:
                if d.stat().st_mtime < orphan_dir_threshold:
                    cleanup_session(d)
                    log.info("reaper: removed orphan dir %s", sid)
            except OSError:
                pass
    # 3. Orphan DB nodes: Java creates a node row before writing the revision/blob.
    #    If a direct upload (<200 MB, bypasses this sidecar) is interrupted, the node
    #    stays with size=0 and no revision row, causing "version not found" in the UI.
    #    Delete such nodes after ORPHAN_DB_TTL_SECONDS (1 hour).
    try:
        pool = await get_db_pool()
        orphan_threshold_ms = int((time.time() - ORPHAN_DB_TTL_SECONDS) * 1000)
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM node
                WHERE current_version IS NOT NULL
                  AND size = 0
                  AND updated_timestamp < $1
                  AND node_id NOT IN (SELECT DISTINCT node_id FROM revision)
                """,
                orphan_threshold_ms,
            )
        if result != "DELETE 0":
            log.info("reaper: cleaned orphan DB nodes: %s", result)
    except Exception as exc:
        log.warning("reaper: orphan DB cleanup failed: %s", exc)


async def _reaper_loop(app: web.Application) -> None:
    while True:
        try:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            await _reap_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("reaper error: %s", exc)


async def _start_reaper(app: web.Application) -> None:
    app["reaper_task"] = asyncio.create_task(_reaper_loop(app))


async def _stop_reaper(app: web.Application) -> None:
    task = app.get("reaper_task")
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def build_app() -> web.Application:
    app = web.Application(
        client_max_size=MAX_CHUNK_BYTES + 16 * 1024,  # body + headers headroom
    )
    app.router.add_post("/upload-chunked", handle_upload_chunked)
    app.router.add_get("/upload-chunked/status", handle_status)
    app.router.add_post("/upload-chunked/abort", handle_abort)
    app.router.add_get("/public-download/{token}", handle_public_download)
    app.router.add_post("/public-download-multiple", handle_public_download_multiple)
    app.router.add_get("/ui-download/{node_id}/check", handle_ui_download_check)
    app.router.add_get("/ui-download/{node_id}", handle_ui_download)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(_start_reaper)
    app.on_cleanup.append(_stop_reaper)
    return app


if __name__ == "__main__":
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    web.run_app(build_app(), host=LISTEN_HOST, port=LISTEN_PORT, access_log=log)
