# ESP32 desk display

A desk display on a Spotpear ESP32-S3-0.96-SD board (ESP32-S3, 4 MB flash, 160x80
ST7735S LCD, one usable button) running MicroPython v1.25.0, plus a Mac script
that feeds it over USB.

## Files

- `main.py` - board firmware: display driver, screens, button, Wi-Fi sync, main loop.
- `agenda.py` - iCal download and parsing (recurrence, time zones). Runs on both
  MicroPython and CPython, so it can be tested on the Mac.
- `mac_vitals.py` - runs on the Mac, standard library only. Sends vitals, time and
  Slack unread state to the board.
- `wifi_config.example.py` - template for `wifi_config.py`.
- `wifi_config.py` - git-ignored. Holds the Wi-Fi password and the secret calendar
  link. **Never read, print or commit it.** The user edits it themselves; only
  copy it to the board unread.

## Hardware

- LCD: SPI1 at 40 MHz, MOSI 4, SCLK 5, CS 8, DC 6, RST 7, backlight PWM on GPIO3.
  Offsets X=1, Y=26, inversion on, RGB565 with byte-swapped colours.
- Button: BOOT on GPIO0, active low. The other button is a hardware reset: it
  wipes the clock and drops USB.
- SD card (unused): CS 11, MOSI 12, CLK 13, MISO 14.

## Screens

Clock and agenda alternate every `ROTATE_S` seconds (agenda only when it has
events). A short press steps clock/agenda -> pomodoro -> vitals.

- Clock: HH:MM and the date over a digital-rain backdrop; Slack badge top right.
- Agenda: the event in progress, large; otherwise the next two events.
- Pomodoro: 25/5 timer; long press starts/pauses, 2.5 s hold resets.
- Vitals: the Mac's CPU, memory and network.

## The face

A pair of eyes with a mood (`face_mood()`: stressed, slack, focus, break, sleepy,
idle). It appears three ways, all in the "The face" section of `main.py`:

- `react(mood, caption, ms, line2)` pops the full face over any screen for a
  moment. Triggers live where the event happens (`handle_line`, `note_wifi`,
  `announce_event`, the main loop).
- `companion_spot()` places a 24x12 pair of small eyes on the agenda, pomodoro
  and vitals screens. `show()` paints them into every full frame;
  `companion_frame()` animates them with a partial refresh (`show_region`).
  Keep that box clear when changing those layouts.
- The clock's colon blinks and takes the mood's colour (`draw_clock`).

## Design rules

- One cooperative main loop with a 10 ms sleep. Nothing may block: Wi-Fi, NTP and
  the calendar download run as small steps (`sync_step`, `fetch_step`).
- The RTC always holds local time. Times are local seconds since 2000-01-01.
- Wi-Fi is switched off between syncs.
- Time comes from NTP; the Mac's time is used only while Wi-Fi sync is failing
  (`wifi_time_ok`).

## Serial protocol (Mac -> board, one line each)

```
V <cpu%> <mem%> <battery% or -1> <charging 0/1> <down bytes/s> <up bytes/s>
T <year> <month> <day> <hour> <minute> <second>        Mac local time
S <unread count, -1 = Slack not open> <mention 0/1>
```

New line types need an entry in `LINE_FIELDS` in `main.py`. Slack state comes
from a Safari tab title, falling back to the Slack app's Dock badge (needs
Accessibility permission).

## Deploying and testing

`mac_vitals.py` holds the serial port. Stop it before using `mpremote` or
`esptool`, and restart it afterwards.

```bash
pkill -INT -f mac_vitals.py
mpremote connect /dev/cu.usbmodem* cp main.py :main.py   # same for agenda.py
mpremote connect /dev/cu.usbmodem* reset
python3 mac_vitals.py -v
```

- The port name changes between plugs; always glob `/dev/cu.usbmodem*`.
- `main.py` only starts its loop under `if __name__ == "__main__"`, so a test
  script can `import main as m` and call functions directly, run on the board
  with `mpremote run test.py`.
- `mpremote ... reset` drops USB and may print a harmless traceback.
- Verify changes on the board, not just by reading the code.
