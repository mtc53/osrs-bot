"""Tests for the reading half.

state.py deliberately imports nothing but the standard library, and no
ctypes, so all of this runs without a game, a client, or Windows.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osrsbot.state import Box, Container, Menu, State  # noqa: E402


def snapshot(**overrides) -> dict:
    base = {
        "tick": 4210,
        "timestamp": 1_000_000,
        "gameState": "LOGGED_IN",
        "loggedIn": True,
        "canvas": {"width": 1024, "height": 768, "screenX": 100,
                   "screenY": 60, "onScreen": True, "focused": True},
        "player": {
            "name": "Test", "combatLevel": 42,
            "worldPoint": {"x": 3200, "y": 3200, "plane": 0},
            "animation": -1, "poseAnimation": 808, "idlePoseAnimation": 808,
            "moving": False, "idle": True, "interacting": None,
        },
        "vitals": {"hitpoints": 30, "maxHitpoints": 60,
                   "hitpointsPercent": 50, "runEnergy": 42, "running": True,
                   "specialAttack": 100},
        "skills": {"woodcutting": {"level": 60, "boosted": 60, "xp": 273_742}},
        "inventory": {"open": True, "used": 2, "empty": False, "full": False,
                      "items": [
                          {"slot": 0, "id": 1359, "quantity": 1,
                           "name": "Rune axe"},
                          {"slot": 3, "id": 1521, "quantity": 12,
                           "name": "Oak logs"}]},
        "equipment": {"open": True, "items": []},
        "bank": {"open": False, "items": []},
        "widgets": {"bank": False, "levelUp": False, "dialogueNpc": False},
        "npcs": [], "objects": [], "groundItems": [], "players": [],
        "chat": [], "menu": {"open": False, "entries": [], "bounds": None},
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- containers

def test_container_counts_stacks_not_slots():
    inv = Container(snapshot()["inventory"])
    assert len(inv) == 2
    assert inv.count("Oak logs") == 12
    assert inv.contains("Rune axe")
    assert not inv.contains("Oak")  # count() is an exact name match


def test_find_matching_is_a_substring_search():
    inv = Container(snapshot()["inventory"])
    assert inv.find_matching("oak").name == "Oak logs"
    assert inv.find_matching("axe").slot == 0
    assert inv.find_matching("dragon") is None


def test_free_slots_uses_reported_usage():
    inv = Container(snapshot()["inventory"])
    assert inv.free_slots == 26


# ------------------------------------------------------------------ state

def test_player_basics():
    state = State(snapshot())
    assert state.logged_in
    assert state.name == "Test"
    assert state.idle
    assert state.position == (3200, 3200, 0)
    assert state.level("woodcutting") == 60
    assert state.hp_percent == 50


def test_distance_is_chebyshev_and_plane_aware():
    state = State(snapshot())
    assert state.distance_to((3203, 3204, 0)) == 4
    assert state.distance_to((3200, 3200, 1)) > 1000


def test_nearest_matches_on_substring_and_skips_offscreen():
    raw = snapshot(npcs=[
        {"name": "Cow", "id": 1, "distance": 2, "clickbox": None,
         "dead": False},
        {"name": "Cow calf", "id": 2, "distance": 5,
         "clickbox": {"x": 10, "y": 10, "w": 30, "h": 30}, "dead": False},
    ])
    state = State(raw)
    # The nearer cow has no hitbox, so it is not clickable and is skipped.
    assert state.nearest_npc("cow").name == "Cow calf"
    assert state.nearest_npc("cow", clickable_only=False).name == "Cow"


def test_dead_npcs_are_excluded_by_default():
    raw = snapshot(npcs=[
        {"name": "Cow", "id": 1, "distance": 1, "dead": True,
         "clickbox": {"x": 0, "y": 0, "w": 10, "h": 10}},
    ])
    assert State(raw).nearest_npc("cow") is None


def test_said_recently_respects_the_window():
    raw = snapshot(chat=[
        {"message": "You need a Woodcutting level of 60.",
         "timestamp": 998_000},
        {"message": "Ancient words.", "timestamp": 100},
    ])
    state = State(raw)
    assert state.said_recently("woodcutting level", within_ms=6000)
    assert not state.said_recently("woodcutting level", within_ms=500)
    assert not state.said_recently("ancient words")


def test_any_interface_open_covers_the_blocking_ones():
    assert not State(snapshot()).any_interface_open
    assert State(snapshot(widgets={"levelUp": True})).any_interface_open
    assert State(snapshot(widgets={"bank": True})).any_interface_open


# -------------------------------------------------------------------- box

def test_box_points_stay_inside_the_inset():
    box = Box(100, 200, 40, 20)
    rng = random.Random(7)
    for _ in range(2000):
        x, y = box.point(rng)
        assert 106 <= x <= 134, x
        assert 203 <= y <= 217, y


def test_box_points_are_not_all_the_centre():
    box = Box(0, 0, 60, 60)
    rng = random.Random(1)
    seen = {box.point(rng) for _ in range(200)}
    assert len(seen) > 20


def test_tiny_boxes_do_not_collapse_or_throw():
    for w, h in ((1, 1), (2, 3), (0, 0)):
        x, y = Box(50, 50, w, h).point(random.Random(3))
        assert isinstance(x, int) and isinstance(y, int)


# ------------------------------------------------------------------- menu

def menu(entries, bounds=None, open_=True) -> Menu:
    return Menu({
        "open": open_,
        "entries": [{"option": o, "target": t} for o, t in entries],
        "bounds": bounds,
    })


def test_top_entry_is_what_a_left_click_does():
    m = menu([("Chop down", "Oak tree"), ("Examine", "Oak tree"),
              ("Walk here", ""), ("Cancel", "")])
    assert m.top == ("Chop down", "Oak tree")
    assert m.matches("Chop down", "Oak")
    assert not m.matches("Chop down", "Willow")
    assert not m.matches("Examine")


def test_index_of_finds_a_row_by_option_and_target():
    m = menu([("Walk here", ""), ("Chop down", "Oak tree"),
              ("Examine", "Oak tree")])
    assert m.index_of("Chop down") == 1
    assert m.index_of("Chop down", "oak") == 1
    assert m.index_of("Chop down", "willow") is None
    assert m.index_of("Attack") is None


def test_row_box_divides_the_menu_under_its_header():
    m = menu([("A", ""), ("B", ""), ("C", "")],
             bounds={"x": 200, "y": 100, "w": 120, "h": 19 + 45})
    first = m.row_box(0)
    second = m.row_box(1)
    assert first.y == 119
    assert second.y == 134            # one 15px row further down
    assert first.h == 15
    assert 200 <= first.x < 320
    assert m.row_box(9) is None


def test_row_box_needs_bounds():
    assert menu([("A", "")]).row_box(0) is None


def test_a_closed_menu_still_reports_the_hover_entries():
    # Not a quirk worth hiding: with no menu open, getMenuEntries() is what
    # a click would do, which is exactly what hover-verify needs.
    m = menu([("Attack", "Cow")], open_=False)
    assert not m.open
    assert m.matches("Attack", "Cow")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
