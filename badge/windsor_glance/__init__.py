# MG Windsor EV & Home Telemetry Glance — Tier 2 badge client
# Badgeware (Pimoroni Tufty 2350). Copy this folder to /apps on the TUFTY drive.
#
# Badgeware provides these globals to every app: screen, badge, color, font,
# image, BUTTON_A/B/C/UP/DOWN, HIRES, VSYNC.
#
# Config lives in the badge's existing secrets.py (the same file Badgeware's
# wifi module reads). Add to it:
#   WIFI_SSID = "..."            WIFI_PASSWORD = "..."
#   BRIDGE_URL = "http://192.168.1.50:8000/api/v1/glance"
#   BADGE_KEY  = "..."           # == BADGE_API_KEY on the bridge

import gc
import json
import math
import time

import wifi

try:
    import secrets as _sec
except ImportError:
    _sec = None

SSID = getattr(_sec, "WIFI_SSID", None)
PSK = getattr(_sec, "WIFI_PASSWORD", None)
BRIDGE_URL = getattr(_sec, "BRIDGE_URL", "http://192.168.1.50:8000/api/v1/glance")
BADGE_KEY = getattr(_sec, "BADGE_KEY", "")

CACHE_PATH = "/windsor_glance.json"   # "/" is the only runtime-writable volume
WIFI_TIMEOUT_MS = 10000               # FR-2.1
HTTP_TIMEOUT_S = 5                    # FR-2.1
AWAKE_MS = 20000                      # FR-2.5 battery mode
SLEEP_S = 30 * 60                     # FR-2.5 battery mode
DESK_REFRESH_MS = 15 * 60 * 1000      # FR-2.5 USB mode
BRIGHT_LEVELS = (0.25, 0.5, 0.8, 1.0)  # index 4 == AUTO

# ----------------------------------------------------------------- display setup
try:
    badge.mode(HIRES | VSYNC)
except Exception:
    pass
W, H = screen.width, screen.height
S = W / 320  # layout is designed for 320x240; scales down to 160x120

try:
    VFONT = font.load("/system/assets/fonts/MonaSans-Medium.af")
    screen.antialias = image.X4
except Exception:
    VFONT = None

C_BG = color.rgb(12, 16, 26)
C_CARD = color.rgb(26, 33, 50)
C_HEAD = color.rgb(18, 24, 38)
C_TXT = color.rgb(235, 240, 250)
C_DIM = color.rgb(130, 142, 165)
C_GREEN = color.rgb(46, 204, 113)
C_AMBER = color.rgb(255, 176, 32)
C_RED = color.rgb(235, 64, 52)
C_CYAN = color.rgb(40, 190, 240)
C_SEG_OFF = color.rgb(44, 52, 72)

try:
    badge.default_clear = C_BG
except Exception:
    pass


def px(v):
    return int(v * S)


def text(msg, x, y, size, pen=None):
    """Draw text with the vector font (size in px) or fall back to a scaled pixel font."""
    if pen is not None:
        screen.pen = pen
    msg = str(msg)
    if VFONT:
        screen.font = VFONT
        screen.text(msg, int(x), int(y), max(6, px(size)))
    else:
        screen.font = font.smart
        screen.text(msg, int(x), int(y), max(1, round(px(size) / 9)))


def twidth(msg, size):
    msg = str(msg)
    if VFONT:
        screen.font = VFONT
        return screen.measure_text(msg, max(6, px(size)))[0]
    screen.font = font.smart
    return screen.measure_text(msg)[0] * max(1, round(px(size) / 9))


def text_r(msg, xr, y, size, pen=None):
    text(msg, xr - twidth(msg, size), y, size, pen)


def text_c(msg, xc, y, size, pen=None):
    text(msg, xc - twidth(msg, size) // 2, y, size, pen)


def fit(msg, maxw, size):
    msg = str(msg)
    if twidth(msg, size) <= maxw:
        return msg
    while msg and twidth(msg + "..", size) > maxw:
        msg = msg[:-1]
    return msg.rstrip() + ".."


def rect(x, y, w, h, pen=None):
    if pen is not None:
        screen.pen = pen
    screen.rectangle(int(x), int(y), int(w), int(h))


def pill(x, y, w, h, pen):
    r = h // 2
    rect(x + r, y, w - 2 * r, h, pen)
    screen.circle(int(x + r), int(y + r), r)
    screen.circle(int(x + w - r - 1), int(y + r), r)


def temp_text(t, x, y, size, pen, bg):
    """'24°C' without relying on a degree glyph."""
    if t is None:
        text("--", x, y, size, pen)
        return x + twidth("--", size)
    s = str(t)
    text(s, x, y, size, pen)
    dx = x + twidth(s, size) + px(2)
    r = max(2, px(size) // 8)
    screen.pen = pen
    screen.circle(int(dx + r), int(y + r + px(2)), r)
    screen.pen = bg
    screen.circle(int(dx + r), int(y + r + px(2)), max(1, r - max(1, px(2))))
    text("C", dx + 2 * r + px(2), y, size, pen)
    return dx + 2 * r + px(2) + twidth("C", size)


def soc_color(soc):
    if soc is None:
        return C_DIM
    return C_GREEN if soc > 50 else (C_AMBER if soc >= 20 else C_RED)


def aqi_color(aqi):
    if aqi is None:
        return C_DIM
    if aqi <= 50:
        return C_GREEN
    if aqi <= 100:
        return color.rgb(230, 210, 40)
    if aqi <= 150:
        return C_AMBER
    if aqi <= 200:
        return C_RED
    return color.rgb(150, 60, 200)


def aqi_cat(aqi):
    if aqi is None:
        return "--"
    for lim, name in ((50, "Good"), (100, "Moderate"), (150, "Poor"), (200, "Unhealthy"), (300, "V.Unhealthy")):
        if aqi <= lim:
            return name
    return "Hazardous"


# ------------------------------------------------------------------------- state
class App:
    def __init__(self):
        self.data = None
        self.view = 0          # 0 glance, 1 TPMS, 2 weather/schedule
        self.offline = False
        self.err = ""
        self.bright = 3
        self.overlay = None
        self.last_input = time.ticks_ms()
        self.last_fetch = None
        self.fetch_tick = time.ticks_ms()
        self.alert_start = None
        self.toast = None
        self.usb_seen = time.ticks_ms()
        self.load()

    # ---- persistence (FR-2.1)
    def load(self):
        try:
            with open(CACHE_PATH, "r") as f:
                c = json.load(f)
            self.data = c.get("data")
            self.bright = int(c.get("bright", 3)) % 5
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump({"data": self.data, "bright": self.bright}, f)
        except OSError:
            pass

    # ---- networking
    def wifi_up(self):
        if wifi.is_connected():
            return True
        deadline = time.ticks_add(time.ticks_ms(), WIFI_TIMEOUT_MS)
        while True:
            try:
                ok = wifi.connect(SSID, PSK, WIFI_TIMEOUT_MS // 1000, 1) if SSID else wifi.connect()
            except Exception:
                ok = False
            if ok:
                return True
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return False
            self.frame("Connecting Wi-Fi")

    def http_get_json(self):
        import socket

        url = BRIDGE_URL
        if not url.startswith("http://"):
            raise ValueError("BRIDGE_URL must be http://")
        hostport, _, path = url[7:].partition("/")
        host, _, port = hostport.partition(":")
        addr = socket.getaddrinfo(host, int(port or 80), 0, socket.SOCK_STREAM)[0][-1]
        s = socket.socket()
        try:
            s.settimeout(HTTP_TIMEOUT_S)
            s.connect(addr)
            req = "GET /%s HTTP/1.0\r\nHost: %s\r\nX-Badge-Key: %s\r\nAccept: application/json\r\nConnection: close\r\n\r\n" % (
                path, hostport, BADGE_KEY)
            s.send(req.encode())
            buf = b""
            while len(buf) < 8192:
                chunk = s.recv(1024)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        if status != 200:
            raise OSError("HTTP %d" % status)
        return json.loads(body)

    def sync(self, keep_wifi=False):
        self.overlay = "Syncing"
        try:
            if not self.wifi_up():
                raise OSError("Wi-Fi timeout")
            self.frame("Fetching")
            self.data = self.http_get_json()
            self.offline = False
            self.err = ""
            self.save()
            self.check_alerts()
        except Exception as e:
            self.offline = True
            self.err = str(e)[:40]
        finally:
            gc.collect()  # NFR-3
            if not keep_wifi:
                try:
                    wifi.disconnect()
                except Exception:
                    pass
            self.overlay = None
            self.fetch_tick = time.ticks_ms()
            self.last_input = time.ticks_ms()

    def ev(self):
        return (self.data or {}).get("ev") or {}

    def check_alerts(self):
        ev = self.ev()
        soc = ev.get("soc")
        if ev.get("locked") is False or (soc is not None and soc < 20):
            self.alert_start = time.ticks_ms()

    # ---- rear case lights (FR-2.4). Tufty's case LEDs are single-colour, so the
    # colour cue lives on screen and the LEDs signal via pattern instead.
    def leds(self, now):
        lvl = 0.0
        if self.alert_start is not None:
            t = time.ticks_diff(now, self.alert_start)
            if t < 1500:
                lvl = 1.0 if (t // 250) % 2 == 0 else 0.0  # 3 solid pulses
            else:
                self.alert_start = None
        elif self.ev().get("charging"):
            lvl = 0.05 + 0.45 * (0.5 + 0.5 * math.sin(now / 600))  # slow breathe
        try:
            badge.caselights(lvl)
        except Exception:
            pass

    # ---- brightness (FR-2.3)
    def brightness(self):
        if self.bright < 4:
            return BRIGHT_LEVELS[self.bright]
        try:  # AUTO: map ambient light sensor onto 25..100%
            raw = badge.light_level()
            return min(1.0, max(0.25, 0.25 + 0.75 * (raw / 40000)))
        except Exception:
            return 1.0

    def apply_brightness(self):
        b = self.brightness()
        for obj in (globals().get("display"), badge):
            fn = getattr(obj, "backlight", None) if obj is not None else None
            if fn:
                try:
                    fn(b)
                    return
                except Exception:
                    pass
        if b < 0.99:  # no backlight API on this firmware: dim in software
            screen.pen = color.rgb(0, 0, 0, int(255 * (1 - b)))
            screen.rectangle(0, 0, W, H)

    # ---------------------------------------------------------------- rendering
    def header(self, title):
        rect(0, 0, W, px(28), C_HEAD)
        text(title, px(10), px(7), 14, C_TXT)
        # badge battery
        bx, by, bw, bh = W - px(36), px(8), px(26), px(12)
        try:
            lvl = badge.battery_level()
            usb = badge.usb_connected()
        except Exception:
            lvl, usb = 100, False
        rect(bx, by, bw, bh, C_DIM)
        rect(bx + 1, by + 1, bw - 2, bh - 2, C_HEAD)
        rect(bx + bw, by + bh // 4, px(2), bh // 2, C_DIM)
        rect(bx + 2, by + 2, max(1, (bw - 4) * lvl // 100), bh - 4, C_RED if lvl < 15 else C_GREEN)
        if usb:
            text_c("+", bx + bw // 2, by - px(1), 11, C_TXT)
        x = bx - px(8)
        d = self.data or {}
        if self.offline:
            tag, pen = "OFFLINE", C_AMBER
        elif d.get("stale"):
            tag, pen = "STALE", C_DIM
        else:
            tag, pen = None, None
        t = d.get("updated_at") or "--:--"
        text_r(t, x, px(8), 12, C_DIM)
        if tag:
            text_r(tag, x - twidth(t, 12) - px(6), px(8), 11, pen)

    def view_glance(self, now):
        ev = self.ev()
        self.header(fit((ev.get("model") or "MG Windsor EV").upper(), px(170), 14))
        soc = ev.get("soc")
        # hero card
        cx, cy, cw, ch = px(8), px(36), W - px(16), px(124)
        rect(cx, cy, cw, ch, C_CARD)
        big = "--" if soc is None else str(soc)
        text(big, cx + px(14), cy + px(6), 62, soc_color(soc))
        text("%", cx + px(18) + twidth(big, 62), cy + px(30), 26, C_DIM)
        rng = ev.get("range_km")
        text_r("RANGE", cx + cw - px(14), cy + px(16), 11, C_DIM)
        text_r(("--" if rng is None else str(rng)) + " km", cx + cw - px(14), cy + px(34), 24, C_TXT)
        if soc is None and self.err:
            text(fit("! " + self.err, cw - px(28), 12), cx + px(14), cy + px(70), 12, C_AMBER)
        # segmented bar
        segs, gap = 10, px(4)
        bx, by, bw, bh = cx + px(14), cy + px(88), cw - px(28), px(22)
        sw = (bw - gap * (segs - 1)) // segs
        lit = 0 if soc is None else min(segs, (soc + 9) // 10)
        col = soc_color(soc)
        for i in range(segs):
            rect(bx + i * (sw + gap), by, sw, bh, col if i < lit else C_SEG_OFF)
        # status pill
        charging = ev.get("charging")
        locked = ev.get("locked")
        if charging:
            m = ev.get("charge_time_min") or 0
            label = "CHARGING" + (" - %dh %02dm" % (m // 60, m % 60) if m >= 60 else (" - %d min" % m if m else ""))
            pen, tpen = C_CYAN, C_BG
        elif locked is False:
            flash = (now // 500) % 2 == 0
            label, pen, tpen = "UNLOCKED", (C_RED if flash else C_CARD), (C_TXT if flash else C_RED)
        elif locked is True:
            label, pen, tpen = "LOCKED", C_GREEN, C_BG
        else:
            label, pen, tpen = "NO DATA", C_SEG_OFF, C_DIM
        pw = twidth(label, 14) + px(36)
        py = px(168)
        pill((W - pw) // 2, py, pw, px(26), pen)
        text_c(label, W // 2, py + px(6), 14, tpen)
        # footer
        fy = px(204)
        rect(0, fy, W, H - fy, C_HEAD)
        w = (self.data or {}).get("weather") or {}
        x = temp_text(w.get("temp_c"), px(10), fy + px(10), 15, C_TXT, C_HEAD)
        aqi = w.get("aqi")
        atxt = "AQI " + ("--" if aqi is None else str(aqi))
        aw = twidth(atxt, 12) + px(12)
        pill(x + px(8), fy + px(8), aw, px(20), aqi_color(aqi))
        text(atxt, x + px(14), fy + px(12), 12, C_BG)
        nxt = (self.data or {}).get("next_event")
        if nxt:
            left = x + px(16) + aw
            tm = nxt.get("time", "")
            text_r(tm, W - px(10), fy + px(4), 11, C_DIM)
            text_r(fit(nxt.get("title", ""), W - px(10) - left, 13), W - px(10), fy + px(18), 13, C_TXT)

    def view_tpms(self, now):
        ev = self.ev()
        self.header("DIAGNOSTICS / TPMS")
        # car body, top-down
        cx, top, bw, bh = W // 2, px(42), px(70), px(150)
        x0 = cx - bw // 2
        rect(x0, top, bw, bh, C_CARD)
        rect(x0 + px(8), top + px(26), bw - px(16), px(30), C_SEG_OFF)   # windscreen
        rect(x0 + px(8), top + bh - px(36), bw - px(16), px(22), C_SEG_OFF)  # rear glass
        text_c("FRONT", cx, top + px(6), 10, C_DIM)
        psi = ev.get("tire_psi") or [None] * 4
        wheels = ((0, x0 - px(14), top + px(16)), (1, x0 + bw, top + px(16)),
                  (2, x0 - px(14), top + bh - px(52)), (3, x0 + bw, top + bh - px(52)))
        names = ("FL", "FR", "RL", "RR")
        for i, wx, wy in wheels:
            p = psi[i] if i < len(psi) else None
            low = p is not None and p < 30
            pen = C_RED if low else (C_DIM if p is None else C_GREEN)
            rect(wx, wy, px(14), px(36), pen)
            val = "--" if p is None else str(p)
            if i % 2 == 0:
                text_r(val, wx - px(10), wy + px(2), 22, C_RED if low else C_TXT)
                text_r(names[i] + " PSI", wx - px(10), wy + px(26), 10, C_DIM)
            else:
                text(val, wx + px(24), wy + px(2), 22, C_RED if low else C_TXT)
                text(names[i] + " PSI", wx + px(24), wy + px(26), 10, C_DIM)
        # info strip
        fy = px(200)
        rect(0, fy, W, H - fy, C_HEAD)
        text("CABIN", px(10), fy + px(5), 10, C_DIM)
        temp_text(ev.get("cabin_temp_c"), px(10), fy + px(18), 15, C_TXT, C_HEAD)
        lk = ev.get("locked")
        text_c("DOORS", W // 2, fy + px(5), 10, C_DIM)
        text_c("LOCKED" if lk else ("UNLOCKED" if lk is False else "--"), W // 2, fy + px(18), 15,
               C_GREEN if lk else (C_RED if lk is False else C_DIM))
        text_r("CLOUD POLL", W - px(10), fy + px(5), 10, C_DIM)
        text_r(ev.get("polled_at") or "--:--", W - px(10), fy + px(18), 15, C_TXT)

    def view_weather(self, now):
        d = self.data or {}
        w = d.get("weather") or {}
        self.header(((w.get("location") or "Pune") + " - " + (d.get("date") or "")).upper())
        cx, cy, cw, ch = px(8), px(36), W - px(16), px(92)
        rect(cx, cy, cw, ch, C_CARD)
        temp_text(w.get("temp_c"), cx + px(12), cy + px(8), 44, C_TXT, C_CARD)
        text(fit(w.get("condition") or "--", px(150), 15), cx + px(14), cy + px(62), 15, C_DIM)
        rx = cx + cw - px(12)
        hi, lo, hum = w.get("hi"), w.get("lo"), w.get("humidity")
        text_r("H %s  L %s" % ("--" if hi is None else hi, "--" if lo is None else lo), rx, cy + px(10), 13, C_TXT)
        text_r("Humidity %s%%" % ("--" if hum is None else hum), rx, cy + px(30), 12, C_DIM)
        aqi = w.get("aqi")
        cat = w.get("aqi_cat") or aqi_cat(aqi)
        lab = "AQI %s  %s" % ("--" if aqi is None else aqi, cat)
        aw = twidth(lab, 12) + px(16)
        pill(rx - aw, cy + px(56), aw, px(22), aqi_color(aqi))
        text_c(lab, rx - aw // 2, cy + px(61), 12, C_BG)
        # schedule
        text("UP NEXT", px(10), px(138), 11, C_DIM)
        evs = d.get("events") or ([d["next_event"]] if d.get("next_event") else [])
        if not evs:
            text("Nothing scheduled", px(10), px(158), 14, C_DIM)
        y = px(154)
        for e in evs[:3]:
            rect(px(8), y, px(4), px(22), C_CYAN)
            tm = e.get("time", "")
            text(tm, px(18), y + px(3), 13, C_CYAN)
            tx = px(18) + max(twidth(tm, 13), px(96)) + px(8)
            text(fit(e.get("title", ""), W - tx - px(8), 14), tx, y + px(3), 14, C_TXT)
            y += px(28)

    def frame(self, overlay=None):
        now = time.ticks_ms()
        screen.pen = C_BG
        screen.clear()
        (self.view_glance, self.view_tpms, self.view_weather)[self.view](now)
        ov = overlay or self.overlay
        if ov:
            screen.pen = color.rgb(0, 0, 0, 170)
            screen.rectangle(0, 0, W, H)
            dots = "." * ((now // 300) % 4)
            text_c(ov + dots, W // 2, H // 2 - px(10), 18, C_TXT)
        if self.toast is not None and time.ticks_diff(now, self.toast) < 1500:
            lab = "BRIGHTNESS " + ("AUTO" if self.bright == 4 else "%d%%" % int(BRIGHT_LEVELS[self.bright] * 100))
            tw = twidth(lab, 13) + px(24)
            pill((W - tw) // 2, px(132), tw, px(24), C_TXT)
            text_c(lab, W // 2, px(137), 13, C_BG)
        self.apply_brightness()
        self.leds(now)
        badge.update()

    # -------------------------------------------------------------------- loop
    def handle_buttons(self):
        hit = False
        if badge.pressed(BUTTON_A):
            self.sync(keep_wifi=self.on_usb())
            hit = True
        if badge.pressed(BUTTON_B):
            self.view = 0 if self.view == 1 else 1
            hit = True
        if badge.pressed(BUTTON_C):
            self.view = 0 if self.view == 2 else 2
            hit = True
        if badge.pressed(BUTTON_UP):
            self.bright = (self.bright + 1) % 5
            self.save()
            hit = True
        if badge.pressed(BUTTON_DOWN):
            self.bright = (self.bright - 1) % 5
            self.save()
            hit = True
        if badge.pressed(BUTTON_UP) or badge.pressed(BUTTON_DOWN):
            self.toast = time.ticks_ms()
        if hit:
            self.last_input = time.ticks_ms()

    def on_usb(self):
        try:
            return badge.usb_connected()
        except Exception:
            return False

    def go_to_sleep(self):
        try:
            wifi.disconnect()
        except Exception:
            pass
        try:
            badge.caselights(0)
        except Exception:
            pass
        gc.collect()
        badge.sleep(SLEEP_S)  # RAM is lost; waking relaunches like a reset

    def run(self):
        self.frame("Syncing")
        self.sync(keep_wifi=self.on_usb())
        while True:
            self.handle_buttons()
            now = time.ticks_ms()
            if self.on_usb():
                # Desk-clock mode: never sleep, refresh every 15 min
                if time.ticks_diff(now, self.fetch_tick) > DESK_REFRESH_MS:
                    self.sync(keep_wifi=True)
                self.usb_seen = now
            elif time.ticks_diff(now, max_tick(self.last_input, self.usb_seen)) > AWAKE_MS:
                # battery mode: 20 s after wake / last press / unplug
                self.go_to_sleep()
            self.frame()


def max_tick(a, b):
    return a if time.ticks_diff(a, b) >= 0 else b

App().run()
