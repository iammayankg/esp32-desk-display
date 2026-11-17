# ESP32 desk display

A small desk display built on the Spotpear ESP32-S3-0.96-SD board (ESP32-S3 with
a 160x80 colour LCD), written in MicroPython. It shows the time, your next
calendar events, a pomodoro timer and your Mac's vitals.

## Screens

| Screen | What it shows |
| --- | --- |
| Clock | HH:MM and the date over a "digital rain" backdrop, with a Slack unread badge in the corner |
| Agenda | The event in progress, large; otherwise the next two events |
| Pomodoro | 25-minute focus / 5-minute break timer, with a 15-minute break after every fourth round |
| Vitals | The Mac's CPU, memory and network activity, with a CPU history graph |

The clock and agenda alternate every 5 seconds. The BOOT button does the rest:

- **Short press:** clock/agenda → pomodoro → vitals → clock.
- **Long press** on the pomodoro: start or pause. Hold 2.5 seconds to reset.
- **Long press** on the clock or agenda: sync time and calendar now.

## How it gets its data

- **Time:** NTP over Wi-Fi. If Wi-Fi sync fails, it takes the time from the Mac over USB.
- **Calendar:** a private iCal link (for example Google Calendar's "secret address"),
  downloaded over Wi-Fi every 15 minutes. Repeating events and time zones are handled.
- **Mac vitals and Slack:** `mac_vitals.py` runs on the Mac and sends them over the USB cable.

## Files

| File | Runs on | Purpose |
| --- | --- | --- |
| `main.py` | Board | Display driver, screens, button, Wi-Fi sync |
| `agenda.py` | Board | Calendar download and parsing |
| `wifi_config.example.py` | Board | Template for your settings |
| `mac_vitals.py` | Mac | Sends vitals, time and Slack state to the board |

## Setup

You need the board flashed with MicroPython for the ESP32-S3 (built with v1.25.0,
the `ESP32_GENERIC_S3` 4 MB flash build) and
[mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) on the Mac.

1. Copy `wifi_config.example.py` to `wifi_config.py` and fill in your Wi-Fi name,
   password, time zone and, if you want the agenda, your calendar's iCal link.
   The board only supports 2.4 GHz Wi-Fi. `wifi_config.py` is git-ignored; keep it that way.
2. Copy the files to the board and restart it:

   ```bash
   mpremote cp main.py agenda.py wifi_config.py :
   ```

   ```bash
   mpremote reset
   ```

3. For the vitals screen, Slack badge and USB time fallback, run this on the Mac
   and leave it running:

   ```bash
   python3 mac_vitals.py
   ```

   It uses only the Python standard library, finds the board by itself and
   reconnects after a reset or re-plug. Add `-v` to see what it sends.

`mac_vitals.py` holds the serial port, so stop it before using `mpremote` and
start it again afterwards.

## Slack badge

The badge shows the unread count: yellow for unread messages, red for mentions.
`mac_vitals.py` reads it from one of two places:

1. The title of a Slack tab open in Safari. macOS asks once for permission to
   control Safari.
2. If no Slack tab is open, the Dock badge of the Slack desktop app. This needs
   Accessibility permission for the app running the script (System Settings >
   Privacy & Security > Accessibility).

Neither route has been tested against a live Slack session yet, so the parsing
may need adjusting.

## Hardware notes

- LCD (ST7735S): MOSI 4, SCLK 5, CS 8, DC 6, RST 7, backlight on GPIO3.
- Button: BOOT on GPIO0. The second button is a hardware reset.
- The SD card slot is not used.
