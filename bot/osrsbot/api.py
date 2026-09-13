"""Acting on the game.

This is the layer scripts are written against. It owns one idea worth
stating plainly, because everything else follows from it:

    Never click at a coordinate and hope. Point at the thing, ask the
    client what a click would actually do, and only then commit.

A pixel bot cannot do that, which is why pixel bots chop at the empty
square where a tree used to be, or drop a rune scimitar into a fire. Here
the client answers the question directly, so a mis-click becomes a retry
instead of a disaster - and the check costs about 90ms.
"""
from __future__ import annotations

import random
import time
from typing import Callable, Iterable, Sequence

from .input import Keyboard, Mouse, focus_window_at
from .state import Box, Entity, Menu, State, StateClient

TICK = 0.6  # One game tick, in seconds. Everything in OSRS is a multiple.


class ActionFailed(RuntimeError):
    """We tried to do a thing and could not confirm it happened."""


class Game:
    """State plus input, with the retry logic scripts should not rewrite."""

    def __init__(self, client: StateClient, mouse: Mouse | None = None,
                 keyboard: Keyboard | None = None,
                 rng: random.Random | None = None, log=print):
        self.client = client
        self.rng = rng or random.Random()
        self.mouse = mouse or Mouse(self.rng)
        self.keyboard = keyboard or Keyboard(self.rng)
        self.log = log
        self._state: State | None = None

    # -- state ------------------------------------------------------

    @property
    def state(self) -> State:
        if self._state is None:
            return self.refresh()
        return self._state

    def refresh(self) -> State:
        self._state = self.client.fetch()
        return self._state

    def invalidate(self) -> None:
        """Force the next read to hit the plugin. Call after acting."""
        self._state = None

    # -- geometry ---------------------------------------------------

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Canvas coordinates to desktop coordinates.

        The plugin reports where the canvas itself sits on screen, so this
        stays right when the window moves, and there is no window-hunting
        or template-matching anywhere in this codebase.
        """
        canvas = self.state.canvas
        if not canvas.get("onScreen"):
            raise ActionFailed("The client canvas is not on screen - is "
                               "RuneLite minimised?")
        # Stretched Mode draws the game small and scales the canvas up, so
        # every coordinate the client reports is in the unstretched space.
        # The factor is 1.0 when stretching is off, which is most of the time.
        scale_x = float(canvas.get("scaleX", 1.0) or 1.0)
        scale_y = float(canvas.get("scaleY", 1.0) or 1.0)
        return (int(canvas["screenX"]) + int(round(x * scale_x)),
                int(canvas["screenY"]) + int(round(y * scale_y)))

    def box_to_screen(self, box: Box) -> tuple[int, int]:
        return self.to_screen(*box.point(self.rng))

    def ensure_focus(self) -> bool:
        """Input goes to the focused window, so make sure that is the game."""
        state = self.refresh()
        if state.focused:
            return True
        canvas = state.canvas
        if not canvas.get("onScreen"):
            return False
        centre = (int(canvas["screenX"]) + int(canvas.get("width", 400)) // 2,
                  int(canvas["screenY"]) + int(canvas.get("height", 300)) // 2)
        ok = focus_window_at(*centre)
        if ok:
            time.sleep(self.rng.uniform(0.15, 0.35))
        return ok

    # -- the core interaction ---------------------------------------

    def hover(self, box: Box) -> Menu:
        """Move onto a target and read back what a click would do."""
        self.mouse.move(*self.box_to_screen(box))
        # The client needs a frame or two to rebuild its menu for the new
        # cursor position. Reading sooner just returns the previous target.
        time.sleep(self.rng.uniform(0.06, 0.14))
        return self.client.menu()

    def interact(self, target: Entity | Box, option: str,
                 name: str | None = None, tries: int = 3) -> bool:
        """Perform `option` on a thing. The workhorse.

        Tries a plain left click when the client agrees that is what a
        click would do, and falls back to the right-click menu when it
        does not - the same two moves a player makes, in the same order.
        """
        box = target.box if isinstance(target, Entity) else target
        if box is None:
            self.log(f"  ! {option}: nothing clickable to aim at")
            return False
        if name is None and isinstance(target, Entity):
            name = target.name

        for attempt in range(1, tries + 1):
            menu = self.hover(box)

            if menu.matches(option, name or ""):
                self.mouse.click()
                self.invalidate()
                self.log(f"  {option} {name or ''}".rstrip())
                return True

            # The thing we want is on the menu, just not on top - which is
            # normal for stacked objects, or an NPC standing on an item.
            if menu.index_of(option, name or "") is not None:
                if self._right_click_select(option, name or ""):
                    return True

            top = menu.top
            self.log(f"  ~ hover {attempt}/{tries} gave {top[0]!r} "
                     f"{top[1]!r}, wanted {option!r} {name or ''!r}")
            # The target moved, or the camera did. A fresh read gives a
            # fresh hitbox to aim at.
            if isinstance(target, Entity) and name:
                refreshed = self._relocate(target, name)
                if refreshed is not None and refreshed.box is not None:
                    box = refreshed.box
            time.sleep(self.rng.uniform(0.1, 0.3))

        return False

    def _relocate(self, previous: Entity, name: str) -> Entity | None:
        state = self.refresh()
        pool = {"npc": state.npcs, "object": state.objects,
                "item": state.ground_items}.get(previous.kind, [])
        return state.nearest(pool, name)

    def _right_click_select(self, option: str, name: str = "",
                            timeout: float = 1.5) -> bool:
        """Open the menu where the cursor is and click a row by name."""
        self.mouse.click(button="right")
        deadline = time.time() + timeout
        menu = Menu({})
        while time.time() < deadline:
            time.sleep(0.06)
            menu = self.client.menu()
            if menu.open:
                break
        if not menu.open:
            self.log("  ! right click did not open a menu")
            return False

        index = menu.index_of(option, name)
        row = menu.row_box(index) if index is not None else None
        if row is None:
            self.log(f"  ! {option!r} not on the menu; closing it")
            self.keyboard.press("escape")
            return False

        self.mouse.click(*self.to_screen(*row.point(self.rng)))
        self.invalidate()
        self.log(f"  {option} {name} (menu)".rstrip())
        return True

    # -- interfaces -------------------------------------------------

    def click_widget(self, group: int, child: int = 0, sub: int = -1,
                     option: str | None = None) -> bool:
        """Click an interface component, looked up live by address."""
        box = self.client.widget_box(group, child, sub)
        if box is None:
            self.log(f"  ! widget {group}.{child}"
                     f"{'' if sub < 0 else f'[{sub}]'} is not on screen")
            return False
        if option:
            return self.interact(box, option, tries=2)
        self.mouse.click(*self.box_to_screen(box))
        self.invalidate()
        return True

    def inventory_box(self, slot: int) -> Box | None:
        """Where inventory slot `slot` is drawn, right now."""
        return self.client.widget_box(*INVENTORY_CONTAINER, sub=slot)

    def interact_inventory(self, item: str, option: str) -> bool:
        """Do something with the first inventory item matching a name."""
        found = self.state.inventory.find_matching(item)
        if found is None:
            self.log(f"  ! no {item} in the inventory")
            return False
        box = self.inventory_box(found.slot)
        if box is None:
            return False
        return self.interact(box, option, found.name)

    def use_on(self, item: str, target: Entity | Box,
               target_name: str | None = None) -> bool:
        """Use an inventory item on something else - the crafting verb."""
        if not self.interact_inventory(item, "Use"):
            return False
        time.sleep(self.rng.uniform(0.15, 0.35))
        name = target_name or (target.name if isinstance(target, Entity) else "")
        # With an item selected, every menu entry becomes "Use item ->".
        return self.interact(target, "Use", name, tries=2)

    def press(self, key: str) -> None:
        self.keyboard.press(key)
        self.invalidate()

    def close_interface(self) -> None:
        self.press("escape")

    # -- movement ---------------------------------------------------

    def walk_to(self, point: Sequence[int], tolerance: int = 3,
                timeout: float = 30.0) -> bool:
        """Click the minimap to walk somewhere, and wait until we arrive.

        The minimap only reaches so far, so a distant target is walked in
        hops: click the furthest projectable point toward it, repeat.
        """
        deadline = time.time() + timeout
        x, y = point[0], point[1]
        plane = point[2] if len(point) > 2 else 0

        while time.time() < deadline:
            state = self.refresh()
            if state.distance_to((x, y, plane)) <= tolerance:
                return True

            spot = self.client.project(x, y, plane)
            minimap = Box.parse(spot.get("minimap"))
            if minimap is None:
                self.log("  ! target is off the minimap and no route is known")
                return False

            self.mouse.click(*self.to_screen(*minimap.point(self.rng)))
            self.invalidate()
            # Let the step actually happen before asking again.
            self.wait_until(lambda s: not s.moving, timeout=8.0, poll=0.4)

        self.log("  ! walk timed out")
        return False

    def set_run(self, on: bool = True) -> bool:
        """Toggle run, if it is not already how we want it."""
        state = self.refresh()
        if state.running == on:
            return True
        if on and state.run_energy < 5:
            return False
        return self.click_widget(*RUN_ORB)

    # -- waiting ----------------------------------------------------

    def wait_until(self, predicate: Callable[[State], bool],
                   timeout: float = 10.0, poll: float = 0.3) -> bool:
        """Poll until the game agrees something happened."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self.refresh()):
                return True
            time.sleep(poll * self.rng.uniform(0.8, 1.25))
        return False

    def wait_ticks(self, ticks: float) -> None:
        time.sleep(TICK * ticks * self.rng.uniform(0.9, 1.15))

    def wait_for_idle(self, timeout: float = 30.0) -> bool:
        """Wait until the character stops doing whatever it is doing.

        Deliberately requires the idle state to hold across two reads.
        Skilling animations have a one-tick gap between swings, and a
        single-sample check treats that gap as "finished" - which is how a
        woodcutting bot ends up re-clicking the same tree forever.
        """
        deadline = time.time() + timeout
        seen_idle = False
        while time.time() < deadline:
            state = self.refresh()
            if state.idle:
                if seen_idle:
                    return True
                seen_idle = True
            else:
                seen_idle = False
            time.sleep(TICK * self.rng.uniform(0.75, 1.1))
        return False

    # -- convenience ------------------------------------------------

    def watch(self, objects: Iterable[str] = (), npcs: Iterable[str] = (),
              items: Iterable[str] = ()) -> None:
        self.client.watch(objects=objects, npcs=npcs, items=items)


# Interface addresses. These are the only magic numbers in the Python half,
# and they are here rather than scattered through the scripts so that a
# client update moves one line. Find replacements with RuneLite's widget
# inspector (Dev Tools), or `python -m osrsbot.cli inspect --group N`.
INVENTORY_CONTAINER = (149, 0)
RUN_ORB = (160, 27)
LOGOUT_BUTTON = (182, 8)
BANK_CLOSE = (12, 3)
BANK_DEPOSIT_INVENTORY = (12, 44)
BANK_SEARCH = (12, 42)
BANK_ITEM_CONTAINER = (12, 13)
DIALOGUE_CONTINUE = (231, 5)
DIALOGUE_OPTIONS = (219, 1)
