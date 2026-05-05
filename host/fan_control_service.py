"""
Lenovo P330 Tiny - Fan Control Host Service
Main daemon: reads CPU temperatures, communicates with RP2040 over serial,
and exposes an HTTP API for HomeAssistant / Homepage integration.

Classes:
    Config          - Service configuration from env/JSON
    TempReader      - Parse CPU temperature from `sensors`
    SerialProtocol  - SYN/ACK/SYN-ACK serial communication
    FanControlService - Main daemon orchestrator
"""

import os
import sys
import json
import time
import signal
import logging
import subprocess
import threading
import fnmatch
import serial
import serial.tools.list_ports

log = logging.getLogger("fan-control")


# ============================================================================
# Configuration
# ============================================================================

class Config:
    """
    Service configuration loaded from environment variables
    or /etc/fan-control/config.json (env vars take precedence).
    """

    DEFAULTS = {
        "serial_port": "auto",       # "auto" = discover by VID/PID, or /dev/ttyACM0 etc.
        "serial_vid": 0x2E8A,       # Raspberry Pi Foundation USB Vendor ID
        "serial_pid": 0x0005,       # RP2040 MicroPython USB Product ID
        "serial_baud": 115200,
        "poll_interval": 5,         # seconds between temperature reads
        "api_port": 9780,
        "api_host": "0.0.0.0",
        "api_key": "",              # empty = no auth
        "log_level": "INFO",
        "serial_timeout": 2,        # seconds per SYN→ACK wait
        "serial_retries": 3,        # retry count on failed send
        "reconnect_interval": 5,    # seconds between reconnection attempts
        "temp_sensors": {           # sensor mappings: "auto" or {"chip": ..., "sensor": ...}
            "cpu": "auto",          # auto-detect CPU package temp (default)
        },
        "config_file": "/etc/fan-control/config.json",
        "state_file": "/var/lib/fan-control/state.json",
    }

    def __init__(self):
        self._cfg = dict(self.DEFAULTS)

        # Load JSON config file if it exists
        config_path = os.environ.get("FAN_CONTROL_CONFIG", self._cfg["config_file"])
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r") as f:
                    file_cfg = json.load(f)
                self._cfg.update(file_cfg)
                log.info("Loaded config from %s", config_path)
            except Exception as e:
                log.warning("Failed to load config file %s: %s", config_path, e)

        # Environment variable overrides (FAN_CONTROL_ prefix)
        env_map = {
            "FAN_CONTROL_SERIAL_PORT": ("serial_port", str),
            "FAN_CONTROL_SERIAL_BAUD": ("serial_baud", int),
            "FAN_CONTROL_POLL_INTERVAL": ("poll_interval", int),
            "FAN_CONTROL_API_PORT": ("api_port", int),
            "FAN_CONTROL_API_HOST": ("api_host", str),
            "FAN_CONTROL_API_KEY": ("api_key", str),
            "FAN_CONTROL_LOG_LEVEL": ("log_level", str),
        }

        for env_var, (key, cast) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                self._cfg[key] = cast(val)

    def __getattr__(self, name):
        if name.startswith("_"):
            return super().__getattribute__(name)
        if name in self._cfg:
            return self._cfg[name]
        raise AttributeError(f"Config has no attribute '{name}'")

    def to_dict(self):
        """Return config as dict (with api_key redacted)."""
        d = dict(self._cfg)
        if d.get("api_key"):
            d["api_key"] = "***"
        return d


# ============================================================================
# Override State Persistence
# ============================================================================

class OverrideTracker:
    """
    Tracks fan override state and persists to disk.
    Survives service and system restarts so manual fan speeds are
    re-applied after the RP2040 reconnects.

    State format (JSON):
        {"all": 75.0}                       — all channels at 75%
        {"0": 50.0, "2": 80.0}              — per-channel overrides
        {"all": 75.0, "0": 50.0}            — all at 75%, channel 0 at 50%
        {}                                  — all channels in auto mode
    """

    def __init__(self, state_file):
        self._state_file = state_file
        self._overrides = {}  # {"all": percent} or {channel_int: percent}
        self._load()

    def set_override(self, channel, percent):
        """Record an override. channel=None or "all" means all channels."""
        percent = float(percent)
        if channel is None or channel == "all":
            # All-channel override replaces any per-channel entries
            self._overrides = {"all": percent}
        else:
            self._overrides[int(channel)] = percent
        self._save()

    def set_auto(self, channel=None):
        """Record return to auto. channel=None or "all" means all channels."""
        if channel is None or channel == "all":
            self._overrides = {}
        else:
            self._overrides.pop(int(channel), None)
            # If the only remaining key is "all" but this channel was
            # explicitly set back to auto, leave "all" — the firmware
            # will apply auto to just this channel via SET_AUTO.
        self._save()

    def get_restore_commands(self):
        """
        Return list of SET_OVERRIDE payloads to replay on reconnect.
        "all" entries come first, then per-channel overrides on top.
        """
        commands = []
        all_pct = self._overrides.get("all")
        if all_pct is not None:
            commands.append({"percent": all_pct})
        for key, pct in self._overrides.items():
            if key != "all":
                commands.append({"percent": pct, "channel": int(key)})
        return commands

    @property
    def has_overrides(self):
        return bool(self._overrides)

    @property
    def state_summary(self):
        """Human-readable summary for logging."""
        if not self._overrides:
            return "auto"
        parts = []
        for k, v in self._overrides.items():
            label = "all" if k == "all" else f"ch{k}"
            parts.append(f"{label}={v:.0f}%")
        return ", ".join(parts)

    def _save(self):
        try:
            state_dir = os.path.dirname(self._state_file)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            # Convert keys to strings for JSON
            data = {str(k): v for k, v in self._overrides.items()}
            with open(self._state_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning("Failed to save override state: %s", e)

    def _load(self):
        try:
            if os.path.isfile(self._state_file):
                with open(self._state_file, "r") as f:
                    data = json.load(f)
                self._overrides = {}
                for k, v in data.items():
                    if k == "all":
                        self._overrides["all"] = float(v)
                    else:
                        self._overrides[int(k)] = float(v)
                log.info("Loaded saved override state: %s", self.state_summary)
        except Exception as e:
            log.warning("Failed to load override state: %s", e)
            self._overrides = {}


# ============================================================================
# Temperature Reader
# ============================================================================

class TempReader:
    """
    Reads temperatures from lm-sensors.
    Supports auto-detection for CPU and user-configured sensor mappings
    via chip/sensor glob patterns.
    """

    def __init__(self, sensor_config):
        """
        sensor_config: dict from config, e.g.
        {
            "cpu": "auto",
            "nvme": {"chip": "nvme-pci-0400", "sensor": "Composite"},
            "chipset": {"chip": "nct6687*", "sensor": "PCH CHIP"}
        }
        """
        self._sensor_config = sensor_config
        self._last_temps = {}    # {name: float}
        self._last_read_time = None

    def read_all(self):
        """
        Run `sensors -j` and extract all configured temperatures.
        Returns dict of {name: temp_float}, e.g. {"cpu": 52.0, "nvme": 40.8}.
        Values are None for sensors that couldn't be read.
        """
        try:
            result = subprocess.run(
                ["sensors", "-j"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                log.warning("sensors command failed: %s", result.stderr.strip())
                return dict(self._last_temps)

            data = json.loads(result.stdout)
            temps = {}

            for name, mapping in self._sensor_config.items():
                if mapping == "auto":
                    temps[name] = self._find_cpu_temp(data)
                elif isinstance(mapping, dict):
                    chip_pat = mapping.get("chip", "*")
                    sensor_pat = mapping.get("sensor", "*")
                    temps[name] = self._find_mapped_temp(
                        chip_pat, sensor_pat, data
                    )
                else:
                    log.warning("Invalid sensor mapping for '%s': %s",
                                name, mapping)
                    temps[name] = None

            self._last_temps = temps
            self._last_read_time = time.time()
            return temps

        except subprocess.TimeoutExpired:
            log.warning("sensors command timed out")
            return dict(self._last_temps)
        except Exception as e:
            log.warning("Failed to read temperatures: %s", e)
            return dict(self._last_temps)

    def _find_mapped_temp(self, chip_pattern, sensor_pattern, data):
        """
        Find a temperature by matching chip name and sensor label
        using fnmatch glob patterns.
        """
        for chip_name, chip_data in data.items():
            if not isinstance(chip_data, dict):
                continue
            if not fnmatch.fnmatch(chip_name.lower(), chip_pattern.lower()):
                continue

            for sensor_name, sensor_data in chip_data.items():
                if not isinstance(sensor_data, dict):
                    continue
                if not fnmatch.fnmatch(sensor_name.lower(), sensor_pattern.lower()):
                    continue

                # Find the *_input value
                for key, val in sensor_data.items():
                    if "input" in key.lower() and isinstance(val, (int, float)):
                        return float(val)

        log.debug("No match for chip='%s' sensor='%s'", chip_pattern, sensor_pattern)
        return None

    def _find_cpu_temp(self, data):
        """
        Search sensors JSON output for CPU package temperature.
        Handles coretemp, k10temp, and other common chip formats.
        """
        # Chip prefixes that are definitely not CPU sensors
        skip_chips = ("nvme", "iwlwifi", "enp", "eth", "waterforce", "acpitz", "spd5118")

        for chip_name, chip_data in data.items():
            if not isinstance(chip_data, dict):
                continue
            if any(chip_name.lower().startswith(s) for s in skip_chips):
                continue

            for sensor_name, sensor_data in chip_data.items():
                if not isinstance(sensor_data, dict):
                    continue

                # Look for package/Tctl/Tdie temperature
                name_lower = sensor_name.lower()
                is_cpu = any(kw in name_lower for kw in [
                    "package", "tctl", "tdie", "cpu"
                ])

                if is_cpu:
                    for key, val in sensor_data.items():
                        if "input" in key.lower() and isinstance(val, (int, float)):
                            return float(val)

        # Fallback: try the first temp_input we find under coretemp / k10temp
        for chip_name, chip_data in data.items():
            if "coretemp" in chip_name.lower() or "k10temp" in chip_name.lower():
                if isinstance(chip_data, dict):
                    for sensor_name, sensor_data in chip_data.items():
                        if isinstance(sensor_data, dict):
                            for key, val in sensor_data.items():
                                if "input" in key.lower() and isinstance(val, (int, float)):
                                    return float(val)

        log.warning("Could not find CPU temperature in sensors output")
        return None

    @property
    def last_temp(self):
        """CPU temperature (for fan curve and backward compat)."""
        return self._last_temps.get("cpu")

    @property
    def last_temps(self):
        """All sensor temperatures as a dict."""
        return dict(self._last_temps)

    @property
    def last_read_time(self):
        return self._last_read_time


# ============================================================================
# Serial Protocol
# ============================================================================

class SerialProtocol:
    """
    SYN/ACK/SYN-ACK protocol handler over USB serial.
    Thread-safe for concurrent access from API handlers.
    """

    def __init__(self, config):
        self._config = config
        self._port = None
        self._seq = 0
        self._lock = threading.Lock()
        self._connected = False
        self._last_status = None
        self._last_error = None
        self._shutting_down = False
        self._active_port_path = None  # actual /dev path in use

    def find_device(self):
        """
        Discover the RP2040 serial device by USB VID/PID.
        Returns the device path (e.g. /dev/ttyACM0) or None.
        """
        vid = self._config.serial_vid
        pid = self._config.serial_pid

        for port_info in serial.tools.list_ports.comports():
            if port_info.vid == vid and port_info.pid == pid:
                log.info("Discovered RP2040 at %s (serial: %s)",
                         port_info.device, port_info.serial_number or "n/a")
                return port_info.device

        log.warning("No USB device found with VID=0x%04X PID=0x%04X", vid, pid)
        return None

    def _resolve_port(self):
        """
        Resolve the serial port path.  If config is 'auto', discover by
        VID/PID; otherwise use the configured path directly.
        """
        if self._config.serial_port == "auto":
            return self.find_device()
        return self._config.serial_port

    def connect(self):
        """Open the serial port. Returns True on success."""
        port_path = self._resolve_port()
        if port_path is None:
            self._connected = False
            self._last_error = "No RP2040 device found"
            log.error(self._last_error)
            return False

        try:
            self._port = serial.Serial(
                port=port_path,
                baudrate=self._config.serial_baud,
                timeout=self._config.serial_timeout,
            )
            self._connected = True
            self._active_port_path = port_path
            self._last_error = None
            # Flush any boot messages / stale data from the RP2040.
            # The RP2040 may still be in _wait_for_synack (up to 2s) for a
            # previous seq, so we wait long enough for that to expire, then
            # drain everything.
            time.sleep(1.0)
            self._port.reset_input_buffer()
            self._port.reset_output_buffer()
            log.info("Serial connected: %s @ %d baud",
                     port_path, self._config.serial_baud)
            return True
        except serial.SerialException as e:
            self._connected = False
            self._last_error = str(e)
            log.error("Serial connection failed on %s: %s", port_path, e)
            return False

    def close_port(self):
        """Close the serial port (caller MUST hold self._lock or be single-threaded)."""
        if self._port and self._port.is_open:
            try:
                self._port.close()
            except Exception as e:
                log.warning("Error closing serial port: %s", e)
        self._connected = False
        self._active_port_path = None

    def request_reconnect(self):
        """Thread-safe: close port so the main loop reconnects on next iteration."""
        with self._lock:
            self.close_port()

    def disconnect(self):
        """Close the serial port for shutdown. Waits for any in-flight command to finish."""
        self._shutting_down = True
        with self._lock:
            self.close_port()

    def send_command(self, cmd, payload=None):
        """
        Execute full SYN → ACK → SYN-ACK handshake.
        Returns (success: bool, response: dict or None).
        Thread-safe.
        """
        with self._lock:
            return self._send_command_locked(cmd, payload)

    def _send_command_locked(self, cmd, payload=None):
        """Internal send - must be called under lock."""
        if not self._connected or not self._port or not self._port.is_open:
            if not self.connect():
                return False, {"error": "not connected"}

        self._seq += 1
        seq = self._seq

        syn_msg = {
            "seq": seq,
            "type": "SYN",
            "cmd": cmd,
        }
        if payload is not None:
            syn_msg["payload"] = payload

        for attempt in range(self._config.serial_retries):
            try:
                # Send SYN
                line = json.dumps(syn_msg) + "\n"
                self._port.write(line.encode("utf-8"))
                self._port.flush()
                log.debug("TX SYN [%d] %s (attempt %d)", seq, cmd, attempt + 1)

                # Wait for ACK — skip any non-JSON lines (e.g. stale boot messages)
                deadline = time.time() + self._config.serial_timeout
                ack = None
                while time.time() < deadline:
                    ack_line = self._port.readline()
                    if not ack_line:
                        break  # timeout
                    text = ack_line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        log.debug("Skipping non-JSON line: %s", text[:80])
                        continue
                    if parsed.get("type") == "ACK" and parsed.get("seq") == seq:
                        ack = parsed
                        break
                    else:
                        log.debug("Ignoring unexpected msg: %s", parsed)

                if ack is None:
                    log.warning("ACK timeout [%d] attempt %d", seq, attempt + 1)
                    continue

                # Send SYN-ACK
                synack = {"seq": seq, "type": "SYN-ACK"}
                self._port.write((json.dumps(synack) + "\n").encode("utf-8"))
                self._port.flush()
                log.debug("TX SYN-ACK [%d]", seq)

                self._last_status = ack.get("payload")
                self._last_error = None
                return True, ack

            except (serial.SerialException, TypeError, OSError) as e:
                log.error("Serial error [%d]: %s", seq, e)
                self._last_error = str(e)
                # close_port, not disconnect — we already hold the lock
                self.close_port()
                return False, {"error": str(e)}

        self._last_error = f"Failed after {self._config.serial_retries} retries"
        # All retries exhausted — close port under lock so reconnect
        # happens cleanly on the next loop iteration.
        self.close_port()
        return False, {"error": self._last_error}

    @property
    def is_connected(self):
        return self._connected

    @property
    def last_status(self):
        return self._last_status

    @property
    def last_error(self):
        return self._last_error

    @property
    def active_port(self):
        return self._active_port_path


# ============================================================================
# Fan Control Service (main daemon)
# ============================================================================

class FanControlService:
    """Main service orchestrator: temp collection + serial + API."""

    def __init__(self):
        self._config = Config()
        self._temp_reader = TempReader(self._config.temp_sensors)
        self._serial = SerialProtocol(self._config)
        self._override_tracker = OverrideTracker(self._config.state_file)
        self._running = False
        self._shutdown_event = threading.Event()
        self._start_time = time.time()
        self._loop_count = 0

        # Setup logging
        logging.basicConfig(
            level=getattr(logging, self._config.log_level.upper(), logging.INFO),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        log.info("Config: %s", self._config.to_dict())

    def start(self):
        """Start the service: connect serial, start API, begin temp loop."""
        self._running = True

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Connect to RP2040
        port_desc = self._config.serial_port
        if port_desc == "auto":
            log.info("Searching for RP2040 (VID=0x%04X PID=0x%04X)...",
                     self._config.serial_vid, self._config.serial_pid)
        else:
            log.info("Connecting to RP2040 on %s...", port_desc)

        if not self._serial.connect():
            log.warning("Initial serial connection failed - will retry in main loop")
        else:
            self._apply_saved_overrides()

        # Start API server in background thread
        from api_server import APIServer
        self._api = APIServer(self)
        self._api.start()

        # Main temperature polling loop
        log.info("Starting temperature polling every %ds", self._config.poll_interval)
        self._run_loop()

    def _run_loop(self):
        """Main polling loop."""
        while self._running:
            self._loop_count += 1

            if not self._serial.is_connected:
                # Not connected — attempt reconnection and wait
                log.info("Serial disconnected, attempting reconnection...")
                if self._serial.connect():
                    log.info("Reconnected successfully")
                    self._apply_saved_overrides()
                else:
                    log.warning("Reconnection failed, retrying in %ds",
                                self._config.reconnect_interval)
                    self._interruptible_sleep(self._config.reconnect_interval)
                    continue

            # Read temperatures
            temps = self._temp_reader.read_all()
            cpu_temp = temps.get("cpu")

            if cpu_temp is not None:
                log.info("CPU: %.1f°C", cpu_temp)

                # Send CPU temp to RP2040 for fan curve
                ok, resp = self._serial.send_command("SET_TEMP", {"cpu": cpu_temp})
                if ok:
                    payload = resp.get("payload", {})
                    fans = payload.get("fans", [])
                    mode = payload.get("mode", "?")
                    log.info("Fans: %s  Mode: %s", fans, mode)
                else:
                    log.warning("Serial send failed: %s", resp)
                    # send_command already closed the port under lock —
                    # next loop iteration will reconnect.
            else:
                log.warning("CPU temperature read failed (loop %d)", self._loop_count)

            # Log additional sensor temps at debug level
            for name, val in temps.items():
                if name != "cpu" and val is not None:
                    log.debug("%s: %.1f°C", name, val)

            # Sleep for poll interval (interruptible)
            self._interruptible_sleep(self._config.poll_interval)

    def _interruptible_sleep(self, seconds):
        """Sleep that can be interrupted by shutdown signal."""
        self._shutdown_event.wait(timeout=seconds)

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM / SIGINT for clean shutdown."""
        sig_name = signal.Signals(signum).name
        log.info("Received %s - shutting down", sig_name)
        self.shutdown()

    def shutdown(self):
        """Clean shutdown: stop loop, release serial port, stop API, then exit."""
        self._running = False
        self._shutdown_event.set()
        if hasattr(self, "_api"):
            self._api.stop()
        self._serial.disconnect()
        log.info("Shutdown complete")
        sys.exit(0)

    def _apply_saved_overrides(self):
        """Replay any saved override state to the RP2040 after reconnection."""
        if not self._override_tracker.has_overrides:
            return

        log.info("Re-applying saved overrides: %s",
                 self._override_tracker.state_summary)
        for payload in self._override_tracker.get_restore_commands():
            ok, resp = self._serial.send_command("SET_OVERRIDE", payload)
            if ok:
                ch = payload.get("channel", "all")
                log.info("Restored override: ch=%s duty=%.0f%%",
                         ch, payload["percent"])
            else:
                log.warning("Failed to restore override %s: %s", payload, resp)
                break  # Don't keep trying if serial is broken

    # --- Properties for API access ---

    @property
    def config(self):
        return self._config

    @property
    def serial(self):
        return self._serial

    @property
    def temp_reader(self):
        return self._temp_reader

    @property
    def uptime(self):
        return time.time() - self._start_time

    @property
    def loop_count(self):
        return self._loop_count

    @property
    def is_running(self):
        return self._running

    @property
    def override_tracker(self):
        return self._override_tracker


# ============================================================================
# Entry Point
# ============================================================================

def main():
    service = FanControlService()
    service.start()


if __name__ == "__main__":
    main()
