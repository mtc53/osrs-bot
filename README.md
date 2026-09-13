# osrs-bot

An Old School RuneScape automation framework, split across two processes.

```
  RuneLite  ──► State Link plugin ──► 127.0.0.1:8095 ──► Python bot ──► SendInput ──► the OS
   (reads)          (Java)              (JSON/HTTP)        (decides)                   (clicks)
```

The plugin **reads** the game and publishes it. The Python half **decides**
and moves the mouse. Neither writes to the client, and nothing is injected
into it — input goes through the same OS-level events a real mouse
produces, aimed using coordinates the client itself reported.

## Before anything else

Automating RuneScape violates Jagex's Terms of Service. Accounts that run
this get banned — that is the expected outcome, not the tail risk. Jagex
runs client-side detection and server-side behavioural analysis, and this
project makes no attempt to defeat either. There is no proxy rotation, no
client patching, no hardware-ID spoofing, and no ban-evasion tooling here,
and none is planned.

What is here is a clean, well-instrumented automation framework. Point it
at an account you are willing to lose.

## Why this shape

The usual approach — screenshot the window, template-match a button,
click where you found it — fails in RuneScape specifically, because the
camera rotates, lighting changes, models animate, and the thing you
matched a frame ago has moved. Pixel bots chop at the empty square where a
tree used to be.

RuneLite already knows exactly where every tree, NPC and inventory slot is
drawn this frame, because it drew them. So the plugin answers that, and
the bot's core loop becomes:

1. Ask the plugin where the target's hitbox is.
2. Move the mouse there.
3. **Ask the client what a click would actually do right now.**
4. Only then click.

Step 3 is the whole reliability story. A mis-aim becomes a retry instead
of a disaster, and the check costs about 90ms.

## Setup

### 1. The plugin

Needs JDK 11+ and Gradle. The RuneLite plugin workflow is IntelliJ-shaped:
open `plugin/` as a Gradle project and run `StateLinkPluginTest.main()`,
which launches a real RuneLite with the plugin loaded.

```bash
cd plugin && gradle build
```

Then in RuneLite: enable **State Link** in the plugin list. Default port
is 8095; set a token in its config if you want the endpoint authenticated.

> **Not yet compiled.** This half was written without a JDK available on
> the machine, so it has not been through a compiler. Expect to fix an
> import or two on first build — the API surface it uses is stable, but
> `isStretchedEnabled`/`getRealDimensions` in `SnapshotBuilder.stretchScale()`
> is the most likely thing to have drifted. That method is isolated and can
> return `{1.0, 1.0}` if you do not use Stretched Mode.

### 2. The bot

No dependencies. Python 3.11+ (for `tomllib`), Windows (for `SendInput`).

```bash
cd bot && python -m osrsbot.cli doctor
```

That tells you whether the plugin is answering, where the canvas is, and
whether the client is focused.

## Running something

```bash
cd bot && python -m osrsbot.cli run config/woodcutting.toml
```

Stop it with Ctrl-C, by creating the `STOP` file named in the config, or
by letting `max_minutes` expire.

Three script types ship, chosen by `run.script`:

| | |
|---|---|
| `gather` | Click a resource until the bag is full, then drop or bank it. |
| `combat` | Attack NPCs, eat, loot drops, skip fights that are already someone else's. |
| `craft` | Withdraw materials, make things, bank the products, repeat. |

Each is configured by name — `resources = ["Oak tree"]`, `targets = ["Cow"]` —
rather than by ID, because names are what the client puts on the menu and
what you can check yourself.

## Finding out what things are called

The slow part of writing one of these is never the logic. It is finding
out what the client calls the thing you are looking at. Three commands
cover it:

```bash
python -m osrsbot.cli menu --watch
```

Hover anything in-game and it prints the live menu, marking which row a
left click would take. Whatever it prints is what goes in your config.

```bash
python -m osrsbot.cli watch --objects "oak" --npcs "cow" --items "cowhide"
```

Sets the plugin's export filter and lists what it can currently see, with
distances and available actions.

```bash
python -m osrsbot.cli inspect --group 12 --child 44
```

Looks up one interface component — for when a client update moves a
button and you need its new address for the `[widgets]` table.

## Writing a script

A script is a **priority list of tasks**. Each pass, the runner asks each
task in order "is this your turn?" and runs the first that says yes.

```python
from osrsbot.task import Task

class Fletch(Task):
    name = "fletch"

    def should_run(self, ctx):
        return ctx.state.idle and ctx.state.inventory.contains("Yew logs")

    def run(self, ctx):
        ctx.game.use_on("Knife", ctx.game.inventory_box(
            ctx.state.inventory.find("Yew logs").slot))
        ctx.game.wait_ticks(3)


def build(ctx):
    ctx.game.watch(objects=["Bank booth"])
    return [Fletch()]
```

Drop that in `osrsbot/scripts/`, add it to `SCRIPTS` in `cli.py`, done.

This is deliberately not a state machine. State machines for this grow a
transition for every way the game can interrupt you — a level-up box, a
random dialogue, someone crashing your spot — and that list is not
knowable up front. A priority list lets you insert the interruption at the
top and leave the rest of the script alone. The convention:

1. safety (eat, flee, stop)
2. interruptions (dismiss a dialogue, clear a level-up box)
3. the actual work
4. idling

Levels 1 and 4 come free from `safety.standard()`.

## What it deliberately does not do

| | |
|---|---|
| **Type your bank PIN** | It is a credential. A bot that types your PIN is a bot that stores your PIN. The script stops and asks you to enter it. |
| **Trade or use the Grand Exchange** | Where a stuck script stops costing you time and starts costing you items. |
| **Log in** | Same reason as the PIN. Start it logged in. |
| **Evade detection** | Covered above. Mouse paths are curved and clicks are jittered because that is what makes clicks *land*, not as a disguise. |
| **Solve CAPTCHAs or randoms** | If something appears that it does not understand, it stops. |

## Layout

```
plugin/                        RuneLite plugin (Java)
  StateLinkPlugin.java           wiring, endpoints, chat buffer
  SnapshotBuilder.java           reads the client, once per tick
  StateServer.java               loopback HTTP, no game access
  Json.java, Ids.java            a tiny writer, and interface group ids

bot/osrsbot/
  state.py                       the read model — no clicking, no ctypes
  input.py                       SendInput mouse and keyboard
  api.py                         Game: hover-verify-click, waiting, walking
  bank.py                        opening, depositing, withdrawing
  task.py                        Task, Runner, Stats, breaks
  safety.py                      eat, level-ups, dialogues, panic
  scripts/                       gather, combat, craft
  cli.py                         run, doctor, state, menu, inspect, watch
bot/config/                      example profiles
bot/tests/                       state-model tests, no game required
```

`state.py` imports nothing but the standard library and no `ctypes`, which
is why its tests run anywhere:

```bash
cd bot && python -m pytest tests/ -q
```

## Endpoints

| | |
|---|---|
| `GET /ping` | Is the plugin alive. |
| `GET /state` | The full snapshot, rebuilt once per game tick. |
| `GET /menu` | The menu under the cursor, read live. |
| `GET /project?x=&y=&plane=` | Where a world tile is on canvas and minimap. |
| `GET /widget?group=&child=&sub=` | One interface component, read live. |
| `GET /watch?objects=&npcs=&items=` | Set the export filters. |

Bound to `127.0.0.1` only. Scene objects are not exported until `/watch`
asks for them by name — scanning every tile around the player each tick
for nobody is the one thing on the plugin side that would cost frames.
