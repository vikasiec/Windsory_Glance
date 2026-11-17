"""Desktop simulator for the windsor_glance Badgeware app.

Fakes just enough of the Badgeware runtime (screen, badge, color, font, wifi,
buttons) on top of Pillow to run badge/windsor_glance/__init__.py unmodified
and save screenshots.

    # against a running bridge
    python tools/simulator.py --url http://127.0.0.1:8000/api/v1/glance --key KEY --out shots/
    # against a JSON fixture (no bridge needed)
    python tools/simulator.py --fixture tools/fixtures/parked.json --out shots/ --prefix parked
    # bridge unreachable -> exercises OFFLINE + cache fallback
    python tools/simulator.py --url http://127.0.0.1:9/x --out shots/ --prefix offline

Script: one screenshot per view (A=refresh, B=TPMS, C=weather).
"""

import argparse
import builtins
import io
import json
import os
import sys
import tempfile
import time
import types

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "badge", "windsor_glance", "__init__.py")
FONT_FILE = None
for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf",
             "/System/Library/Fonts/Supplemental/Arial.ttf"):
    if os.path.exists(cand):
        FONT_FILE = cand
        break


class Done(Exception):
    pass


# ---------------------------------------------------------------- time shims
_t0 = time.monotonic()
time.ticks_ms = lambda: int((time.monotonic() - _t0) * 1000)
time.ticks_diff = lambda a, b: a - b
time.ticks_add = lambda a, b: a + b


# ---------------------------------------------------------------- graphics
class Color(tuple):
    pass


class ColorNS:
    @staticmethod
    def rgb(r, g, b, a=255):
        return Color((int(r), int(g), int(b), int(a)))


for name, rgb in dict(black=(0, 0, 0), white=(255, 255, 255), navy=(29, 43, 83), red=(255, 0, 77),
                      green=(0, 228, 54), blue=(41, 173, 255), orange=(255, 163, 0), grey=(95, 87, 79),
                      cyan=(0, 255, 255), yellow=(255, 236, 39), lime=(0, 255, 0), grape=(126, 37, 83),
                      brown=(171, 82, 54), taupe=(131, 118, 156), smoke=(194, 195, 199), latte=(255, 204, 170)).items():
    setattr(ColorNS, name, ColorNS.rgb(*rgb))


class VecFont:
    vector = True

    def __init__(self):
        self.cache = {}

    def get(self, size):
        if size not in self.cache:
            self.cache[size] = ImageFont.truetype(FONT_FILE, size) if FONT_FILE else ImageFont.load_default()
        return self.cache[size]


class PixFont(VecFont):
    vector = False

    def __init__(self, h):
        super().__init__()
        self.h = h


class FontNS:
    smart = PixFont(9)

    @staticmethod
    def load(path):
        return VecFont()


class ImageNS:
    X4 = X2 = OFF = 0
    CENTER = MIDDLE = ELLIPSES = 0


class Screen:
    def __init__(self, w, h):
        self.width, self.height = w, h
        self.img = Image.new("RGB", (w, h))
        self.d = ImageDraw.Draw(self.img, "RGBA")
        self.pen = ColorNS.white
        self.font = FontNS.smart
        self.antialias = 0

    def _ttf(self, size):
        f = self.font
        if f.vector:
            return f.get(int(size or 12))
        return f.get(int(f.h * (size or 1) * 1.15))

    def clear(self):
        self.d.rectangle([0, 0, self.width, self.height], fill=tuple(self.pen))

    def rectangle(self, x, y, w, h):
        if w > 0 and h > 0:
            self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=tuple(self.pen))

    def circle(self, x, y, r):
        self.d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(self.pen))

    def line(self, x0, y0, x1, y1):
        self.d.line([x0, y0, x1, y1], fill=tuple(self.pen))

    def text(self, msg, x, y, size=None):
        self.d.text((x, y), str(msg), font=self._ttf(size), fill=tuple(self.pen), anchor="lt")

    def measure_text(self, msg, size=None):
        l, t, r, b = self._ttf(size).getbbox(str(msg), anchor="lt")
        return (r - l, b - t)


# ---------------------------------------------------------------- badge
BUTTONS = ["BUTTON_A", "BUTTON_B", "BUTTON_C", "BUTTON_UP", "BUTTON_DOWN", "BUTTON_HOME"]


class Badge:
    def __init__(self, screen, script, outdir, prefix, usb, battery):
        self.screen, self.script, self.outdir, self.prefix = screen, list(script), outdir, prefix
        self.frame_no = 0
        self.pressed_now = set()
        self.usb, self.batt = usb, battery
        self.lights = 0.0
        self.saved = []
        self.frame_ms = []
        self._last = time.perf_counter()

    def mode(self, *a):
        pass

    def pressed(self, b=None):
        return b in self.pressed_now if b is not None else list(self.pressed_now)

    def caselights(self, *lv):
        if lv:
            self.lights = lv[0]

    def battery_level(self):
        return self.batt

    def usb_connected(self):
        return self.usb

    def light_level(self):
        return 20000

    def sleep(self, s=None):
        self.saved.append(("sleep", s))
        raise Done()

    def update(self):
        now = time.perf_counter()
        self.frame_ms.append((now - self._last) * 1000)
        self._last = now
        self.frame_no += 1
        self.pressed_now = set()
        while self.script and self.script[0][0] <= self.frame_no:
            _, act, arg = self.script.pop(0)
            if act == "press":
                self.pressed_now.add(arg)
            elif act == "shot":
                path = os.path.join(self.outdir, f"{self.prefix}_{arg}.png")
                self.screen.img.resize((640, 480), Image.NEAREST).save(path)
                self.saved.append(path)
            elif act == "end":
                raise Done()


def fake_socket_module(payload: bytes):
    mod = types.ModuleType("socket")
    mod.SOCK_STREAM = 1

    class S:
        def __init__(self, *a):
            self.buf = io.BytesIO(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + payload)

        settimeout = connect = close = lambda self, *a: None
        send = lambda self, b: len(b)
        recv = lambda self, n: self.buf.read(n)

    mod.socket = S
    mod.getaddrinfo = lambda *a: [(0, 0, 0, "", ("127.0.0.1", 80))]
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/v1/glance")
    ap.add_argument("--key", default="")
    ap.add_argument("--fixture")
    ap.add_argument("--out", default="shots")
    ap.add_argument("--prefix", default="view")
    ap.add_argument("--usb", action="store_true")
    ap.add_argument("--battery", type=int, default=72)
    ap.add_argument("--cache", help="cache file to use (simulates /windsor_glance.json)")
    ap.add_argument("--extra", default="", help="extra script, e.g. 'shot:flash@8'")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    # secrets + wifi
    sys.modules["secrets"] = types.SimpleNamespace(WIFI_SSID="sim", WIFI_PASSWORD="sim", BRIDGE_URL=a.url, BADGE_KEY=a.key)
    sys.modules["wifi"] = types.SimpleNamespace(is_connected=lambda: False, connect=lambda *x: True,
                                                disconnect=lambda: None)
    if a.fixture:
        with open(a.fixture, "rb") as f:
            sys.modules["socket"] = fake_socket_module(f.read())

    script = [(3, "shot", "1_glance"), (4, "press", "BUTTON_B"), (6, "shot", "2_tpms"),
              (7, "press", "BUTTON_C"), (9, "shot", "3_weather"), (10, "press", "BUTTON_C")]
    for item in filter(None, a.extra.split(",")):
        what, at = item.split("@")
        act, arg = what.split(":")
        script.append((int(at), act, arg))
    script.append((max(s[0] for s in script) + 2, "end", None))
    script.sort(key=lambda s: s[0])

    scr = Screen(320, 240)
    bdg = Badge(scr, script, a.out, a.prefix, a.usb, a.battery)
    g = dict(screen=scr, badge=bdg, color=ColorNS, font=FontNS, image=ImageNS, HIRES=1, VSYNC=2,
             __name__="windsor_glance")
    for b in BUTTONS:
        g[b] = b

    cache = a.cache or os.path.join(tempfile.mkdtemp(), "cache.json")
    src = open(APP).read().replace('"/windsor_glance.json"', repr(cache))
    try:
        exec(compile(src, APP, "exec"), g)
    except Done:
        pass
    ft = bdg.frame_ms[2:] or [0]
    print(json.dumps({"saved": bdg.saved, "frames": bdg.frame_no, "max_frame_ms": round(max(ft), 1),
                      "offline": g.get("App") and None, "cache": cache}, indent=1))


if __name__ == "__main__":
    main()
