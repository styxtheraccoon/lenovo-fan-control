"""
RP2040 Fan Controller Configuration
Lenovo P330 Tiny - 4x 40mm PWM Fan Control
"""

# --- PWM Pin Assignments ---
# GPIO pins for 4 PWM fan outputs on RP2040 Zero
FAN_PINS = [0, 2, 4, 6]

# PWM frequency: 25kHz per Intel 4-wire PWM fan spec
PWM_FREQUENCY = 25000

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
TACH_ENABLED = False              # Set True to enable RPM reading
TACH_PINS = [1, 3, 5, 7]         # GPIO inputs, adjacent to PWM outputs
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

# --- Serial ---
SERIAL_BAUD = 115200

# --- Timing ---
# Main loop sleep (ms) - keep responsive but not busy-waiting
LOOP_SLEEP_MS = 50
