"""Banking.

Almost every loop that is not "drop it all" ends up here, so it is worth
having in one place rather than re-derived in each script.

Two things are deliberately not automated:

  * The bank PIN. It is a credential, and a bot that types your PIN is a
    bot that stores your PIN. If the PIN screen appears the script stops
    and says so - enter it yourself and restart.
  * Anything involving the Grand Exchange. Trading is where a stuck script
    stops costing you time and starts costing you items.
"""
from __future__ import annotations

from .state import Entity, State
from .task import Context, Stop

BANK_NAMES = ("Bank booth", "Bank chest", "Banker", "Bank counter", "Grand Exchange booth")
BANK_ACTIONS = ("Bank", "Use", "Collect")

# Quantities the bank offers as their own menu option. Anything else has to
# go through Withdraw-X and a typed number.
QUICK_QUANTITIES = (1, 5, 10)


class Bank:
    """Operations on the bank interface. All static; there is no state to keep."""

    @staticmethod
    def check_pin(ctx: Context) -> None:
        if ctx.state.bank_pin_open:
            raise Stop(
                "the bank PIN screen is up - this bot does not type PINs. "
                "Enter it yourself and start the script again.")

    @staticmethod
    def is_open(state: State) -> bool:
        return state.bank_open

    @staticmethod
    def find(ctx: Context, names=BANK_NAMES) -> Entity | None:
        """The nearest bank booth, chest or banker we can see."""
        state = ctx.state
        return (state.nearest_object(names) or state.nearest_npc(names))

    @staticmethod
    def open(ctx: Context, names=BANK_NAMES, timeout: float = 12.0) -> bool:
        game = ctx.game
        Bank.check_pin(ctx)
        if game.state.bank_open:
            return True

        target = Bank.find(ctx, names)
        if target is None:
            ctx.log("  ! no bank in sight")
            return False

        # Booths take "Bank", chests take "Use", bankers take "Bank" - and
        # which one a given object offers varies by location, so ask the
        # object what it supports rather than guessing per bank.
        options = [a for a in BANK_ACTIONS if a in target.actions] or ["Bank"]
        for option in options:
            if game.interact(target, option, target.name, tries=2):
                if game.wait_until(lambda s: s.bank_open, timeout=timeout):
                    ctx.stats.count("bank visits")
                    return True
        Bank.check_pin(ctx)
        ctx.log("  ! bank did not open")
        return False

    @staticmethod
    def close(ctx: Context) -> bool:
        if not ctx.state.bank_open:
            return True
        ctx.game.press("escape")
        return ctx.game.wait_until(lambda s: not s.bank_open, timeout=4.0)

    # -- moving items ----------------------------------------------

    @staticmethod
    def deposit_inventory(ctx: Context) -> bool:
        """The deposit-everything button."""
        game = ctx.game
        if game.state.inventory.empty:
            return True
        address = ctx.opt("widgets.bank_deposit_inventory", (12, 44))
        if not game.click_widget(*address):
            return False
        ok = game.wait_until(lambda s: s.inventory.empty, timeout=4.0)
        if ok:
            ctx.stats.count("deposits")
        return ok

    @staticmethod
    def deposit(ctx: Context, item: str, all_of_them: bool = True) -> bool:
        """Deposit one item stack from the inventory, by name."""
        game = ctx.game
        found = game.state.inventory.find_matching(item)
        if found is None:
            return True
        box = game.inventory_box(found.slot)
        if box is None:
            return False
        option = "Deposit-All" if all_of_them else "Deposit-1"
        before = game.state.inventory.count(found.name)
        if not game.interact(box, option, found.name):
            return False
        return game.wait_until(
            lambda s: s.inventory.count(found.name) < before, timeout=4.0)

    @staticmethod
    def withdraw(ctx: Context, item: str, quantity: int | str = 1) -> bool:
        """Take an item out of the bank by name.

        `quantity` is a number, or "all". Numbers the bank does not offer
        as their own menu entry go through Withdraw-X, which opens a
        number prompt to type into.
        """
        game = ctx.game
        if not game.state.bank_open:
            ctx.log("  ! withdraw called with the bank closed")
            return False

        entry = game.state.bank.find_matching(item)
        if entry is None:
            ctx.log(f"  ! no {item} in the bank")
            return False

        box = Bank._slot_box(ctx, entry.slot)
        if box is None:
            ctx.log(f"  ! {entry.name} is in the bank but not on screen")
            return False

        if quantity == "all":
            option = "Withdraw-All"
        elif int(quantity) in QUICK_QUANTITIES:
            option = f"Withdraw-{int(quantity)}"
        else:
            option = "Withdraw-X"

        before = game.state.inventory.count(entry.name)
        if not game.interact(box, option, entry.name):
            return False

        if option == "Withdraw-X":
            # The prompt is a chatbox input; typing straight into it works.
            game.wait_ticks(2)
            game.keyboard.type_text(str(int(quantity)), enter=True)

        ok = game.wait_until(
            lambda s: s.inventory.count(entry.name) > before, timeout=5.0)
        if ok:
            ctx.stats.count("withdrawals")
        return ok

    @staticmethod
    def _slot_box(ctx: Context, slot: int, scroll_tries: int = 4):
        """Find a bank slot on screen, scrolling to it if it is out of view.

        Bank slot widgets exist whether or not they are visible, so an
        off-screen item still reports bounds - just ones outside the
        container. Comparing against the container rectangle is what
        distinguishes "not there" from "not scrolled to".
        """
        game = ctx.game
        address = ctx.opt("widgets.bank_item_container", (12, 13))
        container = game.client.widget_box(*address)

        for attempt in range(scroll_tries):
            box = game.client.widget_box(*address, sub=slot)
            if box is None:
                return None
            if container is None:
                return box
            inside = (box.y >= container.y - 2
                      and box.y + box.h <= container.y + container.h + 2)
            if inside:
                return box
            if attempt == scroll_tries - 1:
                break
            # Scroll toward it: down if the slot sits below the view.
            direction = -1 if box.y > container.y else 1
            game.mouse.move(*game.box_to_screen(container))
            game.mouse.scroll(direction * 3)
            game.wait_ticks(0.5)
        return None
