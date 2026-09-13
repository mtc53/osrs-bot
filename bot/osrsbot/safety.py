"""The tasks that go at the top of every script's list.

These handle the game interrupting you: a level-up box eating your next
click, a dialogue you did not ask for, your health running out. None of
them are the point of any script, and every script needs all of them,
which is exactly what a priority list is for.
"""
from __future__ import annotations

import time

from .api import LOGOUT_BUTTON
from .task import Context, Stop, Task

# Chat lines that mean "this script cannot continue". Recognising them is
# the difference between stopping with a clear reason and spinning for an
# hour clicking a tree you are not high enough level to chop.
FATAL_MESSAGES = (
    "you do not have enough inventory space",
    "you need a woodcutting level of",
    "you need a mining level of",
    "you need a fishing level of",
    "you don't have the required level",
)


class HandleLevelUp(Task):
    """Clear the level-up box.

    It steals the next click if left up, so this sits near the top. Space
    dismisses it, same as any other continue prompt.
    """

    name = "level-up"

    def should_run(self, ctx: Context) -> bool:
        return ctx.state.level_up_open

    def run(self, ctx: Context) -> None:
        ctx.log("  level up")
        ctx.stats.count("levels")
        ctx.game.press("space")
        ctx.game.wait_ticks(1)


class DismissDialogue(Task):
    """Press through a dialogue the script did not open."""

    name = "dialogue"

    def should_run(self, ctx: Context) -> bool:
        return ctx.state.dialogue_open and not ctx.data.get("expect_dialogue")

    def run(self, ctx: Context) -> None:
        text = ctx.state.dialogue_text
        if text:
            ctx.log(f"  dialogue: {text[:60]}")
        ctx.game.press("space")
        ctx.game.wait_ticks(1)


class Eat(Task):
    """Eat when health drops below a threshold.

    The threshold is a percentage rather than a fixed number of hitpoints
    so the same config works on a level 3 and a level 90.
    """

    name = "eat"

    def __init__(self, foods: list[str], at_percent: int = 55):
        self.foods = [f.lower() for f in foods]
        self.at_percent = at_percent

    def _food_in_bag(self, ctx: Context) -> str | None:
        for food in self.foods:
            item = ctx.state.inventory.find_matching(food)
            if item is not None:
                return item.name
        return None

    def should_run(self, ctx: Context) -> bool:
        if ctx.state.hp_percent > self.at_percent:
            return False
        return self._food_in_bag(ctx) is not None

    def run(self, ctx: Context) -> None:
        food = self._food_in_bag(ctx)
        before = ctx.state.hitpoints
        ctx.log(f"  eating {food} at {ctx.state.hp_percent}%")
        if ctx.game.interact_inventory(food, "Eat"):
            ctx.stats.count("food eaten")
            ctx.game.wait_until(lambda s: s.hitpoints > before, timeout=4.0)


class SurviveOrStop(Task):
    """Last line: out of food and dropping. Leave, or stop trying.

    Deliberately conservative. A half-working panic routine that fumbles
    clicks while at 10% health is worse than one that stops the script and
    says so plainly - so if the logout does not go through, this raises
    rather than flailing.
    """

    name = "survive"

    def __init__(self, at_percent: int = 25, logout: bool = True):
        self.at_percent = at_percent
        self.logout = logout

    def should_run(self, ctx: Context) -> bool:
        return ctx.state.hp_percent <= self.at_percent

    def run(self, ctx: Context) -> None:
        state = ctx.state
        ctx.log(f"  ! health at {state.hp_percent}% with no food left")
        if not self.logout:
            raise Stop("health critical")

        logout_tab = ctx.opt("widgets.logout_tab")
        if logout_tab and ctx.game.click_widget(*logout_tab):
            ctx.game.wait_ticks(2)
        if ctx.game.click_widget(*LOGOUT_BUTTON):
            ctx.game.wait_until(lambda s: not s.logged_in, timeout=8.0)
        raise Stop("health critical - logged out")


class LogoutIfAttacked(Task):
    """Leave if another player starts attacking. Off by default."""

    name = "attacked"

    def should_run(self, ctx: Context) -> bool:
        return bool(ctx.state.attackers())

    def run(self, ctx: Context) -> None:
        who = ctx.state.attackers()[0].get("name", "someone")
        raise Stop(f"{who} is attacking - stopping")


class KeepRunning(Task):
    """Turn run back on once energy has recovered."""

    name = "run"

    def __init__(self, above: int = 30):
        self.above = above

    def should_run(self, ctx: Context) -> bool:
        state = ctx.state
        return (not state.running and state.run_energy >= self.above
                and not state.any_interface_open)

    def run(self, ctx: Context) -> None:
        if ctx.game.set_run(True):
            ctx.log(f"  run on ({ctx.state.run_energy}% energy)")


class NoticeStuck(Task):
    """Stop when the game has told us, in the chatbox, that this cannot work."""

    name = "stuck"

    def should_run(self, ctx: Context) -> bool:
        return any(ctx.state.said_recently(m) for m in FATAL_MESSAGES)

    def run(self, ctx: Context) -> None:
        for message in FATAL_MESSAGES:
            if ctx.state.said_recently(message):
                raise Stop(f"the game said: {message!r}")


class IdleFidget(Task):
    """Something to do while waiting for a tree to fall.

    Lowest priority, so it only ever runs when nothing else wants the
    turn. It exists mostly so the cursor is not parked on the target for
    twenty seconds at a time, which is both conspicuous and a nuisance
    when you take the mouse back.
    """

    name = "idle"

    def __init__(self, chance: float = 0.12):
        self.chance = chance
        self._last = 0.0

    def should_run(self, ctx: Context) -> bool:
        if time.time() - self._last < 4:
            return False
        if ctx.state.any_interface_open:
            return False
        return ctx.rng.random() < self.chance

    def run(self, ctx: Context) -> None:
        self._last = time.time()
        roll = ctx.rng.random()
        if roll < 0.55:
            ctx.game.mouse.drift(ctx.rng.randint(40, 180))
        elif roll < 0.8:
            ctx.game.keyboard.press(ctx.rng.choice(["left", "right"]))
        else:
            time.sleep(ctx.rng.uniform(0.5, 2.5))


def standard(ctx_config: dict) -> list[Task]:
    """The safety block most scripts want, built from config.

    Returned as a list so a script can drop one or splice in its own -
    a combat script wants Eat, a fishing script in Lumbridge does not.
    """
    tasks: list[Task] = [NoticeStuck(), HandleLevelUp(), DismissDialogue()]

    foods = ctx_config.get("food") or []
    if foods:
        tasks.append(Eat(foods, int(ctx_config.get("eat_at_percent", 55))))
        tasks.append(SurviveOrStop(
            int(ctx_config.get("panic_at_percent", 25)),
            bool(ctx_config.get("logout_on_panic", True))))

    if ctx_config.get("stop_if_attacked"):
        tasks.append(LogoutIfAttacked())
    if ctx_config.get("use_run", True):
        tasks.append(KeepRunning(int(ctx_config.get("run_above", 30))))
    return tasks
