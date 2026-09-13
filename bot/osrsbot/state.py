"""Reading the game.

Everything the bot knows comes through here: one HTTP call to the State
Link plugin per decision, wrapped in accessors that answer the questions a
script actually asks - "is my inventory full", "where is the nearest live
chicken", "what would a left click do right now".

Nothing in this module clicks anything. Keeping reads and writes in
separate modules is what makes a script's logic testable against a
recorded snapshot.
"""
from __future__ import annotations

import json
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class StateError(RuntimeError):
    """The plugin could not be reached, or answered with something odd."""


@dataclass(frozen=True)
class Box:
    """A clickable rectangle in *canvas* coordinates."""

    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def point(self, rng: random.Random | None = None) -> tuple[int, int]:
        """A plausible click point inside the box.

        Not uniform: a normal distribution around the middle, clamped to the
        inner 70%. Uniform clicks inside a hitbox are a strange thing for a
        hand to produce - the corners of a tree trunk get hit as often as
        the middle - and the clamp also keeps us off the edge, where a
        one-pixel drift lands on whatever is behind the target.
        """
        rng = rng or random
        inset_w = max(1, int(self.w * 0.15))
        inset_h = max(1, int(self.h * 0.15))
        lo_x, hi_x = self.x + inset_w, self.x + self.w - inset_w
        lo_y, hi_y = self.y + inset_h, self.y + self.h - inset_h
        cx, cy = self.center
        x = int(rng.gauss(cx, max(1.0, self.w / 6)))
        y = int(rng.gauss(cy, max(1.0, self.h / 6)))
        return (min(max(x, lo_x), hi_x), min(max(y, lo_y), hi_y))

    @classmethod
    def parse(cls, raw: dict | None) -> "Box | None":
        if not raw:
            return None
        return cls(int(raw["x"]), int(raw["y"]), int(raw["w"]), int(raw["h"]))


@dataclass(frozen=True)
class Entity:
    """An NPC, scene object or ground item the plugin told us about."""

    kind: str
    name: str
    id: int
    distance: int
    box: Box | None
    raw: dict

    @property
    def clickable(self) -> bool:
        """On screen with a hitbox - i.e. we could click it right now."""
        return self.box is not None

    @property
    def actions(self) -> list[str]:
        return list(self.raw.get("actions") or [])

    @property
    def world_point(self) -> tuple[int, int, int] | None:
        wp = self.raw.get("worldPoint")
        if not wp:
            return None
        return int(wp["x"]), int(wp["y"]), int(wp["plane"])

    @property
    def dead(self) -> bool:
        return bool(self.raw.get("dead"))

    @property
    def health_ratio(self) -> int:
        return int(self.raw.get("healthRatio", -1))

    @property
    def interacting(self) -> str | None:
        return self.raw.get("interacting")

    @property
    def quantity(self) -> int:
        return int(self.raw.get("quantity", 1))


@dataclass(frozen=True)
class Item:
    slot: int
    id: int
    quantity: int
    name: str


class Container:
    """An inventory, equipment or bank container."""

    def __init__(self, raw: dict | None):
        self.raw = raw or {}
        self.items = [
            Item(int(i["slot"]), int(i["id"]), int(i["quantity"]), i.get("name", ""))
            for i in self.raw.get("items", [])
        ]

    def __len__(self) -> int:
        return int(self.raw.get("used", len(self.items)))

    @property
    def full(self) -> bool:
        return bool(self.raw.get("full"))

    @property
    def empty(self) -> bool:
        return bool(self.raw.get("empty", not self.items))

    @property
    def free_slots(self) -> int:
        return max(0, 28 - len(self))

    def count(self, name: str) -> int:
        """Total quantity of an item, matched case-insensitively."""
        want = name.lower()
        return sum(i.quantity for i in self.items if i.name.lower() == want)

    def contains(self, name: str) -> bool:
        return self.count(name) > 0

    def contains_any(self, names: Iterable[str]) -> bool:
        return any(self.contains(n) for n in names)

    def find(self, name: str) -> Item | None:
        want = name.lower()
        for item in self.items:
            if item.name.lower() == want:
                return item
        return None

    def find_matching(self, fragment: str) -> Item | None:
        want = fragment.lower()
        for item in self.items:
            if want in item.name.lower():
                return item
        return None

    def slots_matching(self, fragment: str) -> list[int]:
        want = fragment.lower()
        return [i.slot for i in self.items if want in i.name.lower()]

    def names(self) -> list[str]:
        return [i.name for i in self.items]


class Menu:
    """The right-click menu, or the options a left click is choosing between.

    Entries arrive top-first, so index 0 is the row a plain left click
    takes. That ordering is the reason this is worth a type of its own -
    RuneLite stores them the other way round.
    """

    HEADER_HEIGHT = 19

    def __init__(self, raw: dict):
        self.raw = raw or {}

    @property
    def open(self) -> bool:
        return bool(self.raw.get("open"))

    @property
    def entries(self) -> list[tuple[str, str]]:
        return [(e.get("option", ""), e.get("target", ""))
                for e in self.raw.get("entries") or []]

    @property
    def top(self) -> tuple[str, str]:
        entries = self.entries
        return entries[0] if entries else ("", "")

    @property
    def box(self) -> Box | None:
        return Box.parse(self.raw.get("bounds"))

    def index_of(self, option: str, target: str = "") -> int | None:
        """Find a row by action, and optionally by what it acts on."""
        want_option = option.lower()
        want_target = target.lower()
        for i, (opt, tgt) in enumerate(self.entries):
            if opt.lower() != want_option:
                continue
            if want_target and want_target not in tgt.lower():
                continue
            return i
        return None

    def matches(self, option: str, target: str = "") -> bool:
        """Would a left click here do the thing we intended?"""
        opt, tgt = self.top
        if opt.lower() != option.lower():
            return False
        return not target or target.lower() in tgt.lower()

    def row_box(self, index: int) -> Box | None:
        """Where to click for row `index` of an open menu."""
        box = self.box
        entries = self.entries
        if box is None or not entries or index >= len(entries):
            return None
        usable = box.h - self.HEADER_HEIGHT
        row_h = max(1, usable // len(entries))
        return Box(
            x=box.x + 2,
            y=box.y + self.HEADER_HEIGHT + index * row_h,
            w=max(1, box.w - 4),
            h=row_h,
        )


class State:
    """One snapshot, with the questions a script wants to ask attached."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.inventory = Container(raw.get("inventory"))
        self.equipment = Container(raw.get("equipment"))
        self.bank = Container(raw.get("bank"))

    # -- session ----------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self.raw.get("loggedIn"))

    @property
    def game_state(self) -> str:
        return str(self.raw.get("gameState", "UNKNOWN"))

    @property
    def tick(self) -> int:
        return int(self.raw.get("tick", 0))

    @property
    def canvas(self) -> dict:
        return self.raw.get("canvas") or {}

    @property
    def on_screen(self) -> bool:
        return bool(self.canvas.get("onScreen"))

    @property
    def focused(self) -> bool:
        return bool(self.canvas.get("focused"))

    # -- player -----------------------------------------------------

    @property
    def player(self) -> dict:
        return self.raw.get("player") or {}

    @property
    def name(self) -> str:
        return str(self.player.get("name", ""))

    @property
    def animation(self) -> int:
        return int(self.player.get("animation", -1))

    @property
    def animating(self) -> bool:
        return self.animation != -1

    @property
    def moving(self) -> bool:
        return bool(self.player.get("moving"))

    @property
    def idle(self) -> bool:
        """Not animating and not walking - the signal most loops turn on."""
        return bool(self.player.get("idle"))

    @property
    def interacting_with(self) -> str | None:
        target = self.player.get("interacting")
        return target.get("name") if target else None

    @property
    def position(self) -> tuple[int, int, int] | None:
        wp = self.player.get("worldPoint")
        if not wp:
            return None
        return int(wp["x"]), int(wp["y"]), int(wp["plane"])

    def distance_to(self, point: Sequence[int]) -> int:
        """Chebyshev distance, which is how the game measures tiles."""
        here = self.position
        if here is None:
            return 10 ** 6
        if len(point) >= 3 and point[2] != here[2]:
            return 10 ** 6
        return max(abs(here[0] - point[0]), abs(here[1] - point[1]))

    # -- vitals -----------------------------------------------------

    @property
    def vitals(self) -> dict:
        return self.raw.get("vitals") or {}

    @property
    def hitpoints(self) -> int:
        return int(self.vitals.get("hitpoints", 0))

    @property
    def max_hitpoints(self) -> int:
        return int(self.vitals.get("maxHitpoints", 0))

    @property
    def hp_percent(self) -> int:
        return int(self.vitals.get("hitpointsPercent", 100))

    @property
    def prayer(self) -> int:
        return int(self.vitals.get("prayer", 0))

    @property
    def run_energy(self) -> int:
        return int(self.vitals.get("runEnergy", 0))

    @property
    def running(self) -> bool:
        return bool(self.vitals.get("running"))

    @property
    def special_attack(self) -> int:
        return int(self.vitals.get("specialAttack", 0))

    # -- skills -----------------------------------------------------

    @property
    def skills(self) -> dict:
        return self.raw.get("skills") or {}

    def level(self, skill: str) -> int:
        return int(self.skills.get(skill.lower(), {}).get("level", 0))

    def xp(self, skill: str) -> int:
        return int(self.skills.get(skill.lower(), {}).get("xp", 0))

    # -- interfaces -------------------------------------------------

    @property
    def widgets(self) -> dict:
        return self.raw.get("widgets") or {}

    @property
    def bank_open(self) -> bool:
        return bool(self.widgets.get("bank"))

    @property
    def bank_pin_open(self) -> bool:
        return bool(self.widgets.get("bankPin"))

    @property
    def level_up_open(self) -> bool:
        return bool(self.widgets.get("levelUp"))

    @property
    def logout_open(self) -> bool:
        return bool(self.widgets.get("logout"))

    @property
    def dialogue_open(self) -> bool:
        w = self.widgets
        return bool(w.get("dialogueNpc") or w.get("dialoguePlayer")
                    or w.get("dialogueSprite"))

    @property
    def dialogue_options_open(self) -> bool:
        return bool(self.widgets.get("dialogueOption"))

    @property
    def dialogue_text(self) -> str:
        return str(self.widgets.get("dialogueText", ""))

    @property
    def production_open(self) -> bool:
        return bool(self.widgets.get("production"))

    @property
    def any_interface_open(self) -> bool:
        return (self.bank_open or self.dialogue_open or self.level_up_open
                or self.dialogue_options_open or self.production_open)

    # -- menu -------------------------------------------------------

    @property
    def menu(self) -> dict:
        return self.raw.get("menu") or {}

    @property
    def menu_open(self) -> bool:
        return bool(self.menu.get("open"))

    @property
    def menu_entries(self) -> list[dict]:
        return list(self.menu.get("entries") or [])

    @property
    def menu_top(self) -> tuple[str, str]:
        """What a plain left click at the cursor would do, right now."""
        entries = self.menu_entries
        if not entries:
            return ("", "")
        return (entries[0].get("option", ""), entries[0].get("target", ""))

    @property
    def menu_box(self) -> Box | None:
        return Box.parse(self.menu.get("bounds"))

    # -- entities ---------------------------------------------------

    def _entities(self, key: str, kind: str) -> list[Entity]:
        out = []
        for raw in self.raw.get(key) or []:
            out.append(Entity(
                kind=kind,
                name=str(raw.get("name", "")),
                id=int(raw.get("id", -1)),
                distance=int(raw.get("distance", 999)),
                box=Box.parse(raw.get("clickbox")),
                raw=raw,
            ))
        return out

    @property
    def npcs(self) -> list[Entity]:
        return self._entities("npcs", "npc")

    @property
    def objects(self) -> list[Entity]:
        return self._entities("objects", "object")

    @property
    def ground_items(self) -> list[Entity]:
        return self._entities("groundItems", "item")

    @property
    def players(self) -> list[dict]:
        return list(self.raw.get("players") or [])

    def nearest(self, candidates: list[Entity], names: Iterable[str] | str,
                clickable_only: bool = True) -> Entity | None:
        """Closest entity whose name matches, already sorted by distance."""
        if isinstance(names, str):
            names = [names]
        wants = [n.lower() for n in names]
        for entity in candidates:
            low = entity.name.lower()
            if not any(w == low or w in low for w in wants):
                continue
            if clickable_only and not entity.clickable:
                continue
            return entity
        return None

    def nearest_npc(self, names, alive: bool = True, **kw) -> Entity | None:
        pool = [n for n in self.npcs if not (alive and n.dead)]
        return self.nearest(pool, names, **kw)

    def nearest_object(self, names, **kw) -> Entity | None:
        return self.nearest(self.objects, names, **kw)

    def nearest_ground_item(self, names, **kw) -> Entity | None:
        return self.nearest(self.ground_items, names, **kw)

    def attackers(self) -> list[dict]:
        return [p for p in self.players if p.get("interactingWithMe")]

    # -- chat -------------------------------------------------------

    @property
    def chat(self) -> list[dict]:
        return list(self.raw.get("chat") or [])

    def said_recently(self, fragment: str, within_ms: int = 6000) -> bool:
        """Did a game message containing this text arrive in the last few seconds?

        The game narrates a lot of failure states only in the chatbox -
        "You do not have enough inventory space", "You need a Woodcutting
        level of 60" - so scripts read it to notice they are stuck.
        """
        now = int(self.raw.get("timestamp", 0))
        want = fragment.lower()
        for message in self.chat:
            if want not in str(message.get("message", "")).lower():
                continue
            if now - int(message.get("timestamp", 0)) <= within_ms:
                return True
        return False


class StateClient:
    """HTTP client for the State Link plugin."""

    def __init__(self, port: int = 8095, token: str = "", timeout: float = 3.0,
                 host: str = "127.0.0.1"):
        self.base = f"http://{host}:{port}"
        self.token = token or ""
        self.timeout = timeout

    def _get(self, path: str, **params: Any) -> dict:
        if self.token:
            params["token"] = self.token
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url)
        if self.token:
            request.add_header("X-Token", self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise StateError(f"{path} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise StateError(
                f"Could not reach the State Link plugin at {self.base} - is "
                f"RuneLite running with the plugin enabled? ({exc.reason})"
            ) from exc
        except json.JSONDecodeError as exc:
            raise StateError(f"{path} did not return JSON") from exc

    def ping(self) -> bool:
        try:
            return bool(self._get("/ping").get("ok"))
        except StateError:
            return False

    def fetch(self) -> State:
        return State(self._get("/state"))

    def watch(self, objects: Iterable[str] = (), npcs: Iterable[str] = (),
              items: Iterable[str] = ()) -> dict:
        """Tell the plugin which entity names are worth exporting.

        Scene objects are not exported at all until asked for, because
        scanning every tile around the player each tick for nobody is the
        one thing on the plugin side that would cost real frames.
        """
        return self._get(
            "/watch",
            objects=",".join(objects),
            npcs=",".join(npcs),
            items=",".join(items),
        )

    def menu(self) -> "Menu":
        """The right-click menu as it is *right now*, not as of last tick.

        Hovering and then reading the tick snapshot would add up to 600ms to
        every interaction, so this reads through to the client on demand.
        """
        return Menu(self._get("/menu"))

    def project(self, x: int, y: int, plane: int = 0) -> dict:
        """Where a world tile is on the canvas and on the minimap.

        Only tiles in the loaded scene project at all, so anything further
        than about fifty tiles away has to be reached in hops.
        """
        return self._get("/project", x=x, y=y, plane=plane)

    def widget(self, group: int, child: int = 0, sub: int = -1) -> dict:
        """Live widget lookup - bounds, text and item id for one component."""
        return self._get("/widget", group=group, child=child,
                         sub=(sub if sub >= 0 else ""))

    def widget_box(self, group: int, child: int = 0, sub: int = -1) -> Box | None:
        data = self.widget(group, child, sub)
        if not data.get("found") or data.get("hidden"):
            return None
        return Box.parse(data.get("bounds"))
