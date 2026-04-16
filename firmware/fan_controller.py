"""
Fan Controller - PWM management for 4x 40mm fans
Handles PWM output, fan curve interpolation, and mode management.
Optional tachometer input for RPM reading and stall detection.
"""

from machine import Pin, PWM
import time
import config


class TachReader:
    """
    Reads fan RPM from tachometer signals using GPIO IRQ pulse counting.
    Each fan's tach pin is set as input with internal pull-up (3.3V).
    Pulse counting uses IRQ on falling edge — safe for both open-collector
    (Noctua) and open-drain fan tach outputs.
    """

    def __init__(self):
        self._pins = []
        self._counts = [0] * len(config.TACH_PINS)
        self._rpms = [0] * len(config.TACH_PINS)
        self._stall_counters = [0] * len(config.TACH_PINS)
        self._stalled = [False] * len(config.TACH_PINS)
        self._last_sample_ms = time.ticks_ms()

        for i, pin_num in enumerate(config.TACH_PINS):
            pin = Pin(pin_num, Pin.IN, Pin.PULL_UP)
            # Each pin gets its own ISR that increments the right counter.
            # Use default-arg capture to bind `i` at definition time.
            pin.irq(trigger=Pin.IRQ_FALLING,
                     handler=lambda p, idx=i: self._isr(idx))
            self._pins.append(pin)

    def _isr(self, idx):
        """Interrupt handler — just count pulses."""
        self._counts[idx] += 1

    def sample(self, duties):
        """
        Calculate RPM from pulses accumulated since last sample.
        Should be called periodically from the main loop.
        `duties` is the current duty list — used to determine stall
        (RPM=0 while duty > MIN_DUTY).
        Returns True if sample window elapsed and values were updated.
        """
        now = time.ticks_ms()
        elapsed = time.ticks_diff(now, self._last_sample_ms)

        if elapsed < config.TACH_SAMPLE_MS:
            return False

        self._last_sample_ms = now
        window_s = elapsed / 1000.0

        for i in range(len(self._counts)):
            pulses = self._counts[i]
            self._counts[i] = 0

            # RPM = (pulses / pulses_per_rev) * (60 / window_seconds)
            if window_s > 0 and config.TACH_PULSES_PER_REV > 0:
                self._rpms[i] = int(
                    (pulses / config.TACH_PULSES_PER_REV) * (60.0 / window_s)
                )
            else:
                self._rpms[i] = 0

            # Stall detection: fan should be spinning but RPM is 0
            if self._rpms[i] == 0 and duties[i] > config.MIN_DUTY:
                self._stall_counters[i] += 1
            else:
                self._stall_counters[i] = 0

            self._stalled[i] = (
                self._stall_counters[i] >= config.TACH_STALL_THRESHOLD
            )

        return True

    @property
    def rpms(self):
        return list(self._rpms)

    @property
    def stalled(self):
        return list(self._stalled)

    @property
    def any_stalled(self):
        return any(self._stalled)

    def deinit(self):
        """Disable IRQs."""
        for pin in self._pins:
            pin.irq(handler=None)


class FanController:
    """Manages 4 PWM fan outputs with auto/manual/failsafe modes."""

    MODE_BOOT = "boot"
    MODE_AUTO = "auto"
    MODE_OVERRIDE = "override"
    MODE_FAILSAFE = "failsafe"

    def __init__(self):
        self._fans = []
        self._duties = [0, 0, 0, 0]
        self._mode = self.MODE_BOOT
        self._last_temp = None

        # Initialise PWM on each fan pin
        for pin_num in config.FAN_PINS:
            pwm = PWM(Pin(pin_num))
            pwm.freq(config.PWM_FREQUENCY)
            self._fans.append(pwm)

        # Boot at full speed as sanity check
        self.set_all_duty(config.BOOT_DUTY)
        self._mode = self.MODE_BOOT

        # Initialise tach reader if enabled
        self._tach = TachReader() if config.TACH_ENABLED else None

    def _percent_to_u16(self, percent):
        """Convert 0-100% to 0-65535 PWM duty value."""
        clamped = max(0, min(100, percent))
        return int(clamped * 65535 / 100)

    def set_duty(self, fan_index, percent):
        """Set individual fan duty cycle (0–100%)."""
        if 0 <= fan_index < len(self._fans):
            self._duties[fan_index] = percent
            self._fans[fan_index].duty_u16(self._percent_to_u16(percent))

    def set_all_duty(self, percent):
        """Set all fans to the same duty cycle."""
        for i in range(len(self._fans)):
            self.set_duty(i, percent)

    def temp_to_duty(self, temp_c):
        """
        Piecewise-linear interpolation of fan curve.
        Returns duty cycle percentage for a given temperature.
        """
        curve = config.FAN_CURVE

        # Below lowest point
        if temp_c <= curve[0][0]:
            return max(curve[0][1], config.MIN_DUTY)

        # Above highest point
        if temp_c >= curve[-1][0]:
            return curve[-1][1]

        # Interpolate between curve segments
        for i in range(len(curve) - 1):
            t1, d1 = curve[i]
            t2, d2 = curve[i + 1]
            if t1 <= temp_c <= t2:
                ratio = (temp_c - t1) / (t2 - t1)
                duty = d1 + ratio * (d2 - d1)
                return max(duty, config.MIN_DUTY)

        return config.FAILSAFE_DUTY  # Should never reach here

    def update_from_temp(self, temp_c):
        """Update fan speeds based on temperature (auto mode)."""
        self._last_temp = temp_c
        if self._mode in (self.MODE_AUTO, self.MODE_BOOT):
            duty = self.temp_to_duty(temp_c)
            self.set_all_duty(duty)
            self._mode = self.MODE_AUTO

    def set_override(self, percent):
        """Manual override - set all fans to specified duty."""
        self._mode = self.MODE_OVERRIDE
        self.set_all_duty(percent)

    def set_auto(self):
        """Return to automatic fan curve mode."""
        self._mode = self.MODE_AUTO
        if self._last_temp is not None:
            self.update_from_temp(self._last_temp)

    def trigger_failsafe(self):
        """Watchdog triggered - ramp fans to failsafe speed."""
        self._mode = self.MODE_FAILSAFE
        self.set_all_duty(config.FAILSAFE_DUTY)

    def sample_tach(self):
        """
        Sample tachometer readings. Call from main loop.
        No-op if tach is disabled.
        """
        if self._tach is not None:
            self._tach.sample(self._duties)

    def get_status(self):
        """Return current state as a dict for serial reporting."""
        status = {
            "fans": list(self._duties),
            "mode": self._mode,
            "last_temp": self._last_temp,
        }
        if self._tach is not None:
            status["rpm"] = self._tach.rpms
            status["stall"] = self._tach.stalled
            status["any_stalled"] = self._tach.any_stalled
        return status

    def deinit(self):
        """Clean up PWM and tach resources."""
        for pwm in self._fans:
            pwm.deinit()
        if self._tach is not None:
            self._tach.deinit()
