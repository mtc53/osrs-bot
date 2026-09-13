"""Gathering: chop, mine, fish, until the bag is full, then empty it.

The shape every gathering loop shares, with the differences pushed into
config: what to click, what action to use, and what to do when full. A
fishing spot is an NPC and an oak tree is a scene object, so both lists
get searched - that distinction is the game's, not something a script
should have to care about.
"""
from __future__ import annotations

import time

from ..bank import BANK_NAMES, Bank
from ..state import Entity
from ..task import Context, Stop, Task

# How long the character must read as idle before we believe the resource
# is gone. Skilling animations have brief gaps, and re-clicking during one
# is how a woodcutting bot ends up resetting its own swing forever.
IDLE_GRACE = 1.2


class ClickResource(Task):
    name = "gather"

    def __init__(self, names: list[str], action: str):
        self.names = names
        self.action = action
        self._idle_since: float | None = None

    def _target(self, ctx: Context) -> Entity | None:
        state = ctx.state
        return (state.nearest_object(self.names)
                or state.nearest_npc(self.names))

    def should_run(self, ctx: Context) -> bool:
        state = ctx.state
        if state.inventory.full or state.any_interface_open:
            self._idle_since = None
            return False

        if not state.idle:
            self._idle_since = None
            return False
        if self._idle_since is None:
            self._idle_since = time.time()
        if time.time() - self._idle_since < IDLE_GRACE:
            return False

        return self._target(ctx) is not None

    def run(self, ctx: Context) -> None:
        target = self._target(ctx)
        if target is None:
            return
        self._idle_since = None
        before = len(ctx.state.inventory)

        if not ctx.game.interact(target, self.action, target.name):
            ctx.stats.count("missed clicks")
            return

        # Confirm the click took: either we started the animation, or we
        # are walking to it. If neither happens, the click went nowhere and
        # the next loop should try again rather than wait out a timeout.
        ctx.game.wait_until(
            lambda s: s.animating or s.moving or len(s.inventory) != before,
            timeout=4.0, poll=0.2)
        ctx.stats.count("gathered")


class DropInventory(Task):
    """Shift-drop everything that is not on the keep list."""

    name = "drop"

    def __init__(self, keep: list[str]):
        self.keep = [k.lower() for k in keep]

    def _droppable(self, ctx: Context) -> list:
        return [i for i in ctx.state.inventory.items
                if not any(k in i.name.lower() for k in self.keep)]

    def should_run(self, ctx: Context) -> bool:
        return ctx.state.inventory.full and bool(self._droppable(ctx))

    def run(self, ctx: Context) -> None:
        game = ctx.game
        items = self._droppable(ctx)
        ctx.log(f"  dropping {len(items)} items")

        # Shift stays down for the whole pass - `holding` releases it even
        # if a click throws, because a stuck shift turns every later click
        # in the session into a shift-click.
        with game.keyboard.holding("shift"):
            for item in items:
                box = game.inventory_box(item.slot)
                if box is None:
                    continue
                menu = game.hover(box)
                if not menu.matches("Drop", item.name):
                    # Shift-click is only "Drop" when the client agrees it
                    # is. If it does not, skip rather than click blind -
                    # the alternative is dropping the wrong thing.
                    ctx.log(f"  ~ skipping {item.name}: shift-click is "
                            f"{menu.top[0]!r}, not Drop")
                    continue
                game.mouse.click()
                ctx.stats.count("dropped")
                time.sleep(ctx.rng.uniform(0.08, 0.22))

        game.invalidate()


class BankInventory(Task):
    """Walk to a bank, deposit the haul, come back."""

    name = "bank"

    def __init__(self, keep: list[str], bank_names: list[str],
                 bank_tile=None, work_tile=None):
        self.keep = [k.lower() for k in keep]
        self.bank_names = bank_names
        self.bank_tile = bank_tile
        self.work_tile = work_tile

    def should_run(self, ctx: Context) -> bool:
        return ctx.state.inventory.full or ctx.state.bank_open

    def run(self, ctx: Context) -> None:
        game = ctx.game

        if not game.state.bank_open:
            if Bank.find(ctx) is None and self.bank_tile:
                ctx.log("  walking to the bank")
                if not game.walk_to(self.bank_tile):
                    raise Stop("could not reach the bank")
            if not Bank.open(ctx, self.bank_names):
                return

        deposited = 0
        for item in list(game.state.inventory.items):
            if any(k in item.name.lower() for k in self.keep):
                continue
            if Bank.deposit(ctx, item.name):
                deposited += 1

        ctx.log(f"  banked {deposited} stacks")
        Bank.close(ctx)

        if self.work_tile:
            ctx.log("  walking back")
            game.walk_to(self.work_tile)


def build(ctx: Context) -> list[Task]:
    """Assemble the gathering task list from config."""
    names = ctx.opt("script.resources") or []
    if not names:
        raise Stop("script.resources is empty - nothing to gather")

    action = ctx.opt("script.action", "Chop down")
    keep = ctx.opt("script.keep") or []
    bank_names = ctx.opt("script.bank_names") or list(BANK_NAMES)

    # The plugin exports nothing it was not asked for, so declare interest
    # in every name this script might click before the loop starts.
    ctx.game.watch(objects=names + bank_names, npcs=names + bank_names)

    tasks: list[Task] = []
    when_full = str(ctx.opt("script.when_full", "drop")).lower()
    if when_full == "bank":
        tasks.append(BankInventory(keep, bank_names,
                                   ctx.opt("script.bank_tile"),
                                   ctx.opt("script.work_tile")))
    else:
        tasks.append(DropInventory(keep))

    tasks.append(ClickResource(names, action))
    return tasks
