# Desk display for the Spotpear ESP32-S3-0.96-SD board (160x80 ST7735S LCD), MicroPython.
# Pins and init sequence are taken from Arduino/example/SD_Display/Video_demo/Display_ST7789.*
#
# Screens. The clock and agenda alternate by themselves every few seconds; a
# short press of the BOOT button (GPIO0) steps clock/agenda -> pomodoro -> vitals.
#   clock     - HH:MM and "Mon 16 Nov" over a dim digital-rain backdrop; time from
#               NTP over Wi-Fi (wifi_config.py), or from the Mac over USB
#               (mac_vitals.py) whenever the Wi-Fi sync is not working
#   agenda    - the event in progress, large; otherwise the next two events, from
#               the iCal link in wifi_config.CALENDAR_URL
#   pomodoro  - 25/5 timer; hold the button to start/pause, hold 2.5 s to reset
#   vitals    - CPU / memory / network of the Mac, sent over USB by mac_vitals.py
# The RTC always holds local time.
import gc
import sys
import time
import random
import select
import framebuf
import network
import ntptime
from machine import Pin, SPI, PWM, RTC

import agenda

try:
    import wifi_config as cfg
    if not cfg.SSID or cfg.SSID == "YOUR_WIFI_NAME":
        cfg = None
except ImportError:
    cfg = None

CAL_URL = getattr(cfg, "CALENDAR_URL", "") if cfg else ""
CAL_DAYS = 7             # how far ahead the agenda looks
AGENDA_EVENTS = 2        # events shown on the agenda screen
ROTATE_S = 5             # clock and agenda swap this often
RESYNC_S = 900 if CAL_URL else 3600   # how often to go online
FETCH_TIMEOUT_S = 120
RETRY_S = 60             # wait between failed attempts
CONNECT_TIMEOUT_S = 20

W, H = 160, 80
OFF_X, OFF_Y = 1, 26
PIN_MOSI, PIN_SCLK, PIN_CS, PIN_DC, PIN_RST, PIN_BL = 4, 5, 8, 6, 7, 3
PIN_BUTTON = 0

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def rgb(r, g, b):
    # The panel wants big-endian RGB565; framebuf stores little-endian, so swap bytes.
    c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return ((c & 0xFF) << 8) | (c >> 8)


BLACK = rgb(0, 0, 0)
FG = rgb(0, 255, 160)
DIM = rgb(0, 40, 26)
TEXT = rgb(200, 200, 200)
WARN = rgb(255, 80, 60)
STATUS = rgb(110, 110, 110)
FOCUS = rgb(255, 90, 60)
FOCUS_DIM = rgb(48, 14, 8)
BREAK = rgb(70, 190, 255)
BREAK_DIM = rgb(10, 30, 46)
BAR_BG = rgb(40, 40, 40)
AMBER = rgb(255, 190, 40)
SLACK = rgb(236, 178, 46)

spi = SPI(1, baudrate=40_000_000, polarity=0, phase=0,
          sck=Pin(PIN_SCLK), mosi=Pin(PIN_MOSI))
cs = Pin(PIN_CS, Pin.OUT, value=1)
dc = Pin(PIN_DC, Pin.OUT, value=0)
rst = Pin(PIN_RST, Pin.OUT, value=1)
backlight = PWM(Pin(PIN_BL), freq=1000, duty_u16=65535)  # active high
button = Pin(PIN_BUTTON, Pin.IN, Pin.PULL_UP)            # low when pressed

buf = bytearray(W * H * 2)
fb = framebuf.FrameBuffer(buf, W, H, framebuf.RGB565)


def cmd(c, data=None):
    cs(0)
    dc(0)
    spi.write(bytes([c]))
    if data is not None:
        dc(1)
        spi.write(bytes(data))
    cs(1)


def lcd_init():
    rst(0)
    time.sleep_ms(50)
    rst(1)
    time.sleep_ms(50)
    cmd(0x11)
    time.sleep_ms(120)
    cmd(0x36, [0xA8])                       # landscape, BGR
    cmd(0xB1, [0x01, 0x2C, 0x2D])
    cmd(0xB2, [0x01, 0x2C, 0x2D])
    cmd(0xB3, [0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D])
    cmd(0xB4, [0x07])
    cmd(0x21)                               # inversion on
    cmd(0xC0, [0xA2, 0x02, 0x84])
    cmd(0xC1, [0xC5])
    cmd(0xC2, [0x0A, 0x00])
    cmd(0xC3, [0x8A, 0x2A])
    cmd(0xC4, [0x8A, 0xEE])
    cmd(0xC5, [0x0E])
    cmd(0xE0, [0x0F, 0x1A, 0x0F, 0x18, 0x2F, 0x28, 0x20, 0x22,
               0x1F, 0x1B, 0x23, 0x37, 0x00, 0x07, 0x02, 0x10])
    cmd(0xE1, [0x0F, 0x1B, 0x0F, 0x17, 0x33, 0x2C, 0x29, 0x2E,
               0x30, 0x30, 0x39, 0x3F, 0x00, 0x07, 0x03, 0x10])
    cmd(0x3A, [0x05])                       # 16-bit colour
    cmd(0x29)


def show():
    cmd(0x2A, [0, OFF_X, 0, OFF_X + W - 1])
    cmd(0x2B, [0, OFF_Y, 0, OFF_Y + H - 1])
    cmd(0x2C)
    cs(0)
    dc(1)
    spi.write(buf)
    cs(1)


# Seven-segment digits: segments a..g as bits 0..6.
SEGS = (0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F)
DW, DH, T = 18, 40, 4   # digit width, height, stroke thickness


def digit(x, y, n, on=FG, off=DIM, w=DW, h=DH, t=T, outline=False):
    # off=None leaves unlit segments undrawn; outline puts a black edge round lit ones.
    m = SEGS[n]
    half = (h - 3 * t) // 2
    parts = (
        (x + t, y, w - 2 * t, t),                        # a
        (x + w - t, y + t, t, half),                     # b
        (x + w - t, y + 2 * t + half, t, half),          # c
        (x + t, y + h - t, w - 2 * t, t),                # d
        (x, y + 2 * t + half, t, half),                  # e
        (x, y + t, t, half),                             # f
        (x + t, y + t + half, w - 2 * t, t),             # g
    )
    if outline:
        for i, (px, py, pw, ph) in enumerate(parts):
            if m & (1 << i):
                fb.fill_rect(px - 2, py - 2, pw + 4, ph + 4, BLACK)
    for i, (px, py, pw, ph) in enumerate(parts):
        if m & (1 << i):
            fb.fill_rect(px, py, pw, ph, on)
        elif off is not None:
            fb.fill_rect(px, py, pw, ph, off)


def colon(x, y, color):
    fb.fill_rect(x + 2, y + 11, T, T, color)
    fb.fill_rect(x + 2, y + 25, T, T, color)


def centered(text, y, color):
    fb.text(text, (W - len(text) * 8) // 2, y, color)


def bar(x, y, w, h, fraction, color):
    fb.fill_rect(x, y, w, h, BAR_BG)
    fraction = min(max(fraction, 0), 1)
    fb.fill_rect(x, y, int(w * fraction + 0.5), h, color)


# ---- Screen: clock ----------------------------------------------------------

CLOCK_FG = rgb(235, 255, 240)
CLOCK_W, CLOCK_H, CLOCK_T = 28, 48, 6          # big digits for HH:MM
LABEL_KEY = rgb(255, 0, 255)                   # transparent colour in the label image
label_buf = bytearray(W * 18 * 2)
label_fb = framebuf.FrameBuffer(label_buf, W, 18, framebuf.RGB565)
label_text = None
label_x = 0


def render_label(text):
    # Draw `text` double-size with a black edge into label_fb (done only when it changes).
    global label_text, label_x
    label_text = text
    scale = 2 if len(text) * 16 <= W else 1
    width = len(text) * 8
    mono = framebuf.FrameBuffer(bytearray(width), width, 8, framebuf.MONO_HLSB)
    mono.text(text, 0, 0, 1)
    label_fb.fill(LABEL_KEY)
    label_x = (W - width * scale) // 2
    for color, grow in ((BLACK, 1), (CLOCK_FG, 0)):
        for y in range(8):
            for x in range(width):
                if mono.pixel(x, y):
                    label_fb.fill_rect(label_x + x * scale - grow, 1 + y * scale - grow,
                                       scale + 2 * grow, scale + 2 * grow, color)


def draw_clock(t):
    year, month, mday, hour, minute, second, wday = t[0:7]
    fb.fill(BLACK)
    rain_paint(CLOCK_RAIN_HEAD, CLOCK_RAIN_SHADES)
    x, y = 9, 3
    step = CLOCK_W + 6
    for n in (hour // 10, hour % 10):
        digit(x, y, n, CLOCK_FG, None, CLOCK_W, CLOCK_H, CLOCK_T, True)
        x += step
    for cy in (y + 12, y + 30):                  # colon
        fb.fill_rect(x - 2, cy - 2, CLOCK_T + 4, CLOCK_T + 4, BLACK)
        fb.fill_rect(x, cy, CLOCK_T, CLOCK_T, CLOCK_FG)
    x += CLOCK_T + 6
    for n in (minute // 10, minute % 10):
        digit(x, y, n, CLOCK_FG, None, CLOCK_W, CLOCK_H, CLOCK_T, True)
        x += step
    text = "TIME NOT SET" if year < 2024 else "%s %d %s" % (DAYS[wday], mday, MONTHS[month - 1])
    if text != label_text:
        render_label(text)
    fb.blit(label_fb, 0, 58, LABEL_KEY)
    unread = slack_unread()
    if unread:                                   # Slack badge, top right corner
        fb.fill_rect(146, 0, 14, 14, BLACK)
        fb.fill_rect(148, 0, 12, 12, WARN if unread[1] else SLACK)
        fb.text(str(unread[0]) if unread[0] < 10 else "+", 150, 2, BLACK)
    show()


# ---- Screen: agenda ---------------------------------------------------------

cal_events = []                     # (start, end, all_day, title), local seconds since 2000
cal_status = "loading" if CAL_URL else "no calendar link"
cal_version = 0


def local_now():
    return agenda.epoch(*time.localtime()[0:6])


def hhmm(t):
    s = t % 86400
    return "%02d:%02d" % (s // 3600, s % 3600 // 60)


def day_label(t, today):
    d = t // 86400
    if d == today:
        return "Today"
    if d == today + 1:
        return "Tomorrow"
    return "%s %d" % (DAYS[agenda.weekday(d)], agenda.civil(d)[2])


def upcoming_events(now):
    return [e for e in cal_events if e[1] > now or (e[0] == e[1] and e[0] >= now)]


def wrap2(title, width):
    # Split a title into two lines of `width` characters, breaking at a space if possible.
    first, rest = title[:width], title[width:2 * width]
    if rest and title[width] != " " and " " in first:
        cut = first.rindex(" ")
        first, rest = title[:cut], title[cut + 1:cut + 1 + width]
    return first, rest.strip()


def big_text(text, x, y, color, scale=2):
    # The built-in 8x8 font, enlarged.
    width = len(text) * 8
    if not width:
        return
    mono = framebuf.FrameBuffer(bytearray(width), width, 8, framebuf.MONO_HLSB)
    mono.text(text, 0, 0, 1)
    for py in range(8):
        for px in range(width):
            if mono.pixel(px, py):
                fb.fill_rect(x + px * scale, y + py * scale, scale, scale, color)


def ongoing_event(now):
    # The timed event happening right now (the most recently started, if several).
    current = None
    for e in cal_events:
        if not e[2] and e[0] <= now < e[1] and (current is None or e[0] >= current[0]):
            current = e
    return current


def draw_ongoing(event, now):
    start, end, all_day, title = event
    fb.text("NOW", 0, 2, AMBER)
    until = hhmm(end)
    if end // 86400 != now // 86400:
        until = "%s %s" % (DAYS[agenda.weekday(end // 86400)], until)
    until = "til " + until
    fb.text(until, W - len(until) * 8, 2, STATUS)
    first, rest = wrap2(title, 10)
    if rest:
        big_text(first, 0, 15, TEXT)
        big_text(rest, 0, 34, TEXT)
    else:
        big_text(first, 0, 24, TEXT)
    bar(0, 56, W, 4, (now - start) / (end - start), AMBER)
    mins = (end - now + 59) // 60
    left = "%dm left" % mins if mins < 60 else "%dh %02dm left" % (mins // 60, mins % 60)
    centered(left, 67, AMBER)


def draw_agenda():
    # A timed event in progress takes the whole screen; otherwise list what is next.
    now = local_now()
    today = now // 86400
    fb.fill(BLACK)
    current = ongoing_event(now)
    if current:
        draw_ongoing(current, now)
        show()
        return
    upcoming = upcoming_events(now)
    if not upcoming:
        centered("AGENDA", 10, TEXT)
        if cal_status == "ok":
            centered("nothing in the", 32, STATUS)
            centered("next %d days" % CAL_DAYS, 44, STATUS)
        else:
            centered(cal_status, 36, WARN if cal_status != "loading" else STATUS)
        show()
        return
    y = 3
    for start, end, all_day, title in upcoming[:AGENDA_EVENTS]:
        soon = ""
        if all_day:
            when = day_label(max(start, now), today) + " all day"
            color = BREAK
        else:
            d = start // 86400
            when = hhmm(start) if d == today else "%s %s" % (
                "Tom" if d == today + 1 else DAYS[agenda.weekday(d)], hhmm(start))
            color = FG
            mins = (start - now + 59) // 60
            if mins < 60:
                soon = "in %dm" % mins
            elif mins < 24 * 60:
                soon = "in %dh" % (mins // 60)
        fb.text(when, 0, y, color)
        if soon:
            fb.text(soon, W - len(soon) * 8, y, STATUS)
        first, rest = wrap2(title, 20)
        fb.text(first, 0, y + 11, TEXT)
        fb.text(rest, 0, y + 21, TEXT)
        y += 40
    show()


# ---- Screen: pomodoro -------------------------------------------------------

FOCUS_MIN, BREAK_MIN, LONG_BREAK_MIN, ROUNDS = 25, 5, 15, 4


class Pomodoro:
    def __init__(self):
        self.reset()

    def reset(self):
        self.round = 1              # 1..ROUNDS
        self.on_break = False
        self.running = False
        self.total_ms = FOCUS_MIN * 60000
        self.left_ms = self.total_ms
        self.last = time.ticks_ms()

    def toggle(self):
        self.running = not self.running
        self.last = time.ticks_ms()

    def tick(self):
        # Advance the timer. Returns True when a phase has just finished.
        now = time.ticks_ms()
        elapsed = time.ticks_diff(now, self.last)
        self.last = now
        if not self.running:
            return False
        self.left_ms -= elapsed
        if self.left_ms > 0:
            return False
        if self.on_break:
            # Break over: next focus session waits for the button.
            self.on_break = False
            self.round = self.round % ROUNDS + 1
            self.total_ms = FOCUS_MIN * 60000
            self.running = False
        else:
            # Focus over: the break starts by itself.
            self.on_break = True
            long_break = self.round == ROUNDS
            self.total_ms = (LONG_BREAK_MIN if long_break else BREAK_MIN) * 60000
        self.left_ms = self.total_ms
        return True

    def seconds_left(self):
        return (self.left_ms + 999) // 1000


pomo = Pomodoro()


def draw_pomodoro():
    on, off = (BREAK, BREAK_DIM) if pomo.on_break else (FOCUS, FOCUS_DIM)
    fb.fill(BLACK)
    if pomo.on_break:
        long_break = pomo.total_ms == LONG_BREAK_MIN * 60000
        centered("LONG BREAK" if long_break else "BREAK", 2, on)
    else:
        centered("FOCUS %d/%d" % (pomo.round, ROUNDS), 2, on)
    s = pomo.seconds_left()
    m, s = s // 60, s % 60
    x, y = 32, 14
    digit(x, y, m // 10, on, off)
    digit(x + 22, y, m % 10, on, off)
    colon(x + 44, y, on)
    digit(x + 56, y, s // 10, on, off)
    digit(x + 78, y, s % 10, on, off)
    bar(8, 58, W - 16, 4, 1 - pomo.left_ms / pomo.total_ms, on)
    if pomo.running:
        hint = "hold: pause"
    elif pomo.left_ms == pomo.total_ms:
        hint = "hold: start"
    else:
        hint = "paused  hold: go"
    centered(hint, 68, STATUS)
    show()


# ---- Screen: Mac vitals -----------------------------------------------------
# mac_vitals.py sends one line per sample over USB serial:
#   V <cpu%> <mem%> <battery% or -1> <charging 0/1> <down bytes/s> <up bytes/s>
# and now and then:
#   T <year> <month> <day> <hour> <min> <sec>      Mac local time
#   S <unread count, -1 = Slack not open> <mention 0/1>

VITALS_STALE_MS = 6000
vitals = None                       # (cpu, mem, bat, charging, down, up)
vitals_at = 0
cpu_hist = bytearray(W)             # last 160 CPU samples, oldest first
slack = None                        # (unread count, mention) from the Mac's Slack tab
slack_at = 0
SLACK_STALE_MS = 30000
LINE_FIELDS = {"V": 7, "T": 7, "S": 3}
rx_line = ""
poller = select.poll()
poller.register(sys.stdin, select.POLLIN)


def handle_line(line):
    # One line from the Mac. Returns True if it was a new vitals sample.
    global vitals, vitals_at, slack, slack_at
    parts = line.split()
    if not parts or len(parts) != LINE_FIELDS.get(parts[0], 0):
        return False
    try:
        v = tuple(int(p) for p in parts[1:])
    except ValueError:
        return False
    if parts[0] == "S":                 # S <unread count, -1 = Slack not open> <mention 0/1>
        slack = v
        slack_at = time.ticks_ms()
        return False
    if parts[0] == "T":                 # T <year> <month> <day> <hour> <min> <sec>, Mac local time
        set_from_mac(v)
        return False
    if vitals is None or vitals_fresh() is False:
        print("vitals: link up")
    vitals = v
    vitals_at = time.ticks_ms()
    cpu_hist[:-1] = cpu_hist[1:]
    cpu_hist[-1] = min(max(v[0], 0), 100)
    return True


def set_from_mac(t):
    # Fallback time source: take the Mac's clock when Wi-Fi time sync is not working.
    if wifi_time_ok:
        return
    if not (2024 <= t[0] < 2100 and 1 <= t[1] <= 12 and 1 <= t[2] <= 31
            and t[3] < 24 and t[4] < 60 and t[5] < 61):
        return
    if abs(agenda.epoch(*t) - local_now()) < 2:
        return                          # already right
    RTC().datetime((t[0], t[1], t[2], agenda.weekday(agenda.days(t[0], t[1], t[2])),
                    t[3], t[4], t[5], 0))
    print("clock: time set from Mac")


def slack_unread():
    # (count, mention) while the Mac reports unread Slack messages, else None.
    if slack is None or slack[0] <= 0:
        return None
    if time.ticks_diff(time.ticks_ms(), slack_at) > SLACK_STALE_MS:
        return None
    return slack


def poll_serial():
    # Read whatever the Mac has sent; returns True if a new sample arrived.
    global rx_line
    got = False
    for _ in range(128):
        if not poller.poll(0):
            break
        ch = sys.stdin.read(1)
        if ch in ("\n", "\r"):
            line = rx_line
            rx_line = ""
            if handle_line(line):
                got = True
        elif ch < " ":
            pass                        # stray control character (e.g. left by a flashing tool)
        elif len(rx_line) < 80:
            rx_line += ch
        else:
            rx_line = ""
    return got


def vitals_fresh():
    return vitals is not None and time.ticks_diff(time.ticks_ms(), vitals_at) < VITALS_STALE_MS


def rate(n):
    if n < 1000:
        return "%dB" % n
    if n < 1000000:
        return "%dK" % (n // 1000)
    return "%.1fM" % (n / 1000000)


def load_color(pct):
    return FG if pct < 60 else AMBER if pct < 85 else WARN


def draw_vitals():
    fb.fill(BLACK)
    if not vitals_fresh():
        centered("MAC VITALS", 10, TEXT)
        centered("waiting for Mac", 32, WARN)
        centered("run mac_vitals.py", 52, STATUS)
        show()
        return
    cpu, mem, bat, charging, down, up = vitals
    fb.text("CPU%4d%%" % cpu, 2, 2, TEXT)
    bar(72, 2, 86, 8, cpu / 100, load_color(cpu))
    fb.text("MEM%4d%%" % mem, 2, 15, TEXT)
    bar(72, 15, 86, 8, mem / 100, load_color(mem))
    fb.text("DN %-6s UP %s" % (rate(down), rate(up)), 2, 28, TEXT)
    if bat >= 0:
        fb.text("BAT%4d%%%s" % (bat, " chg" if charging else ""), 2, 41, TEXT)
        top = 52
    else:
        top = 42
    # CPU history, one pixel column per sample.
    height = H - top - 1
    fb.hline(0, H - 1, W, BAR_BG)
    for x in range(W):
        v = cpu_hist[x]
        if v:
            h = max(1, v * height // 100)
            fb.vline(x, H - 1 - h, h, load_color(v))
    show()


# ---- Digital rain (backdrop of the clock) -----------------------------------
# A 20 x 10 grid of 8x8 characters. Each column has a bright head that falls one
# row at a time and leaves a fading green trail.

RAIN_COLS, RAIN_ROWS = W // 8, H // 8
RAIN_FRAME_MS = 55
RAIN_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ#$%&*+=<>?@:;"
# Kept dim so the time stays readable on top of it.
CLOCK_RAIN_HEAD = rgb(60, 130, 80)
CLOCK_RAIN_SHADES = [rgb(0, g, g // 5) for g in (120, 104, 88, 74, 62, 50, 40, 31, 23, 16)]

rain_grid = bytearray(RAIN_COLS * RAIN_ROWS)   # character shown in each cell
rain_head = [0] * RAIN_COLS                    # row of each column's head
rain_len = [0] * RAIN_COLS                     # trail length
rain_speed = [1] * RAIN_COLS                   # frames per one-row step
rain_frame = 0


def rain_char():
    return ord(RAIN_CHARS[random.getrandbits(8) % len(RAIN_CHARS)])


def rain_spawn(c, anywhere=False):
    rain_len[c] = 4 + random.getrandbits(3)                  # 4..11
    rain_speed[c] = 1 + random.getrandbits(2) % 3            # 1..3
    if anywhere:
        rain_head[c] = random.getrandbits(5) % (RAIN_ROWS + 8) - 4
    else:
        rain_head[c] = -(random.getrandbits(3))              # start above the screen


def rain_init():
    for i in range(len(rain_grid)):
        rain_grid[i] = rain_char()
    for c in range(RAIN_COLS):
        rain_spawn(c, True)


def rain_step():
    global rain_frame
    rain_frame += 1
    for c in range(RAIN_COLS):
        if rain_frame % rain_speed[c] == 0:
            rain_head[c] += 1
            if rain_head[c] - rain_len[c] > RAIN_ROWS:
                rain_spawn(c)
    for _ in range(4):                                        # a few glyphs flicker
        rain_grid[random.getrandbits(8) % len(rain_grid)] = rain_char()


def rain_paint(head_color, palette):
    # Paint the current rain frame into the framebuffer (no fill, no show).
    shades = len(palette)
    for c in range(RAIN_COLS):
        head = rain_head[c]
        length = rain_len[c]
        base = c * RAIN_ROWS
        for k in range(length):
            row = head - k
            if 0 <= row < RAIN_ROWS:
                color = head_color if k == 0 else palette[k * shades // length]
                fb.text(chr(rain_grid[base + row]), c * 8, row * 8, color)


rain_init()


# ---- Wi-Fi time sync --------------------------------------------------------

def nth_sunday(year, month, n):
    # Day of month of the n-th Sunday.
    wday = time.gmtime(time.mktime((year, month, 1, 0, 0, 0, 0, 0)))[6]  # Mon=0
    return 1 + (6 - wday) % 7 + 7 * (n - 1)


def utc_offset_s(utc):
    # Offset from UTC in seconds at the given UTC epoch time.
    std = cfg.UTC_OFFSET_MINUTES * 60
    if getattr(cfg, "DST", None) != "US":
        return std
    year = time.gmtime(utc + std)[0]
    # US rule: 02:00 standard time, 2nd Sunday of March -> 02:00 daylight
    # time (01:00 standard), 1st Sunday of November.
    start = time.mktime((year, 3, nth_sunday(year, 3, 2), 2, 0, 0, 0, 0)) - std
    end = time.mktime((year, 11, nth_sunday(year, 11, 1), 1, 0, 0, 0, 0)) - std
    return std + 3600 if start <= utc < end else std


wlan = network.WLAN(network.STA_IF)
status = "wifi: not set up" if cfg is None else "wifi: starting"
sync_state = "idle"                 # idle -> connecting -> (fetching) -> idle
wifi_time_ok = False                # True while the latest Wi-Fi time sync attempt succeeded
cal_job = None                      # generator downloading the calendar
cal_parser = None
fetch_started = 0
next_attempt = time.ticks_ms()      # when to start the next attempt
connect_started = 0


def wifi_off():
    try:
        wlan.disconnect()
    except OSError:
        pass
    wlan.active(False)


def schedule(seconds):
    global next_attempt, sync_state
    sync_state = "idle"
    next_attempt = time.ticks_add(time.ticks_ms(), seconds * 1000)


def sync_step():
    # Called from the main loop; never blocks for more than about a second.
    global sync_state, status, connect_started, wifi_time_ok
    if cfg is None:
        return
    if sync_state == "fetching":
        fetch_step()
        return
    if sync_state == "idle":
        if time.ticks_diff(time.ticks_ms(), next_attempt) < 0:
            return
        try:
            wlan.active(True)
            wlan.connect(cfg.SSID, cfg.PASSWORD)
        except OSError:
            status = "wifi: error"
            wifi_time_ok = False
            wifi_off()
            schedule(RETRY_S)
            return
        connect_started = time.ticks_ms()
        sync_state = "connecting"
        if not status.startswith("synced"):
            status = "wifi: connecting"
    elif sync_state == "connecting":
        if wlan.isconnected():
            try:
                ntptime.host = getattr(cfg, "NTP_HOST", "pool.ntp.org")
                ntptime.timeout = 2
                utc = ntptime.time()
                tm = time.gmtime(utc + utc_offset_s(utc))
                RTC().datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
                status = "synced %02d:%02d" % (tm[3], tm[4])
                print("clock: NTP sync ok")
                synced = True
                wifi_time_ok = True
            except Exception as e:
                print("clock: NTP failed:", e)
                wifi_time_ok = False
                if not status.startswith("synced"):
                    status = "ntp: failed"
                synced = False
            if CAL_URL and time.localtime()[0] >= 2024:
                start_fetch()
            else:
                wifi_off()
                schedule(RESYNC_S if synced else RETRY_S)
        elif time.ticks_diff(time.ticks_ms(), connect_started) > CONNECT_TIMEOUT_S * 1000:
            print("clock: wifi connect timed out, status", wlan.status())
            wifi_time_ok = False
            if not status.startswith("synced"):
                status = "wifi: no connection"
            wifi_off()
            schedule(RETRY_S)


def start_fetch():
    global cal_job, cal_parser, sync_state, fetch_started
    gc.collect()
    cal_parser = agenda.Parser(
        local_now(), CAL_DAYS,
        lambda utc: agenda.local_offset(utc, cfg.UTC_OFFSET_MINUTES, getattr(cfg, "DST", None)))
    cal_job = agenda.fetch(CAL_URL, cal_parser)
    fetch_started = time.ticks_ms()
    sync_state = "fetching"


def fetch_step():
    # Run one slice of the calendar download.
    global cal_job, cal_parser, cal_events, cal_status, cal_version
    failed = None
    try:
        if time.ticks_diff(time.ticks_ms(), fetch_started) > FETCH_TIMEOUT_S * 1000:
            raise OSError("timed out")
        next(cal_job)
        return
    except StopIteration:
        cal_events = cal_parser.finish()
        cal_status = "ok"
        print("agenda: %d events read, %d upcoming, %d repeat rules skipped, %d ms" % (
            cal_parser.seen, len(cal_events), cal_parser.skipped,
            time.ticks_diff(time.ticks_ms(), fetch_started)))
    except Exception as e:
        failed = e
        print("agenda: download failed:", repr(e))
        if not cal_events:
            cal_status = "download failed"
    try:
        cal_job.close()
    except Exception:
        pass
    cal_job = cal_parser = None
    cal_version += 1
    gc.collect()
    wifi_off()
    schedule(RETRY_S if failed else RESYNC_S)


# ---- Button -----------------------------------------------------------------

LONG_MS, RESET_MS, DEBOUNCE_MS = 600, 2500, 30


class Button:
    # Call poll() often. Returns "short" on release of a quick press, "long"
    # once the button has been held LONG_MS, "reset" once held RESET_MS.
    def __init__(self, pin):
        self.pin = pin
        self.down = False
        self.since = 0
        self.fired = 0              # 0 none, 1 long sent, 2 reset sent

    def poll(self):
        now = time.ticks_ms()
        pressed = self.pin.value() == 0
        if pressed and not self.down:
            self.down, self.since, self.fired = True, now, 0
            return None
        if not self.down:
            return None
        held = time.ticks_diff(now, self.since)
        if pressed:
            if self.fired == 0 and held >= LONG_MS:
                self.fired = 1
                return "long"
            if self.fired == 1 and held >= RESET_MS:
                self.fired = 2
                return "reset"
            return None
        self.down = False
        if self.fired == 0 and held >= DEBOUNCE_MS:
            return "short"
        return None


# ---- Main loop --------------------------------------------------------------

CLOCK, AGENDA, POMODORO, VITALS = 0, 1, 2, 3
screen = CLOCK
flash_until = 0                     # backlight blinks until this tick


def handle(event):
    # Apply a button event; returns True if the screen needs redrawing.
    global screen, flash_until
    if event is None:
        return False
    flash_until = 0
    if event == "short":
        # Clock and agenda count as one stop: they alternate by themselves.
        screen = {CLOCK: POMODORO, AGENDA: POMODORO, POMODORO: VITALS, VITALS: CLOCK}[screen]
    elif screen == POMODORO:
        if event == "long":
            pomo.toggle()
        else:
            pomo.reset()
    elif screen in (CLOCK, AGENDA) and event == "long" and sync_state == "idle":
        schedule(0)                 # go online now: time sync and calendar refresh
    return True


def main():
    global screen, flash_until
    lcd_init()
    btn = Button(button)
    drawn = None                    # what the display currently shows
    rotate_at = time.ticks_add(time.ticks_ms(), ROTATE_S * 1000)
    rain_at = 0
    logged_minute = -1
    while True:
        sync_step()
        dirty = handle(btn.poll())
        new_sample = poll_serial()
        now_ms = time.ticks_ms()
        if dirty or screen not in (CLOCK, AGENDA):
            rotate_at = time.ticks_add(now_ms, ROTATE_S * 1000)
        elif time.ticks_diff(now_ms, rotate_at) >= 0:
            rotate_at = time.ticks_add(now_ms, ROTATE_S * 1000)
            # Only show the agenda when it has something on it.
            if screen == CLOCK and upcoming_events(local_now()):
                screen = AGENDA
            else:
                screen = CLOCK
        if pomo.tick():
            screen = POMODORO
            flash_until = time.ticks_add(time.ticks_ms(), 4000)
            dirty = True

        now_ms = time.ticks_ms()
        if flash_until and time.ticks_diff(flash_until, now_ms) > 0:
            backlight.duty_u16(65535 if (now_ms // 250) % 2 else 6000)
        elif flash_until:
            flash_until = 0
            backlight.duty_u16(65535)

        if screen == CLOCK:
            if dirty or drawn is None or drawn[0] != CLOCK or \
                    time.ticks_diff(now_ms, rain_at) >= RAIN_FRAME_MS:
                rain_at = now_ms
                now = time.localtime()
                if now[4] != logged_minute:
                    logged_minute = now[4]
                    print("clock: %04d-%02d-%02d %02d:%02d:%02d" % now[0:6])
                rain_step()
                draw_clock(now)
                drawn = (CLOCK,)
        elif screen == AGENDA:
            key = (AGENDA, time.localtime()[4], cal_version, cal_status)
            if dirty or key != drawn:
                draw_agenda()
                drawn = key
        elif screen == POMODORO:
            key = (POMODORO, pomo.seconds_left(), pomo.running, pomo.on_break)
            if dirty or key != drawn:
                draw_pomodoro()
                drawn = key
        else:
            fresh = vitals_fresh()
            key = (VITALS, fresh)
            if dirty or new_sample or key != drawn:
                draw_vitals()
                drawn = key
        time.sleep_ms(10)


if __name__ == "__main__":
    main()
