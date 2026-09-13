"""Processing: withdraw materials, make things, bank the results, repeat.

Covers the three shapes almost every processing skill takes:

    use_on_item    needle on leather, chisel on gems
    use_on_object  ore on a furnace, logs on a fire
    object         click a spinning wheel, a range, a loom

They differ only in what gets clicked first, so they share one task and
branch on a config key. What they all share is the make-X interface that
appears afterwards, and the wait for the inventory to be consumed - that
wait is the part scripts get wrong, because "am I still crafting" has no
animation you can rely on across skills. Watching the materials go down
works everywhere.
"""
from __future__ import annotations

from ..bank import BANK_NAMES, Bank
from ..task import Context, Stop, Task


class Restock(Task):
    """Bank the products, take out more materials."""

    name = "restock"

    def __init__(self, materials: list[dict], products: list[str],
                 bank_names: list[str], bank_tile=None):
        self.materials = materials
        self.products = [p.lower() for p in products]
        self.bank_names = bank_names
        self.bank_tile = bank_tile

    def _has_materials(self, ctx: Context) -> bool:
        return all(ctx.state.inventory.count(m["item"]) >= int(m.get("keep_at", 1))
                   for m in self.materials)

    def should_run(self, ctx: Context) -> bool:
        if ctx.state.bank_open:
            return True
        return not self._has_materials(ctx)

    def run(self, ctx: Context) -> None:
        game = ctx.game

        if not game.state.bank_open:
            if Bank.find(ctx) is None and self.bank_tile:
                ctx.log("  walking to the bank")
                if not game.walk_to(self.bank_tile):
                    raise Stop("could not reach the bank")
            if not Bank.open(ctx, self.bank_names):
                return

        # Products out first, so there is room for what comes back in.
        for name in list(p.name for p in game.state.inventory.items):
            if any(p in name.lower() for p in self.products):
                Bank.deposit(ctx, name)

        for spec in self.materials:
            item = spec["item"]
            want = spec.get("quantity", "all")
            have = game.state.inventory.count(item)
            if have >= int(spec.get("keep_at", 1)):
                continue
            if not Bank.withdraw(ctx, item, want):
                raise Stop(f"out of {item} in the bank")

        Bank.close(ctx)


class Produce(Task):
    """Kick off one batch of whatever we are making."""

    name = "produce"

    def __init__(self, mode: str, tool: str, material: str,
                 target_object: str = "", action: str = "Use"):
        self.mode = mode
        self.tool = tool
        self.material = material
        self.target_object = target_object
        self.action = action

    def should_run(self, ctx: Context) -> bool:
        state = ctx.state
        if state.any_interface_open or state.animating or state.moving:
            return False
        return state.inventory.contains(self.material) or self.mode == "object"

    def run(self, ctx: Context) -> None:
        game = ctx.game
        started = False

        if self.mode == "use_on_item":
            target = game.state.inventory.find_matching(self.material)
            if target is None:
                return
            box = game.inventory_box(target.slot)
            if box is None:
                return
            started = game.use_on(self.tool, box, target.name)

        elif self.mode == "use_on_object":
            obj = game.state.nearest_object(self.target_object)
            if obj is None:
                ctx.log(f"  ! no {self.target_object} in sight")
                return
            started = game.use_on(self.material, obj, obj.name)

        elif self.mode == "object":
            obj = game.state.nearest_object(self.target_object)
            if obj is None:
                ctx.log(f"  ! no {self.target_object} in sight")
                return
            started = game.interact(obj, self.action, obj.name)

        else:
            raise Stop(f"unknown script.mode {self.mode!r}")

        if not started:
            ctx.stats.count("missed clicks")
            return

        self._confirm(ctx)

    def _confirm(self, ctx: Context) -> None:
        """Answer the make-X interface, then wait for the batch to finish."""
        game = ctx.game

        # The production interface does not always appear - a single-item
        # recipe skips it - so this is a short wait, not a requirement.
        if game.wait_until(lambda s: s.production_open or s.dialogue_open,
                           timeout=2.5, poll=0.2):
            game.press("space")
            game.wait_ticks(2)

        before = game.state.inventory.count(self.material)
        ctx.stats.count("batches")

        # Materials going down is the only signal that works across every
        # processing skill - some animate, some do not, some animate once
        # for twenty items.
        game.wait_until(
            lambda s: s.inventory.count(self.material) < before
            or not s.inventory.contains(self.material),
            timeout=float(ctx.opt("script.batch_timeout", 45)), poll=1.0)


def build(ctx: Context) -> list[Task]:
    mode = str(ctx.opt("script.mode", "use_on_item"))
    material = ctx.opt("script.material")
    if not material:
        raise Stop("script.material is required")

    tool = ctx.opt("script.tool", "")
    target_object = ctx.opt("script.object", "")
    materials = ctx.opt("script.withdraw") or [{"item": material, "quantity": "all"}]
    products = ctx.opt("script.products") or []
    bank_names = ctx.opt("script.bank_names") or list(BANK_NAMES)

    watched_objects = list(bank_names)
    if target_object:
        watched_objects.append(target_object)
    ctx.game.watch(objects=watched_objects, npcs=bank_names)

    return [
        Restock(materials, products, bank_names, ctx.opt("script.bank_tile")),
        Produce(mode, tool, material, target_object,
                ctx.opt("script.action", "Use")),
    ]
