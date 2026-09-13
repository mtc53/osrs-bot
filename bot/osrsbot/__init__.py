"""A Old School RuneScape automation framework split across two processes.

The RuneLite plugin in ../plugin reads the game and publishes it on a
loopback port. This half reads that and moves the mouse. Neither half
writes to the client.
"""
from .api import Game
from .input import Keyboard, Mouse
from .state import Box, Container, Entity, Menu, State, StateClient, StateError
from .task import Context, Runner, Stats, Stop, Task

__version__ = "0.1.0"

__all__ = [
    "Box", "Container", "Context", "Entity", "Game", "Keyboard", "Menu",
    "Mouse", "Runner", "State", "StateClient", "StateError", "Stats",
    "Stop", "Task",
]
