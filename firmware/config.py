"""
RP2040 Fan Controller Configuration
Lenovo P330 Tiny - 4x 40mm PWM Fan Control
"""

# --- PWM Configuration ---
# GPIO pins for PWM fan outputs on RP2040 Zero
FAN_PINS = [0, 2, 4, 6]

# Number of active PWM channels (1-4). Only the first N pins in FAN_PINS
# are initialised. Channels are phase-offset to reduce USB rail current ripple.
PWM_CHANNELS = 4

# PWM frequency: 25kHz per Intel 4-wire PWM fan spec
PWM_FREQUENCY = 25000

# --- Duty Ramping (noise mitigation) ---
# Smooth duty transitions to reduce transient current spikes.
# At LOOP_SLEEP_MS=50 and DUTY_RAMP_STEP=2, full 0→100% takes ~2.5s.
DUTY_RAMP_ENABLED = True
DUTY_RAMP_STEP = 2              # Max duty % change per main loop tick

# --- Fan Curve ---
# Piecewise-linear: (temp_c, duty_percent)
# Interpolated between points, clamped at extremes
FAN_CURVE = [
    (30, 25),   # 30°C → 25% duty (quiet idle)
    (50, 50),   # 50°C → 50% duty
    (70, 80),   # 70°C → 80% duty
    (85, 100),  # 85°C → 100% duty (full blast)
]

# Minimum duty cycle (some fans stall below ~20%)
MIN_DUTY = 20

# --- Tachometer (Advanced) ---
# Enable to read fan RPM via tach signal. Requires wiring fan tach
# (Pin 3 on 4-pin connector) to these GPIO inputs.
# WARNING: RP2040 GPIO is 3.3V only. Do NOT use external 5V pull-ups.
#
# Tach channels are INDEPENDENT of PWM channels. You can have fewer
# PWM outputs (daisy-chained) while monitoring RPM on all fans.
# TACH_TO_PWM maps each tach input to its controlling PWM channel index
# for stall detection (e.g. [0, 0, 1, 1] = tach 0,1 on PWM 0; tach 2,3 on PWM 1).
TACH_ENABLED = False              # Set True to enable RPM reading
TACH_PINS = [1, 3, 5, 7]         # GPIO inputs for tach signals
TACH_TO_PWM = [0, 1, 2, 3]       # Which PWM channel drives each tach's fan
TACH_PULSES_PER_REV = 2           # Standard: 2 pulses/rev (Noctua, most 4-pin fans)
TACH_SAMPLE_MS = 1000             # RPM measurement window (ms)
TACH_STALL_THRESHOLD = 2          # Consecutive zero-RPM samples before stall flag

# --- Watchdog ---
# Seconds without a valid message before failsafe kicks in
WATCHDOG_TIMEOUT_S = 15

# Duty cycle when watchdog triggers (100% = full safety)
FAILSAFE_DUTY = 100

# Duty cycle on boot before first temperature received
BOOT_DUTY = 100

# --- Reboot Timeouts ---
# Hard-reset the RP2040 if communication isn't (re-)established within
# these windows.  A hard reset tears down the USB CDC endpoint, forcing
# the host kernel to re-enumerate the device and clearing any stale
# serial state on both sides.

# Seconds in FAILSAFE mode before hard reset (host reboot recovery)
FAILSAFE_REBOOT_TIMEOUT_S = 60

# Seconds in BOOT mode (initial power-on) before hard reset
# Longer than failsafe — the host service may not be started yet.
BOOT_REBOOT_TIMEOUT_S = 180

# --- Serial ---
SERIAL_BAUD = 115200

# --- Timing ---
# Main loop sleep (ms) - keep responsive but not busy-waiting
LOOP_SLEEP_MS = 50
