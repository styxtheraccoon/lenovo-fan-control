"""
Serial Handler - USB CDC serial protocol for host communication
Implements JSON-based SYN/ACK/SYN-ACK protocol over USB serial.
"""

import sys
import json
import time
import select


class SerialHandler:
    """
    Handles JSON-over-serial communication with the host.

    Protocol flow:
        Host → RP2040:  {"seq":N, "type":"SYN", "cmd":"...", "payload":{...}}
        RP2040 → Host:  {"seq":N, "type":"ACK", "status":"ok"|"error", "payload":{...}}
        Host → RP2040:  {"seq":N, "type":"SYN-ACK"}

    Commands: SET_TEMP, GET_STATUS, SET_OVERRIDE, SET_AUTO, PING
    """

    def __init__(self, fan_controller, watchdog):
        self._fan_controller = fan_controller
        self._watchdog = watchdog
        self._poll = select.poll()
        self._poll.register(sys.stdin, select.POLLIN)
        self._buf = ""
        self._last_ack_seq = -1
        self._synack_timeout_ms = 2000  # Wait up to 2s for SYN-ACK

    def _send(self, msg_dict):
        """Send a JSON message line to host."""
        line = json.dumps(msg_dict)
        sys.stdout.write(line + "\n")

    def _send_ack(self, seq, status, payload=None):
        """Send ACK response to host."""
        msg = {
            "seq": seq,
            "type": "ACK",
            "status": status,
        }
        if payload is not None:
            msg["payload"] = payload
        self._send(msg)
        self._last_ack_seq = seq

    def _handle_command(self, msg):
        """Process a validated SYN message and return (status, payload)."""
        cmd = msg.get("cmd", "")
        payload = msg.get("payload", {})

        if cmd == "SET_TEMP":
            cpu_temp = payload.get("cpu")
            if cpu_temp is None:
                return "error", {"error": "missing cpu temp"}
            self._fan_controller.update_from_temp(float(cpu_temp))
            return "ok", self._fan_controller.get_status()

        elif cmd == "GET_STATUS":
            status_data = self._fan_controller.get_status()
            status_data["watchdog"] = {
                "triggered": self._watchdog.is_triggered,
                "since_feed": round(self._watchdog.seconds_since_feed, 1),
            }
            return "ok", status_data

        elif cmd == "SET_OVERRIDE":
            percent = payload.get("percent")
            if percent is None:
                return "error", {"error": "missing percent"}
            self._fan_controller.set_override(float(percent))
            return "ok", self._fan_controller.get_status()

        elif cmd == "SET_AUTO":
            self._fan_controller.set_auto()
            return "ok", self._fan_controller.get_status()

        elif cmd == "PING":
            return "ok", {"pong": True, "uptime_ms": time.ticks_ms()}

        else:
            return "error", {"error": "unknown command: " + cmd}

    def _wait_for_synack(self, seq):
        """
        Wait for SYN-ACK confirmation from host.
        Returns True if received, False on timeout.
        """
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < self._synack_timeout_ms:
            events = self._poll.poll(100)  # 100ms poll intervals
            if events:
                chunk = sys.stdin.readline()
                if chunk:
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    try:
                        msg = json.loads(chunk)
                        if msg.get("type") == "SYN-ACK" and msg.get("seq") == seq:
                            return True
                    except (ValueError, KeyError):
                        pass  # Ignore malformed messages while waiting
        return False

    def poll(self):
        """
        Non-blocking check for incoming serial data.
        Processes one complete message if available.
        Returns True if a valid message was processed.
        """
        events = self._poll.poll(0)  # Non-blocking
        if not events:
            return False

        try:
            line = sys.stdin.readline()
        except Exception:
            return False

        if not line:
            return False

        line = line.strip()
        if not line:
            return False

        # Parse JSON
        try:
            msg = json.loads(line)
        except ValueError:
            return False

        # Validate it's a SYN message
        if msg.get("type") != "SYN":
            return False

        seq = msg.get("seq")
        if seq is None:
            return False

        # Feed watchdog on any valid SYN
        self._watchdog.feed()

        # Process the command
        status, payload = self._handle_command(msg)

        # Send ACK
        self._send_ack(seq, status, payload)

        # Wait for SYN-ACK (non-critical if missed - host will retry)
        self._wait_for_synack(seq)

        return True
