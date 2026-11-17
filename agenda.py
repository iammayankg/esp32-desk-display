# Calendar support for the desk display: download an iCal (.ics) feed over HTTPS
# and pick out the events of the next few days.
#
# Written to run on MicroPython (the board) and on desktop Python (for tests).
# All times are "local seconds since 2000-01-01", plain integers with no timezone.
#
# Known limits:
#   - Times given with a TZID are converted using the VTIMEZONE rules in the file
#     itself (one standard and one daylight rule per zone, as Google exports them).
#     A TZID the file does not define is assumed to be the display's own timezone.
#   - Repeat rules handled: DAILY, WEEKLY, MONTHLY (by date or by "2nd Tuesday"),
#     YEARLY, with INTERVAL / COUNT / UNTIL / EXDATE and edited single instances.
#     Anything more exotic is skipped and counted in Parser.skipped.
#   - The HTTPS certificate is not verified.
import socket
import ssl
import time

DAY = 86400
_BASE = 10957  # days from 1970-01-01 to 2000-01-01


def days(y, m, d):
    # Day number of a date (0 = 2000-01-01).
    if m <= 2:
        y -= 1
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468 - _BASE


def civil(z):
    # (year, month, day) of a day number.
    z += 719468 + _BASE
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return yoe + era * 400 + (1 if m <= 2 else 0), m, d


def weekday(day):
    return (day + 5) % 7  # Monday = 0; 2000-01-01 was a Saturday


def epoch(y, m, d, hh=0, mm=0, ss=0):
    return days(y, m, d) * DAY + hh * 3600 + mm * 60 + ss


def days_in_month(y, m):
    return days(y + (1 if m == 12 else 0), m % 12 + 1, 1) - days(y, m, 1)


def _nth_sunday(y, m, n):
    first = days(y, m, 1)
    return first + (6 - weekday(first)) % 7 + 7 * (n - 1)


def local_offset(utc, std_minutes, dst=None):
    # Seconds to add to a UTC time to get local time.
    std = std_minutes * 60
    if dst != "US":
        return std
    y = civil((utc + std) // DAY)[0]
    start = _nth_sunday(y, 3, 2) * DAY + 2 * 3600 - std
    end = _nth_sunday(y, 11, 1) * DAY + 3600 - std
    return std + 3600 if start <= utc < end else std


def nth_weekday(y, m, n, wd):
    # Day number of the n-th (n > 0) or n-th from last (n < 0) given weekday of a month.
    if n > 0:
        first = days(y, m, 1)
        return first + (wd - weekday(first)) % 7 + 7 * (n - 1)
    last = days(y, m, days_in_month(y, m))
    return last - (weekday(last) - wd) % 7 - 7 * (-n - 1)


def zone_offset(zone, wall):
    # UTC offset in seconds of `zone` at wall-clock time `wall` (seconds in that zone).
    # zone is (std,) or (std, dst, (month, n, weekday, time), (month, n, weekday, time)).
    if len(zone) == 1:
        return zone[0]
    std, dst, on, off = zone
    y = civil(wall // DAY)[0]
    start = nth_weekday(y, on[0], on[1], on[2]) * DAY + on[3]
    end = nth_weekday(y, off[0], off[1], off[2]) * DAY + off[3]
    if start < end:
        in_dst = start <= wall < end
    else:                                  # southern hemisphere
        in_dst = wall >= start or wall < end
    return dst if in_dst else std


def _offset_value(val):
    # b"+0100" / b"-0530" -> seconds
    sign = -1 if val[:1] == b"-" else 1
    return sign * (int(val[1:3]) * 3600 + int(val[3:5]) * 60)


def _tzid(key):
    # b"DTSTART;TZID=Europe/London" -> b"Europe/London"
    i = key.find(b"TZID=")
    if i < 0:
        return None
    j = key.find(b";", i)
    return key[i + 5:] if j < 0 else key[i + 5:j]


_WD = {b"MO": 0, b"TU": 1, b"WE": 2, b"TH": 3, b"FR": 4, b"SA": 5, b"SU": 6}
_UNSUPPORTED = (b"BYSETPOS", b"BYWEEKNO", b"BYYEARDAY", b"BYHOUR", b"BYMINUTE", b"BYSECOND")
_MAX_STEPS = 6000


class Unsupported(Exception):
    pass


def clean(raw, limit=40):
    # iCal text -> plain ASCII for the display font.
    out = []
    i = 0
    n = min(len(raw), 160)
    while i < n and len(out) < limit:
        b = raw[i]
        i += 1
        if b == 92 and i < n:          # backslash escape
            b = raw[i]
            i += 1
            if b in (110, 78):         # \n
                b = 32
        if 32 <= b < 127:
            out.append(chr(b))
        elif b >= 0xC0:                # start of a non-ASCII character
            out.append("?")
    return "".join(out).strip()


class Parser:
    """Feed it the lines of an .ics file; finish() returns the upcoming events
    as a sorted list of (start, end, all_day, title)."""

    def __init__(self, now, days_ahead, utc_offset):
        self.ws = now
        self.we = now + days_ahead * DAY
        self.utc_offset = utc_offset      # function: utc seconds -> offset seconds
        self.events = []
        self.overrides = set()
        self.skipped = 0                  # repeat rules we could not expand
        self.seen = 0                     # VEVENTs read
        self.pending = None
        self.ev = None
        self.depth = 0
        self.zones = {}                   # TZID -> zone tuple, from VTIMEZONE blocks
        self.tz = None                    # VTIMEZONE being read
        self.tz_part = None

    def feed(self, raw):
        # One physical line, without its newline.
        if raw[-1:] == b"\r":
            raw = raw[:-1]
        if raw[:1] in (b" ", b"\t"):      # folded continuation of the previous line
            if self.pending is not None and len(self.pending) < 1000:
                self.pending += raw[1:]
            return
        line = self.pending
        self.pending = raw
        if line:
            self._line(line)

    def finish(self):
        if self.pending:
            self._line(self.pending)
            self.pending = None
        out = [e[:4] for e in self.events if e[4] not in self.overrides]
        out.sort()
        return out[:30]

    def _line(self, line):
        ev = self.ev
        if ev is None:
            if line == b"BEGIN:VEVENT":
                self.ev = {}
                self.depth = 0
            elif self.tz is not None:
                self._tz_line(line)
            elif line == b"BEGIN:VTIMEZONE":
                self.tz = {}
            return
        if line[:6] == b"BEGIN:":
            self.depth += 1
            return
        if line[:4] == b"END:":
            if self.depth:
                self.depth -= 1
                return
            self.ev = None
            self.seen += 1
            try:
                self._event(ev)
            except Unsupported:
                self.skipped += 1
            except (ValueError, KeyError, IndexError):
                pass                       # malformed event: ignore it
            return
        if self.depth:
            return                         # inside a VALARM
        c = line.find(b":")
        if c < 0:
            return
        key = line[:c]
        val = line[c + 1:]
        s = key.find(b";")
        name = key if s < 0 else key[:s]
        try:
            if name in (b"DTSTART", b"DTEND", b"RECURRENCE-ID"):
                ev[name] = self._when(val, key)
            elif name == b"EXDATE":
                ex = ev.setdefault(b"EXDATE", [])
                for v in val.split(b","):
                    t, _, zone = self._when(v, key)
                    ex.append(self._local(t, zone))
            elif name in (b"SUMMARY", b"RRULE", b"STATUS", b"UID"):
                ev[name] = val
        except (ValueError, IndexError):
            pass

    def _tz_line(self, line):
        # Inside a VTIMEZONE block: collect its standard and daylight rules.
        tz = self.tz
        if line == b"END:VTIMEZONE":
            self.tz = None
            try:
                self._tz_done(tz)
            except (KeyError, ValueError, IndexError):
                pass
        elif line in (b"BEGIN:STANDARD", b"BEGIN:DAYLIGHT"):
            self.tz_part = {b"KIND": line[6:]}
        elif line in (b"END:STANDARD", b"END:DAYLIGHT"):
            part = self.tz_part
            self.tz_part = None
            # Keep the newest rule of each kind (files can list historical ones).
            old = tz.get(part[b"KIND"])
            if old is None or part.get(b"DTSTART", b"") >= old.get(b"DTSTART", b""):
                tz[part[b"KIND"]] = part
        else:
            c = line.find(b":")
            if c > 0:
                (tz if self.tz_part is None else self.tz_part)[line[:c]] = line[c + 1:]

    def _tz_done(self, tz):
        std = tz.get(b"STANDARD")
        dst = tz.get(b"DAYLIGHT")
        if std is None:
            return
        std_off = _offset_value(std[b"TZOFFSETTO"])
        zone = (std_off,)
        if dst is not None and b"RRULE" in dst and b"RRULE" in std:
            def rule(part):
                r = {}
                for item in part[b"RRULE"].split(b";"):
                    eq = item.find(b"=")
                    if eq > 0:
                        r[item[:eq]] = item[eq + 1:]
                at = part[b"DTSTART"]
                tok = r[b"BYDAY"]
                return (int(r[b"BYMONTH"]) if b"BYMONTH" in r else int(at[4:6]),
                        int(tok[:-2]) if len(tok) > 2 else 1, _WD[tok[-2:]],
                        int(at[9:11]) * 3600 + int(at[11:13]) * 60)
            zone = (std_off, _offset_value(dst[b"TZOFFSETTO"]), rule(dst), rule(std))
        self.zones[tz[b"TZID"]] = zone

    def _local(self, t, zone):
        # Wall-clock time in `zone` -> display-local time.
        if zone is None:
            return t
        utc = t - zone_offset(zone, t)
        return utc + self.utc_offset(utc)

    def _when(self, val, key=b""):
        # b"20261116T180000Z" / b"20261116T100000" / b"20261116" -> (seconds, all_day, zone)
        # zone is None when the time is already display-local, else the zone tuple
        # the wall-clock time is expressed in.
        t = days(int(val[0:4]), int(val[4:6]), int(val[6:8])) * DAY
        if len(val) < 15:
            return t, True, None
        t += int(val[9:11]) * 3600 + int(val[11:13]) * 60 + int(val[13:15])
        if val[15:16] == b"Z":
            return t + self.utc_offset(t), False, None
        tzid = _tzid(key)
        return t, False, self.zones.get(tzid) if tzid else None

    def _add(self, start, dur, all_day, title, tag):
        if start >= self.we:
            return
        if start + dur <= self.ws and not (dur == 0 and start >= self.ws):
            return
        self.events.append((start, start + dur, all_day, title, tag))
        if len(self.events) > 90:
            self.events.sort()
            del self.events[60:]

    def _event(self, ev):
        if ev.get(b"STATUS") == b"CANCELLED":
            return
        start, all_day, zone = ev[b"DTSTART"]
        end = ev.get(b"DTEND")
        title = clean(ev.get(b"SUMMARY", b"")) or "(no title)"
        uid = hash(ev.get(b"UID", b""))
        rid = ev.get(b"RECURRENCE-ID")
        if rid:
            self.overrides.add((uid, self._local(rid[0], rid[2])))
        rule = ev.get(b"RRULE")
        if rid or not rule:
            s_local = self._local(start, zone)
            if end:
                dur = self._local(end[0], end[2]) - s_local
            else:
                dur = DAY if all_day else 0
            self._add(s_local, max(dur, 0), all_day, title, None)
            return
        if not end:
            dur = DAY if all_day else 0
        elif end[2] is zone:
            dur = end[0] - start            # same zone: wall-clock length
        else:
            dur = self._local(end[0], end[2]) - self._local(start, zone)
        self._expand(rule, start, max(dur, 0), all_day, title, uid,
                     ev.get(b"EXDATE", ()), zone)

    def _expand(self, rule, start, dur, all_day, title, uid, exdates, zone):
        # `start` and the repeat pattern are in the event's own zone; each
        # occurrence is converted to display-local time before it is kept.
        r = {}
        for part in rule.split(b";"):
            eq = part.find(b"=")
            if eq > 0:
                r[part[:eq]] = part[eq + 1:]
        for k in _UNSUPPORTED:
            if k in r:
                raise Unsupported()
        freq = r.get(b"FREQ")
        if freq not in (b"DAILY", b"WEEKLY", b"MONTHLY", b"YEARLY"):
            raise Unsupported()
        interval = int(r.get(b"INTERVAL", b"1")) or 1
        count = int(r.get(b"COUNT", b"0"))
        limit = self.we
        if b"UNTIL" in r:
            until, date_only, _ = self._when(r[b"UNTIL"])
            if date_only:
                until += DAY - 1
            limit = min(limit, until + 1)

        byday = []
        if b"BYDAY" in r:
            for tok in r[b"BYDAY"].split(b","):
                byday.append((int(tok[:-2]) if len(tok) > 2 else 0, _WD[tok[-2:]]))
        bymd = [int(v) for v in r[b"BYMONTHDAY"].split(b",")] if b"BYMONTHDAY" in r else []
        if any(v < 0 for v in bymd):
            raise Unsupported()
        months = None
        if b"BYMONTH" in r:
            if freq != b"YEARLY":
                raise Unsupported()
            months = [int(v) for v in r[b"BYMONTH"].split(b",")]
        if freq == b"YEARLY" and byday:
            raise Unsupported()
        if byday and bymd:
            raise Unsupported()

        sd = start // DAY
        tod = start - sd * DAY
        swd = weekday(sd)
        sy, sm, sdom = civil(sd)
        if months is None:
            months = [sm]
        weekdays = [w for _, w in byday] or [swd]

        def match(day):
            if freq == b"DAILY":
                return (day - sd) % interval == 0
            wd = weekday(day)
            if freq == b"WEEKLY":
                return wd in weekdays and (((day - wd) - (sd - swd)) // 7) % interval == 0
            y, m, d = civil(day)
            if freq == b"YEARLY":
                return (y - sy) % interval == 0 and m in months and d == sdom
            if ((y - sy) * 12 + m - sm) % interval:
                return False
            if byday:
                for n, w in byday:
                    if w != wd:
                        continue
                    if n == 0 or (n > 0 and (d - 1) // 7 + 1 == n):
                        return True
                    if n < 0 and (days_in_month(y, m) - d) // 7 + 1 == -n:
                        return True
                return False
            if bymd:
                return d in bymd
            return d == sdom

        # With COUNT we must count from the first occurrence; otherwise jump to the window.
        # The window is in display-local time, so allow a day of slack either side.
        day = sd if count else max(sd, (self.ws - dur - tod) // DAY - 1)
        ex = set(exdates)                  # already display-local
        n = 0
        steps = 0
        while True:
            o = day * DAY + tod
            if o >= limit + 2 * DAY:
                break
            steps += 1
            if steps > _MAX_STEPS:
                raise Unsupported()
            if match(day):
                n += 1
                if count and n > count:
                    break
                o = self._local(o, zone)
                if o >= limit:
                    break
                if o not in ex:
                    self._add(o, dur, all_day, title, (uid, o))
            day += 1


def _split_url(url):
    if not url.startswith("https://"):
        raise ValueError("calendar link must start with https://")
    rest = url[8:]
    slash = rest.find("/")
    host = rest if slash < 0 else rest[:slash]
    path = "/" if slash < 0 else rest[slash:]
    port = 443
    if ":" in host:
        host, p = host.split(":")
        port = int(p)
    return host, port, path


def _connect(host, port):
    ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
    s = socket.socket(ai[0], ai[1], ai[2])
    s.settimeout(10)
    s.connect(ai[-1])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        ctx.check_hostname = False         # desktop Python needs this before CERT_NONE
    except AttributeError:
        pass
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(s, server_hostname=host)


def fetch(url, parser, chunk=1024):
    """Generator: download `url` and feed it to `parser`, yielding between
    chunks so the caller's main loop keeps running. Raises on failure."""
    for _ in range(4):                      # follow up to 3 redirects
        host, port, path = _split_url(url)
        yield
        s = _connect(host, port)
        try:
            yield
            s.write(("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: esp32-desk-display\r\n"
                     "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
                     % (path, host)).encode())
            read = getattr(s, "recv", None) or s.read
            buf = b""
            in_headers = True
            status = None
            location = None
            while True:
                data = read(chunk)
                if not data:
                    break
                buf += data
                start = 0
                while True:
                    nl = buf.find(b"\n", start)
                    if nl < 0:
                        break
                    line = buf[start:nl]
                    start = nl + 1
                    if not in_headers:
                        parser.feed(line)
                        continue
                    line = line.rstrip()
                    if status is None:
                        status = int(line.split()[1])
                    elif not line:
                        in_headers = False
                        if status != 200:
                            break
                    else:
                        low = line.lower()
                        if low.startswith(b"location:"):
                            location = line[9:].strip().decode()
                        elif low.startswith(b"transfer-encoding:") and b"chunked" in low:
                            raise ValueError("server sent chunked data")
                buf = buf[start:]
                if len(buf) > 4096:         # absurdly long line: drop it
                    buf = b""
                if not in_headers and status != 200:
                    break
                yield
            if status == 200:
                if buf:
                    parser.feed(buf)
                return
            if status in (301, 302, 303, 307, 308) and location:
                url = location
                continue
            raise ValueError("HTTP %s" % status)
        finally:
            s.close()
    raise ValueError("too many redirects")
