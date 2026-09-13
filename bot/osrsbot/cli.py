"""Command line: run a script, or poke at the plugin while writing one.

    python -m osrsbot.cli doctor
    python -m osrsbot.cli run config/woodcutting.toml
    python -m osrsbot.cli state --section inventory
    python -m osrsbot.cli menu
    python -m osrsbot.cli inspect --group 12 --child 13 --sub 0

The last three exist because the slow part of writing one of these is
never the logic - it is finding out what the client calls the thing you
are looking at. Hover something and run `menu`, and it tells you the exact
option and target strings to put in your config.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import tomllib
from pathlib import Path

from . import safety
from .api import Game
from .input import Keyboard, Mouse
from .scripts import combat, craft, gather
from .state import StateClient, StateError
from .task import BreakScheduler, Context, Runner, Stats, Stop

SCRIPTS = {
    "gather": gather.build,
    "combat": combat.build,
    "craft": craft.build,
}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"No config at {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def make_client(config: dict) -> StateClient:
    link = config.get("link", {})
    return StateClient(port=int(link.get("port", 8095)),
                       token=str(link.get("token", "")),
                       timeout=float(link.get("timeout", 3.0)))


# ------------------------------------------------------------------ run

def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    run_cfg = config.get("run", {})

    name = args.script or run_cfg.get("script")
    if name not in SCRIPTS:
        raise SystemExit(f"script must be one of {', '.join(SCRIPTS)}, got {name!r}")

    seed = run_cfg.get("seed")
    rng = random.Random(seed)
    client = make_client(config)
    game = Game(client, Mouse(rng, float(run_cfg.get("mouse_speed", 1.0))),
                Keyboard(rng), rng=rng, log=log)

    ctx = Context(game=game, config=config, stats=Stats(), rng=rng, log=log)

    if not client.ping():
        raise SystemExit(
            "The State Link plugin is not answering on "
            f"{client.base}. Start RuneLite, enable State Link, and check "
            "the port in your config matches the one in the plugin settings.")

    if not game.ensure_focus():
        log("! could not focus the client - clicks will go to the wrong "
            "window until you click on RuneLite yourself")

    try:
        tasks = list(safety.standard(config.get("safety", {})))
        tasks += SCRIPTS[name](ctx)
        if config.get("safety", {}).get("fidget", True):
            tasks.append(safety.IdleFidget())
    except Stop as exc:
        raise SystemExit(f"cannot start: {exc}")

    breaks = None
    if run_cfg.get("breaks", True):
        breaks = BreakScheduler(
            tuple(run_cfg.get("work_minutes", (25, 55))),
            tuple(run_cfg.get("break_minutes", (3, 12))), rng)

    max_runtime = None
    if run_cfg.get("max_minutes"):
        max_runtime = float(run_cfg["max_minutes"]) * 60

    stop_file = Path(run_cfg["stop_file"]) if run_cfg.get("stop_file") else None

    log(f"running {name} from {args.config}")
    if max_runtime:
        log(f"will stop after {max_runtime / 60:.0f} minutes")
    if stop_file:
        log(f"or when {stop_file} appears")

    runner = Runner(tasks, ctx, breaks=breaks, max_runtime=max_runtime,
                    stop_file=stop_file)
    try:
        runner.loop()
    except StateError as exc:
        log(f"! {exc}")
        return 1
    return 0


# ------------------------------------------------------------ inspection

def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config)) if args.config else {}
    client = make_client(config)

    print(f"plugin at {client.base} ... ", end="")
    if not client.ping():
        print("NOT ANSWERING")
        print("  - is RuneLite running?")
        print("  - is the State Link plugin enabled in the plugin list?")
        print("  - does the port in the plugin config match this one?")
        return 1
    print("ok")

    state = client.fetch()
    print(f"game state:  {state.game_state}")
    print(f"logged in:   {state.logged_in}")
    canvas = state.canvas
    print(f"canvas:      {canvas.get('width')}x{canvas.get('height')} at "
          f"({canvas.get('screenX')}, {canvas.get('screenY')})")
    print(f"on screen:   {state.on_screen}")
    print(f"focused:     {state.focused}")

    if not state.on_screen:
        print("  ! the canvas is not on screen - unminimise RuneLite, or "
              "clicks cannot be aimed")
        return 1
    if not state.logged_in:
        print("  ! log in to check the rest")
        return 0

    print(f"player:      {state.name} at {state.position}")
    print(f"health:      {state.hitpoints}/{state.max_hitpoints} "
          f"({state.hp_percent}%)")
    print(f"inventory:   {len(state.inventory)}/28 used")
    print(f"run energy:  {state.run_energy}%")
    if not state.focused:
        print("  ! the client is not focused - input will go elsewhere")
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    client = make_client(load_config(Path(args.config)) if args.config else {})
    raw = client.fetch().raw
    if args.section:
        raw = raw.get(args.section, f"no section {args.section!r}")
    print(json.dumps(raw, indent=2)[:args.limit])
    return 0


def cmd_menu(args: argparse.Namespace) -> int:
    """What would a click do right now? Hover something and run this."""
    client = make_client(load_config(Path(args.config)) if args.config else {})
    if args.watch:
        print("watching the menu under the cursor; Ctrl-C to stop")
        last = None
        try:
            while True:
                entries = client.menu().entries
                if entries != last:
                    last = entries
                    print("-" * 50)
                    for i, (option, target) in enumerate(entries):
                        marker = "<- left click" if i == 0 else ""
                        print(f"  {i}: {option:<20} {target:<28} {marker}")
                time.sleep(0.3)
        except KeyboardInterrupt:
            return 0
    menu = client.menu()
    print(f"open: {menu.open}  bounds: {menu.box}")
    for i, (option, target) in enumerate(menu.entries):
        print(f"  {i}: {option!r} -> {target!r}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    client = make_client(load_config(Path(args.config)) if args.config else {})
    print(json.dumps(client.widget(args.group, args.child, args.sub), indent=2))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Set the plugin's export filters, then dump what it can see."""
    client = make_client(load_config(Path(args.config)) if args.config else {})
    client.watch(objects=args.objects or (), npcs=args.npcs or (),
                 items=args.items or ())
    time.sleep(0.8)  # let a tick go by so the snapshot reflects the filter
    state = client.fetch()
    for label, entities in (("objects", state.objects), ("npcs", state.npcs),
                            ("ground items", state.ground_items)):
        print(f"{label}:")
        for entity in entities[:12]:
            flag = "" if entity.clickable else "  (off screen)"
            print(f"  {entity.name:<24} {entity.distance:>3} tiles"
                  f"  {entity.actions}{flag}")
    return 0


# ----------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="osrsbot")
    parser.add_argument("--config", default=None,
                        help="TOML config, for the link port and token")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a script")
    run.add_argument("config")
    run.add_argument("--script", default=None, choices=sorted(SCRIPTS))
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="check the setup")
    doctor.set_defaults(func=cmd_doctor)

    state = sub.add_parser("state", help="dump the current snapshot")
    state.add_argument("--section", default=None,
                       help="one top-level key, e.g. inventory")
    state.add_argument("--limit", type=int, default=8000)
    state.set_defaults(func=cmd_state)

    menu = sub.add_parser("menu", help="what a click would do right now")
    menu.add_argument("--watch", action="store_true",
                      help="keep printing as the cursor moves")
    menu.set_defaults(func=cmd_menu)

    inspect = sub.add_parser("inspect", help="look up an interface component")
    inspect.add_argument("--group", type=int, required=True)
    inspect.add_argument("--child", type=int, default=0)
    inspect.add_argument("--sub", type=int, default=-1)
    inspect.set_defaults(func=cmd_inspect)

    watch = sub.add_parser("watch", help="set export filters and list matches")
    watch.add_argument("--objects", nargs="*")
    watch.add_argument("--npcs", nargs="*")
    watch.add_argument("--items", nargs="*")
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
