# Lenovo P330 Tiny — Fan Control System

Custom fan control for the Lenovo P330 Tiny using an RP2040 Zero microcontroller and 4× 40mm PWM fans. The host Proxmox service reads CPU temperatures, sends them to the RP2040 over USB serial, and exposes an HTTP API for HomeAssistant / Homepage integration.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Proxmox Host                               │
│                                             │
│  sensors ──► fan-control.service ──► :9780   │
│              (Python daemon)      HTTP API   │
│                   │                          │
│                   │ USB Serial (SYN/ACK)     │
└───────────────────┼─────────────────────────┘
                    │
┌───────────────────┼─────────────────────────┐
│  RP2040 Zero      │                         │
│                   ▼                         │
│  serial_handler ──► fan_controller          │
│                      │  │  │  │             │
│              GPIO 0  2  4  6 (PWM 25kHz)    │
│                 φ=0° 90° 180° 270° (phase)   │
│                   │  │  │  │                │
│              Fan1 Fan2 Fan3 Fan4            │
└─────────────────────────────────────────────┘
```

Channels are configurable (1–4 active). PWM phases are evenly distributed to minimise USB rail current ripple.

## Hardware

| Component | Description |
|-----------|-------------|
| Lenovo P330 Tiny | Proxmox host |
| RP2040 Zero | Waveshare RP2040-Zero or equivalent |
| 4× 40mm PWM fans | 4-pin fans (e.g. Noctua NF-A4x10 PWM) |
| USB cable | Micro-USB / USB-C to host (for power + serial) |

### Wiring

| RP2040 GPIO | Fan Connector | Function |
|-------------|---------------|----------|
| GPIO 0 | Fan 1, Pin 4 (PWM) | PWM signal |
| GPIO 2 | Fan 2, Pin 4 (PWM) | PWM signal |
| GPIO 4 | Fan 3, Pin 4 (PWM) | PWM signal |
| GPIO 6 | Fan 4, Pin 4 (PWM) | PWM signal |
| GPIO 1 | Fan 1, Pin 3 (Tach) | Tach input *(optional, see [Tachometer](#tachometer))* |
| GPIO 3 | Fan 2, Pin 3 (Tach) | Tach input *(optional)* |
| GPIO 5 | Fan 3, Pin 3 (Tach) | Tach input *(optional)* |
| GPIO 7 | Fan 4, Pin 3 (Tach) | Tach input *(optional)* |
| GND | Fan 1–4, Pin 1 (GND) | Common ground |
| VBUS (5V) | Fan 1–4, Pin 2 (VCC) | Fan power (5V from USB) |

> **Note**: 4-pin fan connector pinout: `GND | +12V/5V | Tach | PWM`. These 40mm fans run fine on 5V from USB.

> **Warning**: RP2040 GPIO is **3.3V only**. The firmware uses internal 3.3V pull-ups on tach inputs. Do **NOT** use external 5V pull-ups — this will damage the RP2040.

## Installation

### 1. Flash RP2040 Firmware

1. Install [MicroPython](https://micropython.org/download/RPI_PICO/) on the RP2040 Zero:
   - Hold BOOTSEL, plug in USB, drag the `.uf2` file onto the drive
2. Copy all files from `firmware/` to the RP2040 using [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) or Thonny:
   ```bash
   mpremote connect /dev/ttyACM0 cp firmware/config.py :
   mpremote connect /dev/ttyACM0 cp firmware/fan_controller.py :
   mpremote connect /dev/ttyACM0 cp firmware/serial_handler.py :
   mpremote connect /dev/ttyACM0 cp firmware/watchdog.py :
   mpremote connect /dev/ttyACM0 cp firmware/main.py :
   ```
3. The RP2040 will boot fans at 100% and wait for serial commands.

### 2. Install Host Service

```bash
sudo ./install.sh
```

This will:
- Install `pyserial` and `lm-sensors`
- Copy service files to `/opt/fan-control/`
- Create default config at `/etc/fan-control/config.json`
- Install and enable the systemd service

### 3. Configure

Edit `/etc/fan-control/config.json`:
```json
{
    "serial_port": "auto",
    "serial_baud": 115200,
    "poll_interval": 5,
    "api_port": 9780,
    "api_host": "0.0.0.0",
    "api_key": "your-secure-api-key-here",
    "log_level": "INFO",
    "serial_timeout": 2,
    "serial_retries": 3,
    "reconnect_interval": 5,
    "temp_sensors": {
        "cpu": "auto"
    }
}
```

Or set environment variables in `/etc/fan-control/fan-control.env` (overrides JSON).

### 4. Start

```bash
sudo systemctl start fan-control
sudo journalctl -u fan-control -f
```

## Configuration Reference

| Key / Env Var | Default | Description |
|---------------|---------|-------------|
| `serial_port` / `FAN_CONTROL_SERIAL_PORT` | `auto` | `auto` = discover RP2040 by USB VID/PID, or set a specific path (e.g. `/dev/ttyACM0`) |
| `serial_vid` | `0x2E8A` | USB Vendor ID for auto-discovery (Raspberry Pi Foundation) |
| `serial_pid` | `0x0005` | USB Product ID for auto-discovery (RP2040 MicroPython) |
| `serial_baud` / `FAN_CONTROL_SERIAL_BAUD` | `115200` | Serial baud rate |
| `poll_interval` / `FAN_CONTROL_POLL_INTERVAL` | `5` | Seconds between temp reads |
| `api_port` / `FAN_CONTROL_API_PORT` | `9780` | HTTP API port |
| `api_host` / `FAN_CONTROL_API_HOST` | `0.0.0.0` | API bind address |
| `api_key` / `FAN_CONTROL_API_KEY` | *(empty)* | Bearer token / X-API-Key (empty = no auth) |
| `log_level` / `FAN_CONTROL_LOG_LEVEL` | `INFO` | Logging level |
| `serial_timeout` | `2` | Seconds to wait for ACK |
| `serial_retries` | `3` | Retry count on failed send |
| `reconnect_interval` | `5` | Seconds between reconnection attempts |
| `temp_sensors` | `{"cpu": "auto"}` | Temperature sensor mappings — see [Multi-Sensor Temperatures](#multi-sensor-temperatures) |

## Fan Curve

Default piecewise-linear curve (configured in `firmware/config.py`):

| CPU Temp | Fan Duty |
|----------|----------|
| ≤ 30°C | 25% |
| 50°C | 50% |
| 70°C | 80% |
| ≥ 85°C | 100% |

Temperatures between points are linearly interpolated. Minimum duty is 20% (to prevent fan stall).

## PWM Noise Mitigation

The firmware includes two mechanisms to reduce inductor whine from the host USB 5V regulator:

### Phase Offset

PWM channels are staggered evenly across the switching period using RP2040 hardware slice counter registers. With 4 channels, each is offset by 90° — so current draw is spread across the period rather than all channels switching simultaneously. This halves (or better) the peak transient on the USB VBUS rail.

### Duty Ramping

Duty cycle changes are applied gradually (`DUTY_RAMP_STEP` percent per main loop tick) rather than instantaneously. At default settings (`DUTY_RAMP_STEP=2`, `LOOP_SLEEP_MS=50`), a full 0→100% ramp takes ~2.5 seconds. This eliminates the large transient step that occurs when the fan curve jumps between points.

> **Note**: Failsafe (watchdog timeout) bypasses ramping — fans go to 100% immediately.

| Setting | Default | Description |
|---------|---------|-------------|
| `PWM_CHANNELS` | `4` | Number of active PWM outputs (1-4) |
| `DUTY_RAMP_ENABLED` | `True` | Enable/disable smooth duty transitions |
| `DUTY_RAMP_STEP` | `2` | Max duty % change per main loop tick |

## Safety

- **Boot**: Fans start at **100%** until first valid temperature is received
- **Watchdog**: If no message received for **5 seconds**, fans ramp to **100%**
- **Reconnection**: Normal operation resumes automatically when communication is restored

## HTTP API

All endpoints return JSON. Authenticate with `Authorization: Bearer <key>` or `X-API-Key: <key>`.

### `GET /api/status`

Returns current system state.

```bash
curl -H 'X-API-Key: YOUR_KEY' http://localhost:9780/api/status
```

```json
{
  "cpu_temp": 52.0,
  "mode": "auto",
  "temps": {
    "cpu": 52.0
  },
  "controller": {
    "fans": [50, 50, 50, 50],
    "modes": ["auto", "auto", "auto", "auto"],
    "mode": "auto",
    "channels": 4,
    "last_temp": 52.0,
    "watchdog": { "triggered": false, "since_feed": 2.1 }
  },
  "serial": { "connected": true, "port": "/dev/ttyACM0", "last_error": null },
  "service": { "uptime_s": 3621.3, "poll_interval": 5, "loops": 724 }
}
```

With tach enabled, additional sensors, and per-channel override:

```json
{
  "cpu_temp": 52.0,
  "mode": "override",
  "temps": {
    "cpu": 52.0,
    "nvme": 40.8,
    "chipset": 55.0
  },
  "controller": {
    "fans": [50, 50, 75, 50],
    "modes": ["auto", "auto", "override", "auto"],
    "mode": "override",
    "rpm": [2400, 2400, 3100, 2400],
    "stall": [false, false, false, false],
    "any_stalled": false,
    "channels": 4,
    "last_temp": 52.0,
    "watchdog": { "triggered": false, "since_feed": 2.1 }
  },
  "serial": { "connected": true, "port": "/dev/ttyACM0", "last_error": null },
  "service": { "uptime_s": 3621.3, "poll_interval": 5, "loops": 724 }
}
```

### `GET /api/health`

Simple health check (returns 200 if healthy, 503 if degraded).

### `POST /api/override`

Set manual fan speed (overrides auto curve). Optionally target a single channel.

```bash
# All channels
curl -X POST -H 'X-API-Key: YOUR_KEY' \
     -H 'Content-Type: application/json' \
     -d '{"percent": 75}' \
     http://localhost:9780/api/override

# Single channel
curl -X POST -H 'X-API-Key: YOUR_KEY' \
     -H 'Content-Type: application/json' \
     -d '{"percent": 75, "channel": 2}' \
     http://localhost:9780/api/override
```

### `POST /api/auto`

Return to automatic fan curve mode. Optionally target a single channel.

```bash
# All channels
curl -X POST -H 'X-API-Key: YOUR_KEY' http://localhost:9780/api/auto

# Single channel
curl -X POST -H 'X-API-Key: YOUR_KEY' \
     -H 'Content-Type: application/json' \
     -d '{"channel": 2}' \
     http://localhost:9780/api/auto
```

## Serial Protocol

JSON-over-serial with SYN/ACK/SYN-ACK handshake:

```
Host → RP2040:  {"seq":1, "type":"SYN", "cmd":"SET_TEMP", "payload":{"cpu":62.0}}
RP2040 → Host:  {"seq":1, "type":"ACK", "status":"ok", "payload":{"fans":[50,50,50,50],"mode":"auto"}}
Host → RP2040:  {"seq":1, "type":"SYN-ACK"}
```

Commands: `SET_TEMP`, `GET_STATUS`, `SET_OVERRIDE`, `SET_AUTO`, `PING`

`SET_OVERRIDE` and `SET_AUTO` accept an optional `"channel"` key in the payload (integer 0–N or `"all"`).

## HomeAssistant Integration

Add a [RESTful sensor](https://www.home-assistant.io/integrations/rest/) to `configuration.yaml`:

```yaml
sensor:
  - platform: rest
    name: "P330 Fan Control"
    resource: http://PROXMOX_IP:9780/api/status
    headers:
      X-API-Key: "YOUR_KEY"
    value_template: "{{ value_json.cpu_temp if value_json.cpu_temp is number else None }}"
    unit_of_measurement: "°C"
    json_attributes:
      - temps
      - controller
      - serial
      - service
    scan_interval: 30
```

## Multi-Sensor Temperatures

By default, only CPU temperature is monitored (`"cpu": "auto"`). To add additional sensors, configure `temp_sensors` in your config.json with chip and sensor glob patterns:

```json
"temp_sensors": {
    "cpu": "auto",
    "nvme": {"chip": "nvme-pci-0400", "sensor": "Composite"},
    "chipset": {"chip": "nct6687*", "sensor": "PCH CHIP"},
    "coolant": {"chip": "waterforce*", "sensor": "Coolant temp"}
}
```

To find your available sensors, run `sensors -j` and identify the chip name (top-level key) and sensor label for each temperature you want to monitor. Glob patterns (`*`, `?`) are supported for matching.

All configured temperatures are returned in the `temps` object of the `/api/status` response and can be accessed in Homepage via `temps.nvme`, `temps.chipset`, etc.

## Tachometer (Advanced)

Optional fan RPM reading and stall detection via the RP2040. **Disabled by default.**

### Enabling

1. Wire fan tach signals to the RP2040 (GPIO 1, 3, 5, 7 — see [Wiring](#wiring))
2. Edit `firmware/config.py` on the RP2040:
   ```python
   TACH_ENABLED = True
   ```
3. Re-flash the firmware

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `TACH_ENABLED` | `False` | Enable/disable RPM reading |
| `TACH_PINS` | `[1, 3, 5, 7]` | GPIO input pins for tach signals |
| `TACH_PULSES_PER_REV` | `2` | Pulses per revolution (2 for most fans including Noctua) |
| `TACH_SAMPLE_MS` | `1000` | RPM measurement window in ms |
| `TACH_STALL_THRESHOLD` | `2` | Consecutive zero-RPM samples before stall flag is set |

### Stall Detection

A fan is flagged as stalled when its RPM reads 0 for `TACH_STALL_THRESHOLD` consecutive samples while the duty cycle is above `MIN_DUTY`. The `any_stalled` field provides a single boolean for easy alerting.

In Homepage (customapi widget):
```yaml
- field: controller.any_stalled
  label: Fan Stall
  format: text
  remap:
    - value: false
      to: OK
    - value: true
      to: STALL
```

### Hardware Notes

- The tach signal is **open-collector** (Noctua) or **open-drain** on most 4-pin fans
- The RP2040 firmware uses **internal 3.3V pull-ups** — no external components needed
- **Do NOT use external 5V pull-ups** — this will exceed the RP2040 GPIO voltage limit and cause damage
- Some fans may use 1 or 4 pulses per revolution instead of 2 — adjust `TACH_PULSES_PER_REV` accordingly

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `sensors` not found | `apt install lm-sensors && sensors-detect` |
| Serial port not found | Check `ls /dev/ttyACM*`, verify RP2040 is connected |
| Permission denied on serial | Add user to `dialout` group: `usermod -aG dialout root` |
| Fans stuck at 100% | Check watchdog — service may not be running or serial is disconnected |
| API returns 401 | Verify your API key matches config |
| Fans don't spin below 25% | Normal — `MIN_DUTY` is 20% to prevent stalling. Adjust in `firmware/config.py` |
| HA sensor shows JSON/value errors | Verify your API key in HA `configuration.yaml` matches the service config — a wrong key returns `401` and HA cannot parse the response |
| Tach RPM always shows 0 | Check wiring: tach is Pin 3 on the fan connector. Verify `TACH_ENABLED = True` in firmware config. Some fans need external pull-ups to 3.3V |
| Mapped sensor returns null | Run `sensors -j` and verify the chip name and sensor label match your `temp_sensors` config patterns |

## Project Structure

```
lenovo-fan-control/
├── firmware/                    # RP2040 MicroPython
│   ├── main.py                  # Entry point
│   ├── fan_controller.py        # PWM + fan curve
│   ├── serial_handler.py        # USB serial protocol
│   ├── config.py                # Pin assignments, curve, timeouts
│   └── watchdog.py              # Keepalive safety fallback
├── host/                        # Proxmox host service
│   ├── fan_control_service.py   # Main daemon (Config, TempReader, SerialProtocol, Service)
│   ├── api_server.py            # HTTP API server
│   ├── config.json.example      # Example configuration
│   └── requirements.txt         # Python dependencies
├── systemd/
│   ├── fan-control.service      # systemd unit file
│   └── fan-control.env          # Environment variable defaults
├── install.sh                   # Host installation script
└── README.md                    # This file
```
