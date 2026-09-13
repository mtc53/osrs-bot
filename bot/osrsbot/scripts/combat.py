"""Combat: attack things, eat, pick up what they drop.

The interesting problem here is not attacking - it is knowing when *not*
to. An NPC already fighting someone else is not yours to take, and a
script that attacks anyway spends the session stealing kills and getting
nothing. The plugin reports who each NPC is interacting with, so that
check is one field rather than a guess about health bars.
"""
from __future__ import annotations

from ..state import Entity
from ..task import Context, Task


class Attack(Task):
    name = "attack"

    def __init__(self, names: list[str], max_distance: int = 10):
        self.names = names
        self.max_distance = max_distance

    def _target(self, ctx: Context) -> Entity | None:
        state = ctx.state
        me = state.name
        for npc in state.npcs:
            if not any(n.lower() in npc.name.lower() for n in self.names):
                continue
            if npc.dead or not npc.clickable or npc.distance > self.max_distance:
                continue
            # Someone else's fight. health_ratio > 0 with another player as
            # its target means it is already engaged.
            if npc.interacting and npc.interacting != me:
                continue
            return npc
        return None

    def should_run(self, ctx: Context) -> bool:
        state = ctx.state
        if state.any_interface_open:
            return False
        # Already fighting: leave it alone.
        if state.interacting_with or state.animating:
            return False
        if state.inventory.full:
            return False
        return self._target(ctx) is not None

    def run(self, ctx: Context) -> None:
        target = self._target(ctx)
        if target is None:
            return
        if ctx.game.interact(target, "Attack", target.name):
            ctx.stats.count("attacks")
            ctx.game.wait_until(
                lambda s: s.interacting_with is not None or s.animating,
                timeout=6.0, poll=0.3)


class Loot(Task):
    """Pick up named drops lying nearby."""

    name = "loot"

    def __init__(self, names: list[str], max_distance: int = 6):
        self.names = names
        self.max_distance = max_distance

    def _target(self, ctx: Context) -> Entity | None:
        item = ctx.state.nearest_ground_item(self.names)
        if item is None or item.distance > self.max_distance:
            return None
        return item

    def should_run(self, ctx: Context) -> bool:
        state = ctx.state
        if not self.names or state.inventory.full or state.any_interface_open:
            return False
        return self._target(ctx) is not None

    def run(self, ctx: Context) -> None:
        target = self._target(ctx)
        if target is None:
            return
        before = len(ctx.state.inventory)
        if ctx.game.interact(target, "Take", target.name):
            if ctx.game.wait_until(lambda s: len(s.inventory) > before,
                                   timeout=6.0, poll=0.3):
                ctx.stats.count("looted")


class BuryBones(Task):
    """Bury bones rather than carry them. Optional, and off by default."""

    name = "bury"

    def __init__(self, names: list[str]):
        self.names = names

    def should_run(self, ctx: Context) -> bool:
        if ctx.state.animating or ctx.state.any_interface_open:
            return False
        return any(ctx.state.inventory.find_matching(n) for n in self.names)

    def run(self, ctx: Context) -> None:
        for name in self.names:
            item = ctx.state.inventory.find_matching(name)
            if item is None:
                continue
            if ctx.game.interact_inventory(item.name, "Bury"):
                ctx.stats.count("buried")
                ctx.game.wait_ticks(1.5)
            return


def build(ctx: Context) -> list[Task]:
    targets = ctx.opt("script.targets") or []
    if not targets:
        from ..task import Stop
        raise Stop("script.targets is empty - nothing to fight")

    loot = ctx.opt("script.loot") or []
    bury = ctx.opt("script.bury") or []
    food = ctx.opt("safety.food") or []

    ctx.game.watch(npcs=targets, items=loot + bury + food)

    tasks: list[Task] = []
    if loot:
        # Looting outranks attacking: drops disappear, monsters respawn.
        tasks.append(Loot(loot, int(ctx.opt("script.loot_distance", 6))))
    if bury:
        tasks.append(BuryBones(bury))
    tasks.append(Attack(targets, int(ctx.opt("script.max_distance", 10))))
    return tasks
