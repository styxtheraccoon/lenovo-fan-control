"""
HTTP API Server for HomeAssistant / Homepage integration.
Runs in a background thread, provides fan status and control endpoints.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

log = logging.getLogger("fan-control.api")


class APIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for fan control API."""

    # Reference to the FanControlService instance (set by APIServer)
    service = None

    def log_message(self, format, *args):
        """Route HTTP logs through our logger."""
        log.debug(format, *args)

    def _check_auth(self):
        """Validate API key if configured. Returns True if authorised."""
        api_key = self.service.config.api_key
        if not api_key:
            return True

        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {api_key}":
            return True

        # Also accept X-API-Key header
        x_api_key = self.headers.get("X-API-Key", "")
        if x_api_key == api_key:
            return True

        log.warning("Auth failed from %s for %s %s",
                    self.client_address[0], self.command, self.path)
        self._send_json(401, {"error": "unauthorised"})
        return False

    def _send_json(self, status_code, data):
        """Send a JSON response."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path

        if not self._check_auth():
            return

        if path == "/api/status":
            self._handle_status()
        elif path == "/api/health":
            self._handle_health()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if not self._check_auth():
            return

        if path == "/api/override":
            self._handle_override()
        elif path == "/api/auto":
            self._handle_auto()
        elif path == "/api/reset":
            self._handle_reset()
        else:
            self._send_json(404, {"error": "not found"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, X-API-Key, Content-Type")
        self.end_headers()

    # --- Endpoint handlers ---

    def _handle_status(self):
        """GET /api/status - Current system state."""
        svc = self.service

        # Query RP2040 for live status
        ok, resp = svc.serial.send_command("GET_STATUS")
        rp2040_status = resp.get("payload", {}) if ok else {"error": "offline"}

        # Build temps dict — all configured sensors
        all_temps = svc.temp_reader.last_temps
        # Sanitise: replace None with "unavailable" for HA compatibility
        temps = {
            name: (val if val is not None else "unavailable")
            for name, val in all_temps.items()
        }

        # cpu_temp as top-level for backward compat (HA templates reference it)
        cpu_temp = all_temps.get("cpu")
        if cpu_temp is None:
            cpu_temp = "unavailable"

        # Overall mode at root level (auto/override/failsafe/boot)
        mode = rp2040_status.get("mode", "unknown")

        status = {
            "cpu_temp": cpu_temp,
            "mode": mode,
            "temps": temps,
            "controller": rp2040_status,
            "serial": {
                "connected": svc.serial.is_connected,
                "port": svc.serial.active_port or svc.config.serial_port,
                "last_error": svc.serial.last_error,
            },
            "service": {
                "uptime_s": round(svc.uptime, 1),
                "poll_interval": svc.config.poll_interval,
                "loops": svc.loop_count,
            },
        }
        self._send_json(200, status)

    def _handle_health(self):
        """GET /api/health - Simple health check."""
        svc = self.service
        last_temp = svc.temp_reader.last_temp
        healthy = svc.serial.is_connected and last_temp is not None
        self._send_json(
            200 if healthy else 503,
            {
                "status": "healthy" if healthy else "degraded",
                "serial_connected": svc.serial.is_connected,
                "last_temp": last_temp if last_temp is not None else "unavailable",
                "uptime_s": round(svc.uptime, 1),
            }
        )

    def _handle_override(self):
        """POST /api/override - Set manual fan speed.

        Body: {"percent": 75}
              {"percent": 75, "channel": 0}    — single channel
              {"percent": 75, "channel": "all"} — explicit all
        """
        try:
            body = self._read_body()
        except Exception:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        percent = body.get("percent")
        if percent is None or not isinstance(percent, (int, float)):
            self._send_json(400, {"error": "missing or invalid 'percent' (0-100)"})
            return

        percent = max(0, min(100, float(percent)))
        payload = {"percent": percent}

        channel = body.get("channel")
        if channel is not None:
            if channel != "all" and not isinstance(channel, int):
                self._send_json(400, {
                    "error": "invalid 'channel' — must be int (0-3) or 'all'"
                })
                return
            payload["channel"] = channel

        ok, resp = self.service.serial.send_command("SET_OVERRIDE", payload)

        if ok:
            # Persist override state for recovery after restarts
            self.service.override_tracker.set_override(channel, percent)
            self._send_json(200, {
                "status": "ok",
                "override_percent": percent,
                "channel": channel if channel is not None else "all",
                "controller": resp.get("payload", {}),
            })
        else:
            self._send_json(502, {"error": "controller offline", "detail": resp})

    def _handle_auto(self):
        """POST /api/auto - Return to automatic fan curve.

        Body (optional): {"channel": 0}    — single channel
                         {"channel": "all"} — explicit all
                         {}                 — all (default)
        """
        try:
            body = self._read_body()
        except Exception:
            body = {}

        payload = {}
        channel = body.get("channel")
        if channel is not None:
            if channel != "all" and not isinstance(channel, int):
                self._send_json(400, {
                    "error": "invalid 'channel' — must be int (0-3) or 'all'"
                })
                return
            payload["channel"] = channel

        ok, resp = self.service.serial.send_command(
            "SET_AUTO", payload if payload else None
        )
        if ok:
            # Clear override state (or just this channel)
            self.service.override_tracker.set_auto(channel)
            self._send_json(200, {
                "status": "ok",
                "channel": channel if channel is not None else "all",
                "controller": resp.get("payload", {}),
            })
        else:
            self._send_json(502, {"error": "controller offline", "detail": resp})

    def _handle_reset(self):
        """POST /api/reset - Soft-reset the RP2040 microcontroller.

        Sends a RESET command over serial. The RP2040 ACKs then reboots,
        dropping the USB serial connection. The host service will auto-
        reconnect on the next poll loop iteration.
        """
        svc = self.service

        ok, resp = svc.serial.send_command("RESET")

        if ok:
            # RP2040 is about to reboot — close our end of the serial port
            # so the main loop can cleanly reconnect after the device
            # re-enumerates on USB (~2-3s).
            svc.serial.close_port()
            self._send_json(200, {
                "status": "ok",
                "message": "RP2040 resetting, will reconnect automatically",
                "controller": resp.get("payload", {}),
            })
        else:
            self._send_json(502, {"error": "controller offline", "detail": resp})

class APIServer:
    """Threaded HTTP API server."""

    def __init__(self, service):
        self._service = service
        self._server = None
        self._thread = None

        # Inject service reference into handler class
        APIHandler.service = service

    def start(self):
        """Start the API server in a background daemon thread."""
        host = self._service.config.api_host
        port = self._service.config.api_port

        self._server = HTTPServer((host, port), APIHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="api-server",
            daemon=True,
        )
        self._thread.start()
        log.info("API server listening on %s:%d", host, port)

    def stop(self):
        """Shutdown the API server."""
        if self._server:
            self._server.shutdown()
            log.info("API server stopped")
