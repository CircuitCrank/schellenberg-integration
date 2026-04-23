# Schellenberg — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.11%2B-blue)](https://www.home-assistant.io/)

A fully local Home Assistant integration for Schellenberg roller shutters! Designed to replace shell scripts, YAML hacks, and other workarounds with a clean, native implementation that actually tracks shutter positions.

<img src="logo.png" alt="Schellenberg Integration" width="200"/>

## Why this exists

Earlier setups relied on shell commands and YAML workarounds to control Schellenberg shutters. They work, until they don't. No real state handling, no UI integration, no reliable position tracking.

This integration replaces all of that with a clean, fully native Home Assistant implementation.

## Features

- Fully UI-based configuration (no YAML required)
- Direct USB control of Schellenberg roller shutters
- Native Home Assistant cover entities (open, close, stop, set position)
- Position tracking with calibration-based runtime measurement
- Guided calibration wizard per shutter
- Persistent state across restarts
- Optional control of all shutters at once
- Support for physical remote signal syncing

## Requirements

**Hardware**

- Schellenberg Smart Home Funk-Stick — [schellenberg-shop.de](https://schellenberg-shop.de/smart-home-funk-stick/21009)
- Compatible roller shutter motors (e.g. RolloDrive 65/75 Premium series)

**Software**

- Home Assistant 2024.11 or newer

## Installation

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=CircuitCrank&repository=schellenberg-integration&category=integration)

### HACS (recommended)

1. Open HACS in Home Assistant
2. Add custom repository: `https://github.com/CircuitCrank/schellenberg-integration`
3. Select category: Integration
4. Install "Schellenberg Integration"
5. Restart Home Assistant

### Manual

Copy `custom_components/schellenberg_integration/` into your Home Assistant config directory and restart.

## Setup

**1. Add Integration**

Go to **Settings → Devices & Services → Add Integration** and search for **Schellenberg Integration**. Enter a name, select the USB device from the dropdown, and set a send repeat count.

**2. Add Shutters**

Click the **`+`** button in the integration, select **Roller Shutter**, and follow the guided setup — pair the shutter if needed, then run the calibration wizard to measure full travel time in both directions.

**3. All-Shutters Control (optional)**

Click the **`+`** button and select **All-Shutters Control** to create a control entity that broadcasts to all shutters on a dedicated channel.

**4. Remote Control (optional)**

Click the **`+`** button and select **Remote Control** to add a sensor that listens for incoming signals from physical Schellenberg remotes and keeps shutter positions in sync.

All settings can be changed later via the **⚙** icon next to the respective entry.

## Notes

- This is early-stage software (v0.1.x). Breaking changes may occur.
- Calibration is required for accurate position tracking.
- USB passthrough must be correctly configured in virtualized environments (e.g. Proxmox USB passthrough, not just the host path).

## Troubleshooting

**USB stick not detected**

Check that the stick is physically connected. Run `ls /dev/serial/by-id/` in the HA terminal to verify the device is visible to the OS. In virtualized environments, ensure the USB device is passed through to the VM, not just the host.

**USB stick disconnects intermittently**

Usually caused by insufficient power delivery. Use an externally powered USB hub.

**Shutter does not respond**

Verify the channel number matches the paired shutter and re-run pairing if needed. If pairing is correct, check USB stick placement (it should be centrally located and not obstructed by metal surfaces).

**Position is inaccurate**

Re-run the calibration wizard for the affected shutter.

**Remote signals not received**

Ensure the remote is within range and check the HA logs for incoming raw signals.

## Credits

USB protocol implementation based on [Hypfer/schellenberg-qivicon-usb](https://github.com/Hypfer/schellenberg-qivicon-usb).

## License

MIT License — see [LICENSE](LICENSE)