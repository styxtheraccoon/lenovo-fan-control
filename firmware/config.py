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
