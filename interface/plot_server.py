# =============================================================================
# PLOT SERVER - Serves HTML charts over HTTP
# =============================================================================
# Runs a simple HTTP server on PLOT_SERVER_PORT (default 8181) serving the
# www/ directory. Charts are saved there with fixed filenames and accessed
# via a Tailscale URL that works from anywhere.
#
# Tailscale IP is resolved once at startup. Falls back to local IP if
# Tailscale is not running.
# =============================================================================

import http.server
import logging
import os
import socket
import subprocess
import threading

import config

logger = logging.getLogger(__name__)

PORT     = getattr(config, "PLOT_SERVER_PORT", 8181)
WWW_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "www")

# Resolved once at startup
_base_url: str | None = None


def _get_tailscale_ip() -> str | None:
    """Return the Tailscale IPv4 address, or None if not available."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        if ip:
            return ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _get_local_ip() -> str:
    """Return the local network IP as a fallback."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


def resolve_base_url() -> str:
    """Resolve and cache the base URL for the plot server."""
    global _base_url
    if _base_url is not None:
        return _base_url

    ts_ip = _get_tailscale_ip()
    if ts_ip:
        _base_url = f"http://{ts_ip}:{PORT}"
        logger.info(f"📡 Plot server URL (Tailscale): {_base_url}")
    else:
        local_ip = _get_local_ip()
        _base_url = f"http://{local_ip}:{PORT}"
        logger.warning(
            f"⚠️  Tailscale not running — plot server on local network only: {_base_url}"
        )

    return _base_url


def get_plot_url(filename: str) -> str:
    """Return the full URL for a plot file."""
    return f"{resolve_base_url()}/{filename}"


def start() -> None:
    """Start the HTTP server in a background daemon thread."""
    os.makedirs(WWW_DIR, exist_ok=True)
    resolve_base_url()

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=WWW_DIR, **kwargs)

        def log_message(self, format, *args):
            logger.debug(f"[plot_server] {format % args}")

    def _serve():
        with http.server.HTTPServer(("", PORT), _Handler) as httpd:
            logger.info(f"📊 Plot server started on port {PORT} → serving {WWW_DIR}")
            httpd.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="plot_server")
    t.start()