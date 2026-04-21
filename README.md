# Schellenberg — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.11%2B-blue)](https://www.home-assistant.io/)

**Early development (v0.1.x)** – Breaking changes possible. Use at your own risk.

![Schellenberg Integration](logo.png)

Control Schellenberg roller shutters via USB stick directly from Home Assistant, fully UI-configurable, no YAML required.

## Features

- **USB stick control:** sends commands directly to shutters via the Schellenberg Smart Home Funk-Stick
- **Cover entities:** open, close, stop, set position (slider 0–100)
- **Position tracking:** calibration-based, deterministic, persistent across restarts
- **Calibration wizard:** guided setup per shutter, reconfigurable at any time
- **All-shutters control:** optional dedicated channel to control all shutters simultaneously
- **Remote support:** receive signals from physical Schellenberg remotes and sync shutter positions accordingly
- **Fully UI-based:** setup and reconfiguration via Home Assistant config flow, no YAML

## Requirements

**Hardware**

- Schellenberg Smart Home Funk-Stick — [schellenberg-shop.de](https://schellenberg-shop.de/smart-home-funk-stick/21009)
- Compatible shutters: RolloDrive 65 Premium, RolloDrive 75 Premium, Funk-Rollladenmotoren Premium (2020 generation)

**Software**

- Home Assistant 2024.11 or newer

## Installation

[![Add to Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=CircuitCrank&repository=schellenberg-integration&category=integration)

**Via HACS (recommended)**

1. Open HACS in Home Assistant
2. Go to **Integrations** → three-dot menu → **Custom repositories**
3. Add `https://github.com/CircuitCrank/schellenberg-integration` — Category: **Integration**
4. Search for **Schellenberg USB** and install it
5. Restart Home Assistant

**Manual**

1. Copy the `custom_components/schellenberg_integration/` folder into your HA config directory
2. Restart Home Assistant

## Setup

**1. Add Integration**

Go to **Settings → Devices & Services → Add Integration** and search for **Schellenberg Integration**. You will be asked to enter a name, choose the USB device from the dropdown and a send repeat count.

The USB-Stick will be added as a device with a "USB Raw" sensor entity.

**2. Add Shutters**

Go to **Settings → Devices & Services → Integrations → Schellenberg Integration** and click the **`+`** button (top right).
Select type **Roller Shutter** and follow the guided setup — pair the shutter if needed, then run the calibration wizard which measures the full travel time in both directions.

**3. All-Shutters Control (optional)**

Go to **Settings → Devices & Services → Integrations → Schellenberg Integration** and click the **`+`** button (top right).
Select type **All-Shutters Control** to create a control entity that broadcasts to all shutters on a dedicated channel.

**4. Remote Control (optional)**

Go to **Settings → Devices & Services → Integrations → Schellenberg Integration** and click the **`+`** button (top right).
Select type **Remote Control** to add a sensor that listens for incoming signals from physical Schellenberg remotes and keeps the shutter positions in Home Assistant in sync.

## Reconfiguration

All settings can be changed later via **Settings → Devices & Services → Integrations → Schellenberg Integration**. Click the **⚙** icon next to the entry you want to reconfigure.

## Troubleshooting

**USB stick not detected**

If it is not found, check the following:

- Verify the stick is physically connected
- If Home Assistant runs in a VM, ensure the USB device is passed through correctly (e.g. in Proxmox via USB passthrough, not just the host path)
- Run `ls /dev/serial/by-id/` in the HA terminal to confirm the device is visible to the OS

**USB stick disconnects intermittently**

This is often caused by insufficient power delivery from the host USB port. Using an externally powered USB hub is recommended.

**Shutter does not respond**

Verify the channel number matches the paired shutter and re-run pairing if needed.

**Position is inaccurate after movement**

Re-run the calibration wizard for the affected shutter.

**Remote signals not received**

Ensure the remote is within range and check the HA logs for incoming raw signals from the integration.

## Credits

USB protocol implementation based on [Hypfer/schellenberg-qivicon-usb](https://github.com/Hypfer/schellenberg-qivicon-usb).

## License

MIT License — see [LICENSE](LICENSE)
