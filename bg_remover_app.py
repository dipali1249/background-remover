#!/usr/bin/env python3
"""
bg_remover_app.py — a local, drag-and-drop background remover.

    pip install "rembg[cpu]" pillow
    python bg_remover_app.py

Then open http://127.0.0.1:8000 (it usually opens by itself).
Everything runs on this machine: images are posted to a localhost server, processed
with the segmentation engine in bg_remove.py, and returned to the browser.

Options:
    --port 8000      port to listen on
    --host 127.0.0.1 bind address (use 0.0.0.0 to allow other devices on your LAN)
    --no-browser     don't open a browser window
    --max-mb 40      reject uploads larger than this many megabytes
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from bg_remove import DEFAULT_MODEL, MODELS, _parse_color, remove_background

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "ui.html"

MAX_BYTES = 40 * 1024 * 1024
ENGINE_ERROR: str | None = None


def engine_ready() -> tuple[bool, str | None]:
    global ENGINE_ERROR
    try:
        import rembg  # noqa: F401
        return True, None
    except Exception as exc:  # pragma: no cover
        ENGINE_ERROR = f"rembg not installed ({exc.__class__.__name__}). Run: pip install \"rembg[cpu]\""
        return False, ENGINE_ERROR


def _float(params: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(params.get(key, [default])[0])))
    except (TypeError, ValueError):
        return default


def _int(params: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(params.get(key, [default])[0]))))
    except (TypeError, ValueError):
        return default


def render(image_bytes: bytes, params: dict) -> tuple[bytes, str]:
    """Process one image. Returns (bytes, content_type)."""
    model = params.get("model", [DEFAULT_MODEL])[0]
    if model not in MODELS:
        model = DEFAULT_MODEL
    bg = params.get("bg", ["transparent"])[0]
    if bg not in {"transparent", "color", "blur"}:
        bg = "transparent"
    color = params.get("color", ["#ffffff"])[0]
    try:
        _parse_color(color)
    except ValueError:
        color = "#ffffff"

    fmt = params.get("format", ["png"])[0].lower()
    if fmt not in {"png", "jpg", "webp"}:
        fmt = "png"

    with Image.open(io.BytesIO(image_bytes)) as im:
        im.load()
        out = remove_background(
            im,
            model=model,
            background=bg,
            bg_color=color,
            feather=_float(params, "feather", 1.0, 0, 12),
            grow=_int(params, "grow", 0, -10, 10),
            crop=params.get("crop", ["0"])[0] in {"1", "true", "on"},
            max_size=_int(params, "max_size", 4000, 400, 8000),
        )

    buf = io.BytesIO()
    if fmt == "jpg":
        if out.mode == "RGBA":
            flat = Image.new("RGB", out.size, (255, 255, 255))
            flat.paste(out, (0, 0), out)
            out = flat
        out.convert("RGB").save(buf, "JPEG", quality=95, optimize=True, progressive=True)
        ctype = "image/jpeg"
    elif fmt == "webp":
        try:
            out.save(buf, "WEBP", quality=95, method=4)
            ctype = "image/webp"
        except (KeyError, OSError):
            # No WebP encoder in this Pillow build — send a PNG instead.
            buf = io.BytesIO()
            out.save(buf, "PNG", optimize=True)
            ctype = "image/png"
    else:
        out.save(buf, "PNG", optimize=True)
        ctype = "image/png"
    return buf.getvalue(), ctype


class Handler(BaseHTTPRequestHandler):
    server_version = "BackgroundRemover/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        try:
            msg = fmt % args if args else str(fmt)
        except Exception:
            msg = " ".join(str(a) for a in (fmt, *args))
        sys.stderr.write("  %s %s\n" % (self.address_string(), msg))

    # -- helpers ---------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _text(self, code: int, msg: str):
        self._send(code, msg.encode(), "text/plain; charset=utf-8")

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            try:
                html = UI_FILE.read_bytes()
            except OSError:
                return self._text(500, f"ui.html is missing next to this script ({UI_FILE})")
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/health":
            ok, err = engine_ready()
            return self._json(200, {
                "ok": ok,
                "error": err,
                "models": sorted(MODELS),
                "default": DEFAULT_MODEL,
            })
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._text(404, "not found")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/remove":
            return self._text(404, "not found")

        ok, err = engine_ready()
        if not ok:
            return self._text(503, err or "engine unavailable")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._text(400, "bad Content-Length")
        if length <= 0:
            return self._text(400, "empty request body")
        if length > MAX_BYTES:
            return self._text(413, f"image too large (limit {MAX_BYTES // (1024*1024)} MB)")

        body = b""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        if not body:
            return self._text(400, "empty request body")

        params = parse_qs(parsed.query)
        try:
            out_bytes, ctype = render(body, params)
        except MemoryError:
            self.log_message("render failed: out of memory")
            return self._text(
                507,
                "Out of memory while processing this image. "
                "Try the u2netp model, or a smaller image.",
            )
        except Exception as exc:  # report the reason to the UI instead of dying
            self.log_message("render failed: %s", exc)
            return self._text(500, f"{exc.__class__.__name__}: {exc}")

        self._send(200, out_bytes, ctype)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local background remover web app")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--max-mb", type=int, default=40)
    args = ap.parse_args(argv)

    global MAX_BYTES
    MAX_BYTES = max(1, args.max_mb) * 1024 * 1024

    ok, err = engine_ready()
    if not ok:
        print(f"! {err}", file=sys.stderr)
        print("  The page will load, but background removal needs the engine installed.",
              file=sys.stderr)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{'127.0.0.1' if args.host in {'0.0.0.0', ''} else args.host}:{args.port}/"

    print(f"Background Remover running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
