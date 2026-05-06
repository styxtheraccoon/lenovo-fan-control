"""
Watchdog Timer - Safety fallback for lost communication
Triggers failsafe fan speed if no valid message received within timeout.

If communication is not re-established within the reboot timeout window,
performs a hard reset (machine.reset()) to tear down the USB CDC endpoint
and force the host to re-enumerate the device.
"""

import time
import machine
import config


class Watchdog:
    """Software watchdog that triggers failsafe if communication is lost.

    Reboot behaviour:
        BOOT mode  — if no SYN arrives within BOOT_REBOOT_TIMEOUT_S after
                     power-on, hard-reset to force USB re-enumeration.
        FAILSAFE   — if the host doesn't recover communication within
                     FAILSAFE_REBOOT_TIMEOUT_S, hard-reset to clear any
                     stale USB/serial state on both sides.

    A successful feed() cancels any pending reboot timer.
    """

    def __init__(self, fan_controller):
        self._fan_controller = fan_controller
        self._last_feed_time = None  # None = never fed (boot state)
        self._triggered = False
        self._timeout_s = config.WATCHDOG_TIMEOUT_S

        # Reboot timers — timestamps (ticks_ms) for hard-reset deadlines
        self._boot_time_ms = time.ticks_ms()       # When the micro booted
        self._failsafe_enter_ms = None              # When failsafe was entered

    def feed(self):
        """Reset the watchdog timer. Call on every valid message received."""
        self._last_feed_time = time.ticks_ms()
        self._failsafe_enter_ms = None  # Cancel failsafe reboot timer
        if self._triggered:
            self._triggered = False
            # Recover from failsafe — restore pre-failsafe channel modes
            # so override channels keep their duty, auto channels resume curve
            self._fan_controller.recover_from_failsafe()

    def check(self):
        """
        Check if watchdog has expired. Call from main loop.
        Returns True if failsafe was just triggered this call.

        May call machine.reset() and never return if reboot timeout
        has been exceeded in either boot or failsafe state.
        """
        now = time.ticks_ms()

        # ── Boot state: never fed yet, fans already at BOOT_DUTY ──
        if self._last_feed_time is None:
            boot_elapsed = time.ticks_diff(now, self._boot_time_ms)
            if boot_elapsed > (config.BOOT_REBOOT_TIMEOUT_S * 1000):
                # Host never connected — hard reset to re-enumerate USB
                machine.reset()
                # Never reaches here
            return False

        elapsed_ms = time.ticks_diff(now, self._last_feed_time)

        # ── Transition to failsafe ──
        if elapsed_ms > (self._timeout_s * 1000) and not self._triggered:
            self._triggered = True
            self._failsafe_enter_ms = now
            self._fan_controller.trigger_failsafe()
            return True

        # ── Already in failsafe — check reboot timeout ──
        if self._triggered and self._failsafe_enter_ms is not None:
            failsafe_elapsed = time.ticks_diff(now, self._failsafe_enter_ms)
            if failsafe_elapsed > (config.FAILSAFE_REBOOT_TIMEOUT_S * 1000):
                # Host hasn't recovered — hard reset to clear stale USB state
                machine.reset()
                # Never reaches here

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

    @property
    def reboot_countdown(self):
        """Seconds until hard reset, or -1 if no reboot is pending."""
        now = time.ticks_ms()
        if self._last_feed_time is None:
            # Boot mode
            elapsed = time.ticks_diff(now, self._boot_time_ms) / 1000
            return max(0, config.BOOT_REBOOT_TIMEOUT_S - elapsed)
        if self._triggered and self._failsafe_enter_ms is not None:
            # Failsafe mode
            elapsed = time.ticks_diff(now, self._failsafe_enter_ms) / 1000
            return max(0, config.FAILSAFE_REBOOT_TIMEOUT_S - elapsed)
        return -1
