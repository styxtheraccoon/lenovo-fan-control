"""
Fan Controller - PWM management for configurable fan channels
Handles PWM output, fan curve interpolation, and mode management.
Optional tachometer input for RPM reading and stall detection.

Noise mitigation:
  - Phase-offset PWM slices to spread USB current draw
  - Duty ramping to smooth transitions and reduce transient spikes
"""

from machine import Pin, PWM, mem32
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
        self._num_tach = len(config.TACH_PINS)
        self._pwm_map = config.TACH_TO_PWM
        self._pins = []
        self._counts = [0] * self._num_tach
        self._rpms = [0] * self._num_tach
        self._stall_counters = [0] * self._num_tach
        self._stalled = [False] * self._num_tach
        self._last_sample_ms = time.ticks_ms()

        for i in range(self._num_tach):
            pin_num = config.TACH_PINS[i]
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
        `duties` is the current PWM duty list — used with TACH_TO_PWM
        mapping to determine stall (RPM=0 while duty > MIN_DUTY).
        Returns True if sample window elapsed and values were updated.
        """
        now = time.ticks_ms()
        elapsed = time.ticks_diff(now, self._last_sample_ms)

        if elapsed < config.TACH_SAMPLE_MS:
            return False

        self._last_sample_ms = now
        window_s = elapsed / 1000.0

        for i in range(self._num_tach):
            pulses = self._counts[i]
            self._counts[i] = 0

            # RPM = (pulses / pulses_per_rev) * (60 / window_seconds)
            if window_s > 0 and config.TACH_PULSES_PER_REV > 0:
                self._rpms[i] = int(
                    (pulses / config.TACH_PULSES_PER_REV) * (60.0 / window_s)
                )
            else:
                self._rpms[i] = 0

            # Stall detection: look up the PWM channel driving this fan
            pwm_ch = self._pwm_map[i] if i < len(self._pwm_map) else 0
            duty = duties[pwm_ch] if pwm_ch < len(duties) else 0
            if self._rpms[i] == 0 and duty > config.MIN_DUTY:
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

    @property
    def num_tach(self):
        return self._num_tach

    def deinit(self):
        """Disable IRQs."""
        for pin in self._pins:
            pin.irq(handler=None)


class FanController:
    """Manages configurable PWM fan outputs with per-channel mode tracking."""

    MODE_BOOT = "boot"
    MODE_AUTO = "auto"
    MODE_OVERRIDE = "override"
    MODE_FAILSAFE = "failsafe"

    # RP2040 PWM register addresses for phase offset
    _PWM_BASE = 0x40050000
    _SLICE_STRIDE = 0x14
    _CTR_OFFSET = 0x08
    _TOP_OFFSET = 0x10

    def __init__(self):
        self._num_channels = min(config.PWM_CHANNELS, len(config.FAN_PINS))
        self._fans = []
        self._duties = [0] * self._num_channels
        self._target_duties = [0] * self._num_channels
        self._modes = [self.MODE_BOOT] * self._num_channels
        self._last_temp = None

        # Initialise PWM on active channels
        for i in range(self._num_channels):
            pwm = PWM(Pin(config.FAN_PINS[i]))
            pwm.freq(config.PWM_FREQUENCY)
            self._fans.append(pwm)

        # Stagger PWM phases to reduce USB rail current ripple
        self._apply_phase_offsets()

        # Boot at full speed as sanity check (immediate, no ramp)
        self._set_all_duty_immediate(config.BOOT_DUTY)

        # Initialise tach reader if enabled (independent of PWM channel count)
        self._tach = TachReader() if config.TACH_ENABLED else None

    def _apply_phase_offsets(self):
        """
        Offset PWM slice counters so channels switch at staggered intervals.
        For N channels, channel i starts at i * (TOP+1) / N.
        This spreads the current draw across the PWM period, halving (or
        better) the peak transient on the USB VBUS rail.

        GPIO-to-slice mapping: slice = gpio_num // 2
          GPIO 0 → slice 0, GPIO 2 → slice 1, GPIO 4 → slice 2, GPIO 6 → slice 3
        """
        if self._num_channels <= 1:
            return

        for i in range(self._num_channels):
            slice_num = config.FAN_PINS[i] // 2
            addr_top = self._PWM_BASE + (slice_num * self._SLICE_STRIDE) + self._TOP_OFFSET
            addr_ctr = self._PWM_BASE + (slice_num * self._SLICE_STRIDE) + self._CTR_OFFSET
            top = mem32[addr_top]
            offset = (i * (top + 1)) // self._num_channels
            mem32[addr_ctr] = offset

    def _percent_to_u16(self, percent):
        """Convert 0-100% to 0-65535 PWM duty value."""
        clamped = max(0, min(100, percent))
        return int(clamped * 65535 / 100)

    # --- Immediate duty (bypasses ramping) ---

    def _set_duty_immediate(self, fan_index, percent):
        """Set PWM duty immediately. Used for failsafe and boot."""
        if 0 <= fan_index < self._num_channels:
            self._duties[fan_index] = percent
            self._target_duties[fan_index] = percent
            self._fans[fan_index].duty_u16(self._percent_to_u16(percent))

    def _set_all_duty_immediate(self, percent):
        """Set all channels immediately."""
        for i in range(self._num_channels):
            self._set_duty_immediate(i, percent)

    # --- Target duty (respects ramping) ---

    def set_duty(self, fan_index, percent):
        """Set target duty for a channel (ramps if enabled)."""
        if 0 <= fan_index < self._num_channels:
            self._target_duties[fan_index] = percent
            if not config.DUTY_RAMP_ENABLED:
                self._duties[fan_index] = percent
                self._fans[fan_index].duty_u16(self._percent_to_u16(percent))

    def set_all_duty(self, percent):
        """Set all active channels to the same target duty."""
        for i in range(self._num_channels):
            self.set_duty(i, percent)

    def ramp_tick(self):
        """
        Move current duties toward targets by DUTY_RAMP_STEP.
        Call from main loop. No-op if ramping is disabled.
        """
        if not config.DUTY_RAMP_ENABLED:
            return

        step = config.DUTY_RAMP_STEP
        for i in range(self._num_channels):
            current = self._duties[i]
            target = self._target_duties[i]
            if current == target:
                continue
            if current < target:
                new = min(current + step, target)
            else:
                new = max(current - step, target)
            self._duties[i] = new
            self._fans[i].duty_u16(self._percent_to_u16(new))

    # --- Fan curve ---

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

    # --- Mode management ---

    def update_from_temp(self, temp_c):
        """Update fan speeds based on temperature (auto/boot channels only)."""
        self._last_temp = temp_c
        duty = self.temp_to_duty(temp_c)
        for i in range(self._num_channels):
            if self._modes[i] in (self.MODE_AUTO, self.MODE_BOOT):
                self.set_duty(i, duty)
                self._modes[i] = self.MODE_AUTO

    def set_override(self, percent):
        """Manual override — all active channels."""
        for i in range(self._num_channels):
            self._modes[i] = self.MODE_OVERRIDE
            self.set_duty(i, percent)

    def set_override_channel(self, channel, percent):
        """Manual override — single channel."""
        if 0 <= channel < self._num_channels:
            self._modes[channel] = self.MODE_OVERRIDE
            self.set_duty(channel, percent)

    def set_auto(self):
        """Return all channels to automatic fan curve mode."""
        for i in range(self._num_channels):
            self._modes[i] = self.MODE_AUTO
        if self._last_temp is not None:
            self.update_from_temp(self._last_temp)

    def set_auto_channel(self, channel):
        """Return a single channel to automatic fan curve mode."""
        if 0 <= channel < self._num_channels:
            self._modes[channel] = self.MODE_AUTO
            if self._last_temp is not None:
                duty = self.temp_to_duty(self._last_temp)
                self.set_duty(channel, duty)

    def trigger_failsafe(self):
        """Watchdog triggered — immediate full speed, all channels."""
        for i in range(self._num_channels):
            self._modes[i] = self.MODE_FAILSAFE
        self._set_all_duty_immediate(config.FAILSAFE_DUTY)

    # --- Tach ---

    def sample_tach(self):
        """
        Sample tachometer readings. Call from main loop.
        No-op if tach is disabled.
        """
        if self._tach is not None:
            self._tach.sample(self._duties)

    # --- Status ---

    @property
    def overall_mode(self):
        """Aggregate mode: failsafe > boot > override > auto."""
        modes = set(self._modes)
        if self.MODE_FAILSAFE in modes:
            return self.MODE_FAILSAFE
        if self.MODE_BOOT in modes:
            return self.MODE_BOOT
        if self.MODE_OVERRIDE in modes:
            return self.MODE_OVERRIDE
        return self.MODE_AUTO

    @property
    def num_channels(self):
        return self._num_channels

    def get_status(self):
        """Return current state as a dict for serial reporting."""
        status = {
            "fans": list(self._duties),
            "modes": list(self._modes),
            "mode": self.overall_mode,
            "channels": self._num_channels,
            "last_temp": self._last_temp,
        }
        if self._tach is not None:
            status["rpm"] = self._tach.rpms
            status["stall"] = self._tach.stalled
            status["any_stalled"] = self._tach.any_stalled
            status["tach_channels"] = self._tach.num_tach
        return status

    def deinit(self):
        """Clean up PWM and tach resources."""
        for pwm in self._fans:
            pwm.deinit()
        if self._tach is not None:
            self._tach.deinit()
