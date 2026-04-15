"""
Watchdog Timer - Safety fallback for lost communication
Triggers failsafe fan speed if no valid message received within timeout.
"""

import time
import config


class Watchdog:
    """Software watchdog that triggers failsafe if communication is lost."""

    def __init__(self, fan_controller):
        self._fan_controller = fan_controller
        self._last_feed_time = None  # None = never fed (boot state)
        self._triggered = False
        self._timeout_s = config.WATCHDOG_TIMEOUT_S

    def feed(self):
        """Reset the watchdog timer. Call on every valid message received."""
        self._last_feed_time = time.ticks_ms()
        if self._triggered:
            self._triggered = False

    def check(self):
        """
        Check if watchdog has expired. Call from main loop.
        Returns True if failsafe was just triggered.
        """
        # Not yet fed - we're still in boot, fans already at BOOT_DUTY
        if self._last_feed_time is None:
            return False

        elapsed_ms = time.ticks_diff(time.ticks_ms(), self._last_feed_time)

        if elapsed_ms > (self._timeout_s * 1000) and not self._triggered:
            self._triggered = True
            self._fan_controller.trigger_failsafe()
            return True

        return False

    @property
    def is_triggered(self):
        """Whether the watchdog is currently in failsafe state."""
        return self._triggered

    @property
    def seconds_since_feed(self):
        """Seconds since last valid message, or -1 if never fed."""
        if self._last_feed_time is None:
            return -1
        return time.ticks_diff(time.ticks_ms(), self._last_feed_time) / 1000
