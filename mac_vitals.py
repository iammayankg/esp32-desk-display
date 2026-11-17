#!/usr/bin/env python3
"""Send this Mac's CPU, memory and network activity to the ESP32 desk display over USB.

Usage:  python3 mac_vitals.py [-v] [--port /dev/cu.usbmodemXXXX]

Uses only the Python standard library and built-in macOS tools. It finds the
board by itself and reconnects if the board is reset or re-plugged. Stop it
(Ctrl-C) before using mpremote or esptool on the board: they need the port.

It also sends this Mac's local time every 30 seconds; the board uses it to set
its clock only when its own Wi-Fi time sync is not working.

Line formats:
  V <cpu%> <mem%> <battery% or -1> <charging 0/1> <down bytes/s> <up bytes/s>
  T <year> <month> <day> <hour> <minute> <second>
  S <unread count, -1 = Slack not open> <mention 0/1>

The S line comes from the title of a Slack tab open in Safari (macOS asks once
for permission to control Safari). With no Slack tab it falls back to the Dock
badge of the Slack desktop app, which needs Accessibility permission for the
app running this script. With neither, the board shows no badge.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time
import tty

INTERVAL_S = 2


def run(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


TOTAL_MEM = int(run("sysctl", "-n", "hw.memsize"))


def memory_percent():
    # Active + wired + compressed pages: what Activity Monitor calls "Memory Used",
    # minus cached files.
    out = run("vm_stat")
    page = int(re.search(r"page size of (\d+) bytes", out).group(1))

    def pages(name):
        m = re.search(r"%s:\s+(\d+)" % re.escape(name), out)
        return int(m.group(1)) if m else 0

    used = pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")
    return round(100 * used * page / TOTAL_MEM)


def network_bytes():
    # Total bytes in/out across physical (en*) interfaces.
    rx = tx = 0
    for line in run("netstat", "-ibn").splitlines():
        f = line.split()
        if len(f) >= 10 and f[0].startswith("en") and f[2].startswith("<Link#"):
            try:
                rx += int(f[-5])
                tx += int(f[-2])
            except ValueError:
                pass
    return rx, tx


def battery():
    out = run("pmset", "-g", "batt")
    m = re.search(r"(\d+)%", out)
    if not m:
        return -1, 0                      # desktop Mac: no battery
    return int(m.group(1)), int("AC Power" in out)


SLACK_SCRIPT = '''
if application "Safari" is running then
    tell application "Safari"
        set out to ""
        repeat with w in windows
            repeat with t in tabs of w
                try
                    if URL of t contains "slack.com" then set out to out & (URL of t) & tab & (name of t) & linefeed
                end try
            end repeat
        end repeat
        return out
    end tell
end if
'''


def slack_title_state(title):
    # (unread count, mention) from one Slack tab title. Slack marks unread activity
    # as "... - 3 new items - Slack", or with a leading "*" (unread) or "!" (mention).
    title = title.strip()
    m = re.search(r"(\d+) new items?\b", title)
    count = int(m.group(1)) if m else 0
    mention = title.startswith("!")
    if count == 0 and title[:1] in ("*", "!"):
        count = 1
    return count, int(mention)


def slack_state(lines):
    # Combine all Slack tabs; (-1, 0) when there is none.
    count, mention, found = 0, 0, False
    for line in lines:
        url, _, title = line.partition("\t")
        host = re.sub(r"^https?://([^/:]+).*", r"\1", url.strip())
        if host != "slack.com" and not host.endswith(".slack.com"):
            continue
        found = True
        c, m = slack_title_state(title)
        count, mention = count + c, mention | m
    return (count, mention) if found else (-1, 0)


DOCK_SCRIPT = '''
tell application "System Events" to tell process "Dock"
    return value of attribute "AXStatusLabel" of UI element "Slack" of list 1
end tell
'''


def osascript(script):
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=5).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def badge_state(label):
    # (unread count, mention) from the Slack app's Dock badge: a number means
    # mentions or direct messages, a dot means other unread messages.
    label = label.strip()
    if label in ("", "missing value"):
        return 0, 0
    m = re.match(r"\d+", label)
    if m:
        return int(m.group()), 1
    return 1, 0


def slack_app():
    # Dock badge of the Slack desktop app; (-1, 0) if it is not running or the
    # badge cannot be read.
    if subprocess.run(["pgrep", "-xq", "Slack"]).returncode != 0:
        return -1, 0
    # Older macOS exposes the badge here without any permission.
    m = re.search(r'"label"="([^"]*)"', run("lsappinfo", "info", "-only", "StatusLabel", "-app", "Slack"))
    if m:
        return badge_state(m.group(1))
    # Otherwise ask the Dock, which needs Accessibility permission for the app
    # running this script (System Settings > Privacy & Security > Accessibility).
    out = osascript(DOCK_SCRIPT)
    if not out.strip():
        return -1, 0                      # no permission, or Slack is not in the Dock
    return badge_state(out)


def slack():
    # A Slack tab in Safari wins; with none open, fall back to the desktop app.
    state = slack_state(osascript(SLACK_SCRIPT).splitlines())
    return state if state[0] >= 0 else slack_app()


def open_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    return fd


def find_port(preferred):
    if preferred:
        return preferred if os.path.exists(preferred) else None
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port (default: first /dev/cu.usbmodem*)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each sample and anything the board says")
    args = ap.parse_args()

    # top streams one header block per sample; we only use its CPU line.
    top = subprocess.Popen(["top", "-l", "0", "-n", "0", "-s", str(INTERVAL_S)],
                           stdout=subprocess.PIPE, text=True, bufsize=1)
    fd = None
    last_net = None
    last_time = None
    bat = (-1, 0)
    bat_checked = 0
    time_sent = 0
    slack_sent = 0
    first = True
    try:
        for line in top.stdout:
            m = re.match(r"CPU usage: ([\d.]+)% user, ([\d.]+)% sys", line)
            if not m:
                continue
            if first:                     # top's first sample is an average since boot
                first = False
                last_net, last_time = network_bytes(), time.monotonic()
                continue
            cpu = round(float(m.group(1)) + float(m.group(2)))

            now = time.monotonic()
            net = network_bytes()
            dt = max(now - last_time, 0.001)
            down = max(0, int((net[0] - last_net[0]) / dt))
            up = max(0, int((net[1] - last_net[1]) / dt))
            last_net, last_time = net, now

            if now - bat_checked > 30:
                bat, bat_checked = battery(), now

            msg = "V %d %d %d %d %d %d\n" % (cpu, memory_percent(), bat[0], bat[1], down, up)

            if fd is None:
                path = find_port(args.port)
                if path:
                    try:
                        fd = open_port(path)
                        time_sent = slack_sent = 0   # send time and Slack state straight away
                        print("connected to", path, flush=True)
                    except OSError as e:
                        if args.verbose:
                            print("cannot open %s: %s" % (path, e), flush=True)
                elif args.verbose:
                    print("board not found, waiting", flush=True)
            if fd is None:
                continue
            if now - time_sent > 30:
                # The leading newline ends any partial line the board is holding.
                msg = time.strftime("\nT %Y %m %d %H %M %S\n") + msg
                time_sent = now
            if now - slack_sent > 6:
                msg += "S %d %d\n" % slack()
                slack_sent = now
            try:
                os.write(fd, msg.encode())
                try:
                    said = os.read(fd, 4096)  # drain the board's output
                except BlockingIOError:
                    said = b""
                if args.verbose:
                    print(msg.strip(), flush=True)
                    if said.strip():
                        print("  board:", said.decode(errors="replace").strip(), flush=True)
            except BlockingIOError:
                pass                      # board busy; skip this sample
            except OSError:
                print("board disconnected, waiting", flush=True)
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
    except KeyboardInterrupt:
        pass
    finally:
        top.terminate()
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
