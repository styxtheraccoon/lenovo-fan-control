"""
Lenovo P330 Tiny - RP2040 PWM Fan Controller
Main entry point for MicroPython firmware.

Boots fans at 100%, listens for temperature data over USB serial,
and adjusts fan speeds via configurable fan curve.
Falls back to 100% if communication is lost for >15 seconds.
Hard-resets (USB re-enumeration) if comms aren't restored within
60s (failsafe) or 180s (boot) to clear stale serial state.
"""

import time
from fan_controller import FanController
from watchdog import Watchdog
from serial_handler import SerialHandler
import config


def main():


    # Initialise subsystems
    fans = FanController()


    wdog = Watchdog(fans)
    serial = SerialHandler(fans, wdog)



    # Main loop
    while True:
        # Check for incoming serial commands
        try:
            serial.poll()
        except Exception as e:
            pass

        # Check watchdog timer
        if wdog.check():
            pass

        # Sample tachometer (no-op if tach disabled)
        fans.sample_tach()

        # Ramp duty cycles toward targets (no-op if ramping disabled)
        fans.ramp_tick()

        # Brief sleep to avoid busy-wait
        time.sleep_ms(config.LOOP_SLEEP_MS)


# MicroPython auto-runs main.py on boot
if __name__ == "__main__":
    main()
