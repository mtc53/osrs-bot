"""Driving the mouse and keyboard.

Windows SendInput, straight through ctypes. Nothing here is injected into
the game or hooked onto it - these are ordinary OS-level input events, the
same ones a mouse produces, delivered to whatever window has focus. That is
the entire reason the architecture splits this away from the plugin: the
client is only ever read, never written to.

Movement is a cubic Bezier rather than a straight line, with the step
timing weighted toward the middle of the path. That is not a disguise, it
is just what a mouse does - a linear interpolation at a fixed step rate is
a worse model of a hand, and it also tends to miss, because it arrives at
the target with the client still processing the previous frame.
"""
from __future__ import annotations

import contextlib
import ctypes
import math
import random
import time
from ctypes import wintypes

# --------------------------------------------------------------- ctypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

GA_ROOT = 2


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send(*inputs: _INPUT) -> None:
    array = (_INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput rejected the event "
                      f"(error {ctypes.get_last_error()})")


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_MOUSE, union=_INPUTUNION(
        mi=_MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags,
                       time=0, dwExtraInfo=0)))


def _key_event(vk: int, scan: int, flags: int) -> _INPUT:
    return _INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(
        ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                       time=0, dwExtraInfo=0)))


# ----------------------------------------------------------------- keys

VK = {
    "escape": 0x1B, "esc": 0x1B, "space": 0x20, "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "backspace": 0x08, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
_EXTENDED = {0x25, 0x26, 0x27, 0x28}


class Mouse:
    """Absolute-positioned mouse with a human-shaped path."""

    def __init__(self, rng: random.Random | None = None, speed: float = 1.0):
        self.rng = rng or random.Random()
        # Scales every movement duration. Below ~0.6 the client starts
        # dropping the hover, which is what breaks menu verification.
        self.speed = max(0.3, speed)

    # -- primitives -------------------------------------------------

    @staticmethod
    def position() -> tuple[int, int]:
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    @staticmethod
    def _absolute(x: int, y: int) -> tuple[int, int]:
        """Screen pixels to the 0-65535 space SendInput wants.

        Measured against the whole virtual desktop, so this stays correct
        on a second monitor with a negative origin.
        """
        vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(1, _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 1)
        vh = max(1, _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 1)
        return (int(round((x - vx) * 65535 / vw)),
                int(round((y - vy) * 65535 / vh)))

    def _jump(self, x: int, y: int) -> None:
        ax, ay = self._absolute(x, y)
        _send(_mouse_event(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            dx=ax, dy=ay))

    # -- movement ---------------------------------------------------

    def _duration(self, distance: float) -> float:
        """Fitts' law, roughly: time grows with the log of the distance."""
        if distance < 2:
            return 0.0
        base = 0.08 + 0.06 * math.log2(distance + 2)
        return base * self.rng.uniform(0.75, 1.35) / self.speed

    def _path(self, start: tuple[int, int], end: tuple[int, int],
              steps: int) -> list[tuple[int, int]]:
        """Cubic Bezier whose control points bow off the straight line.

        The bow is perpendicular to the travel direction and scales with
        distance, so short corrections stay nearly straight and long sweeps
        across the screen arc the way an arm does.
        """
        x0, y0 = start
        x1, y1 = end
        dx, dy = x1 - x0, y1 - y0
        distance = math.hypot(dx, dy) or 1.0
        # Unit normal to the direction of travel.
        nx, ny = -dy / distance, dx / distance
        bow = distance * self.rng.uniform(0.03, 0.13) * self.rng.choice((-1, 1))

        c1 = (x0 + dx * 0.3 + nx * bow * self.rng.uniform(0.6, 1.0),
              y0 + dy * 0.3 + ny * bow * self.rng.uniform(0.6, 1.0))
        c2 = (x0 + dx * 0.7 + nx * bow * self.rng.uniform(0.6, 1.0),
              y0 + dy * 0.7 + ny * bow * self.rng.uniform(0.6, 1.0))

        points = []
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            bx = (u ** 3 * x0 + 3 * u * u * t * c1[0]
                  + 3 * u * t * t * c2[0] + t ** 3 * x1)
            by = (u ** 3 * y0 + 3 * u * u * t * c1[1]
                  + 3 * u * t * t * c2[1] + t ** 3 * y1)
            points.append((int(round(bx)), int(round(by))))
        points[-1] = (x1, y1)
        return points

    def move(self, x: int, y: int) -> None:
        start = self.position()
        distance = math.hypot(x - start[0], y - start[1])
        if distance < 1:
            return

        # Long moves sometimes sail past and come back, the way a real
        # correction does. Short ones never do.
        if distance > 260 and self.rng.random() < 0.22:
            over = self.rng.uniform(0.03, 0.09)
            way_past = (int(x + (x - start[0]) * over),
                        int(y + (y - start[1]) * over))
            self._trace(start, way_past, distance)
            time.sleep(self.rng.uniform(0.02, 0.06))
            start = self.position()
            distance = math.hypot(x - start[0], y - start[1])

        self._trace(start, (x, y), distance)

    def _trace(self, start: tuple[int, int], end: tuple[int, int],
               distance: float) -> None:
        steps = max(6, min(70, int(distance / 9) + self.rng.randint(2, 6)))
        duration = self._duration(distance)
        points = self._path(start, end, steps)

        for i, (px, py) in enumerate(points):
            self._jump(px, py)
            if duration <= 0:
                continue
            # Ease in and out: the cursor is slowest at both ends, so the
            # per-step sleep is longest there.
            t = (i + 1) / steps
            weight = 0.5 - 0.5 * math.cos(2 * math.pi * t)
            step_time = (duration / steps) * (0.45 + 1.1 * weight)
            time.sleep(max(0.0005, step_time * self.rng.uniform(0.8, 1.2)))

    # -- clicks -----------------------------------------------------

    def click(self, x: int | None = None, y: int | None = None,
              button: str = "left") -> None:
        if x is not None and y is not None:
            self.move(x, y)
            # Settle before pressing. Clicking the same millisecond the
            # cursor lands is the single most obviously non-human thing a
            # bot can do, and it also races the client's hover handling.
            time.sleep(self.rng.uniform(0.03, 0.12))

        down, up = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]

        _send(_mouse_event(down))
        time.sleep(self.rng.uniform(0.045, 0.115))
        _send(_mouse_event(up))

    def scroll(self, clicks: int) -> None:
        """Positive scrolls up, negative down."""
        for _ in range(abs(clicks)):
            _send(_mouse_event(MOUSEEVENTF_WHEEL,
                               data=120 if clicks > 0 else -120))
            time.sleep(self.rng.uniform(0.04, 0.11))

    def drift(self, radius: int = 60) -> None:
        """Nudge the cursor somewhere nearby, for the idle stretches."""
        x, y = self.position()
        self.move(x + self.rng.randint(-radius, radius),
                  y + self.rng.randint(-radius, radius))


class Keyboard:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def press(self, key: str) -> None:
        """Tap a named key - 'escape', 'f4', 'space', or a single character."""
        self.down(key)
        time.sleep(self.rng.uniform(0.04, 0.11))
        self.up(key)

    def down(self, key: str) -> None:
        vk = self._vk(key)
        scan = _user32.MapVirtualKeyW(vk, 0)
        flags = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0
        _send(_key_event(vk, scan, flags))

    def up(self, key: str) -> None:
        vk = self._vk(key)
        scan = _user32.MapVirtualKeyW(vk, 0)
        flags = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0
        _send(_key_event(vk, scan, flags | KEYEVENTF_KEYUP))

    @contextlib.contextmanager
    def holding(self, key: str):
        """Hold a modifier for the duration of a block.

        Shift-dropping an inventory needs the key held across many clicks,
        and the release has to happen even if a click throws - a stuck
        shift key would turn every later click into a shift-click.
        """
        self.down(key)
        time.sleep(self.rng.uniform(0.05, 0.12))
        try:
            yield
        finally:
            self.up(key)

    def _vk(self, key: str) -> int:
        key = key.lower()
        if key in VK:
            return VK[key]
        if len(key) == 1:
            result = _user32.VkKeyScanW(ctypes.c_wchar(key))
            if result == -1:
                raise ValueError(f"No virtual key for {key!r}")
            return result & 0xFF
        raise ValueError(f"Unknown key {key!r}")

    def type_text(self, text: str, enter: bool = False) -> None:
        """Type a string as unicode events, at a hand's pace.

        Unicode events rather than scancodes, so the result does not depend
        on the active keyboard layout - a bank search for 'Yew logs' should
        not break because the machine is set to AZERTY.
        """
        for char in text:
            code = ord(char)
            _send(_key_event(0, code, KEYEVENTF_UNICODE))
            time.sleep(self.rng.uniform(0.012, 0.05))
            _send(_key_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            # Real typing is bursty: mostly fast, with occasional pauses.
            delay = self.rng.gauss(0.075, 0.03)
            if self.rng.random() < 0.06:
                delay += self.rng.uniform(0.1, 0.35)
            time.sleep(max(0.015, delay))
        if enter:
            time.sleep(self.rng.uniform(0.08, 0.2))
            self.press("enter")


# ---------------------------------------------------------- focus

def focus_window_at(x: int, y: int) -> bool:
    """Bring whatever window sits under this screen point to the front.

    Input goes to the focused window, so a bot that has lost focus is a bot
    clicking someone else's application. Finding the window by point rather
    than by title means this does not care what the client calls itself.
    """
    point = wintypes.POINT(x, y)
    hwnd = _user32.WindowFromPoint(point)
    if not hwnd:
        return False
    root = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    if _user32.GetForegroundWindow() == root:
        return True
    _user32.SetForegroundWindow(root)
    time.sleep(0.25)
    return _user32.GetForegroundWindow() == root
