"""
Fan Controller - PWM management for 4x 40mm fans
Handles PWM output, fan curve interpolation, and mode management.
"""

from machine import Pin, PWM
import config


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

    def get_status(self):
        """Return current state as a dict for serial reporting."""
        return {
            "fans": list(self._duties),
            "mode": self._mode,
            "last_temp": self._last_temp,
        }

    def deinit(self):
        """Clean up PWM resources."""
        for pwm in self._fans:
            pwm.deinit()
