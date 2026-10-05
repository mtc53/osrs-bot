"""The loop everything runs inside.

A script is a priority list of tasks. Each tick the runner asks them in
order "is this your turn?" and runs the first one that says yes. That is
deliberately not a state machine: state machines for this kind of work
grow a transition for every way the game can interrupt you - a level-up
box, a random dialogue, a player crashing your spot - and the list of
those is not knowable up front. A priority list lets you insert the
interruption at the top and leave the rest of the script alone.

The ordering convention scripts follow:

    1. safety      (eat, flee, log out)
    2. interruption(dismiss a dialogue, close a level-up box)
    3. the actual work
    4. idling      (what to do while waiting for a tree to fall)
"""
from __future__ import annotations

import random
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .api import Game
from .state import State, StateError


class Task:
    """One thing the bot might do, and the condition for doing it."""

    name = "task"

    def should_run(self, ctx: "Context") -> bool:
        raise NotImplementedError

    def run(self, ctx: "Context") -> None:
        raise NotImplementedError

    def __str__(self) -> str:
        return self.name


class Lambda(Task):
    """A task from two functions, for the one-offs not worth a class."""

    def __init__(self, name: str, when: Callable[["Context"], bool],
                 do: Callable[["Context"], None]):
        self.name = name
        self._when = when
        self._do = do

    def should_run(self, ctx: "Context") -> bool:
        return self._when(ctx)

    def run(self, ctx: "Context") -> None:
        self._do(ctx)


class Stop(Exception):
    """Raised by a task to end the run cleanly."""


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    iterations: int = 0
    actions: int = 0
    failures: int = 0
    xp_start: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    @property
    def runtime(self) -> float:
        return time.time() - self.started

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def note_xp(self, state: State) -> None:
        if not self.xp_start:
            self.xp_start = {k: int(v.get("xp", 0))
                             for k, v in state.skills.items()}

    def xp_gained(self, state: State) -> dict[str, int]:
        out = {}
        for skill, before in self.xp_start.items():
            now = state.xp(skill)
            if now > before:
                out[skill] = now - before
        return out

    def summary(self, state: State | None = None) -> str:
        hours = self.runtime / 3600
        parts = [f"ran {self._clock(self.runtime)}",
                 f"{self.actions} actions"]
        if self.failures:
            parts.append(f"{self.failures} failures")
        for key, value in sorted(self.counters.items()):
            parts.append(f"{key}: {value}")
        if state is not None:
            for skill, gained in sorted(self.xp_gained(state).items()):
                rate = int(gained / hours) if hours > 0.01 else 0
                parts.append(f"{skill} +{gained:,} xp ({rate:,}/h)")
        return " | ".join(parts)

    @staticmethod
    def _clock(seconds: float) -> str:
        seconds = int(seconds)
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m{seconds % 60:02d}s"


class BreakScheduler:
    """Pauses between stretches of work.

    Long unbroken sessions are the single loudest thing about an automated
    account, and they are also just bad practice for an unattended script:
    a bot that never stops is a bot that is still going three hours after
    something went wrong.
    """

    def __init__(self, work_minutes: tuple[float, float] = (25, 55),
                 break_minutes: tuple[float, float] = (3, 12),
                 rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.work_range = work_minutes
        self.break_range = break_minutes
        self._next_break = time.time() + self._pick(work_minutes)

    def _pick(self, span: tuple[float, float]) -> float:
        return self.rng.uniform(*span) * 60

    @property
    def due(self) -> bool:
        return time.time() >= self._next_break

    def take(self, log=print) -> None:
        duration = self._pick(self.break_range)
        log(f"  break for {duration / 60:.1f} minutes")
        time.sleep(duration)
        self._next_break = time.time() + self._pick(self.work_range)


@dataclass
class Context:
    """What every task gets handed."""

    game: Game
    config: dict
    stats: Stats
    rng: random.Random
    log: Callable[[str], None] = print
    data: dict = field(default_factory=dict)

    @property
    def state(self) -> State:
        return self.game.state

    def opt(self, key: str, default=None):
        """Read a dotted key out of the script config."""
        node = self.config
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class Runner:
    """Drives a task list until something says stop."""

    def __init__(self, tasks: list[Task], ctx: Context,
                 breaks: BreakScheduler | None = None,
                 max_runtime: float | None = None,
                 stop_file: Path | None = None,
                 heartbeat: Callable[[Stats, "State", bool], None] | None = None,
                 heartbeat_interval: float = 60.0):
        self.tasks = tasks
        self.ctx = ctx
        self.breaks = breaks
        self.max_runtime = max_runtime
        self.stop_file = stop_file
        self.heartbeat = heartbeat
        self.heartbeat_interval = heartbeat_interval
        self._last_beat = 0.0
        self.running = True
        self._install_signal_handler()

    def _beat(self, state: "State | None", final: bool = False) -> None:
        """Hand the current stats to the heartbeat, at most once per interval.
        A failing heartbeat never touches the run."""
        if not self.heartbeat:
            return
        now = time.time()
        if not final and now - self._last_beat < self.heartbeat_interval:
            return
        self._last_beat = now
        try:
            self.heartbeat(self.ctx.stats, state, final)
        except Exception as exc:
            self.ctx.log(f"  (heartbeat error: {exc})")

    def _install_signal_handler(self) -> None:
        def handle(signum, frame):
            self.ctx.log("\nstopping after this task (Ctrl-C again to force)")
            self.running = False
            signal.signal(signal.SIGINT, signal.SIG_DFL)

        try:
            signal.signal(signal.SIGINT, handle)
        except ValueError:
            # Not on the main thread; the caller owns the stop condition.
            pass

    def _should_stop(self) -> str | None:
        if not self.running:
            return "asked to stop"
        if self.max_runtime and self.ctx.stats.runtime > self.max_runtime:
            return "hit the runtime limit"
        if self.stop_file and self.stop_file.exists():
            return f"found the stop file {self.stop_file}"
        return None

    def loop(self) -> Stats:
        log = self.ctx.log
        game = self.ctx.game

        if not game.client.ping():
            raise StateError(
                "The State Link plugin is not answering. Start RuneLite, "
                "enable the State Link plugin, and check the port matches.")

        log(f"tasks: {', '.join(str(t) for t in self.tasks)}")
        misses = 0

        while True:
            reason = self._should_stop()
            if reason:
                log(f"stopped: {reason}")
                break

            try:
                state = game.refresh()
            except StateError as exc:
                misses += 1
                log(f"  ! {exc}")
                if misses >= 10:
                    log("stopped: lost contact with the plugin")
                    break
                time.sleep(min(30.0, 2.0 * misses))
                continue
            misses = 0

            self.ctx.stats.iterations += 1
            self.ctx.stats.note_xp(state)
            self._beat(state)

            if not state.logged_in:
                log(f"  waiting: not logged in ({state.game_state})")
                time.sleep(5)
                continue

            if self.breaks and self.breaks.due and not state.any_interface_open:
                self.breaks.take(log)
                continue

            task = self._pick(state)
            if task is None:
                time.sleep(0.6 * self.ctx.rng.uniform(0.8, 1.4))
                continue

            try:
                task.run(self.ctx)
                self.ctx.stats.actions += 1
            except Stop as exc:
                log(f"stopped: {exc}")
                break
            except StateError as exc:
                log(f"  ! {exc}")
                time.sleep(2)
            except Exception as exc:  # a broken task should not end the run
                self.ctx.stats.failures += 1
                log(f"  ! {task} raised {type(exc).__name__}: {exc}")
                if self.ctx.stats.failures > 40:
                    log("stopped: too many task failures")
                    break
                time.sleep(2)

            time.sleep(0.25 * self.ctx.rng.uniform(0.7, 1.6))

        summary = self.ctx.stats.summary(self.ctx.game.state)
        log(summary)
        self._beat(self.ctx.game.state, final=True)
        return self.ctx.stats

    def _pick(self, state: State) -> Task | None:
        for task in self.tasks:
            try:
                if task.should_run(self.ctx):
                    return task
            except Exception as exc:
                self.ctx.log(f"  ! {task}.should_run raised {exc}")
        return None
