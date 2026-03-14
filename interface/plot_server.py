# =============================================================================
# PLOT SERVER - Serves HTML charts over HTTP with Basic Auth
# =============================================================================
# Runs a lightweight HTTP server on PLOT_SERVER_PORT (default 8181) serving
# the www/ directory. Protected by HTTP Basic Auth.
#
# Access from anywhere via DuckDNS hostname + port forwarding on your router.
# Access on local network via Pi's local IP directly.
#
# Config (in .env / config.py):
#   PLOT_SERVER_PORT     = 8181
#   PLOT_SERVER_USER     = admin
#   PLOT_SERVER_PASSWORD = yourpassword
#   PLOT_SERVER_HOST     = yourhostname.duckdns.org   (or public IP)
# =============================================================================

import base64
import http.server
import logging
import os
import socket
import threading

import config

logger = logging.getLogger(__name__)

PORT     = getattr(config, "PLOT_SERVER_PORT",     8181)
USER     = getattr(config, "PLOT_SERVER_USER",     "admin")
PASSWORD = getattr(config, "PLOT_SERVER_PASSWORD", "")
HOST     = getattr(config, "PLOT_SERVER_HOST",     "")   # DuckDNS hostname or public IP

WWW_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "www")

# Resolved once at startup
_base_url: str | None = None


def _get_local_ip() -> str:
    """Return the local network IP."""
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

    if HOST:
        _base_url = f"http://{HOST}:{PORT}"
        logger.info(f"📊 Plot server public URL: {_base_url}")
    else:
        local_ip = _get_local_ip()
        _base_url = f"http://{local_ip}:{PORT}"
        logger.warning(
            f"⚠️  PLOT_SERVER_HOST not set — plot links only work on local network: {_base_url}"
        )

    return _base_url


def get_plot_url(filename: str) -> str:
    """Return the full URL for a plot file."""
    return f"{resolve_base_url()}/{filename}"


def start() -> None:
    """Start the HTTP server in a background daemon thread."""
    os.makedirs(WWW_DIR, exist_ok=True)
    resolve_base_url()

    _expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=WWW_DIR, **kwargs)

        def do_GET(self):
            # Check Basic Auth if password is configured
            if PASSWORD:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    self._require_auth()
                    return
                provided = auth[len("Basic "):].strip()
                if provided != _expected:
                    self._require_auth()
                    return
            super().do_GET()

        def _require_auth(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="TroubleHouseBot"')
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Unauthorized")

        def log_message(self, format, *args):
            logger.debug(f"[plot_server] {format % args}")

    def _serve():
        with http.server.HTTPServer(("", PORT), _Handler) as httpd:
            logger.info(f"📊 Plot server started on port {PORT} → serving {WWW_DIR}")
            httpd.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="plot_server")
    t.start()