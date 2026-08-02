"""
killswitch — layered emergency stop for the iGEM wildfire soil chamber.

    from killswitch import KillSwitch

    ks = KillSwitch()
    ks.register_shutdown_hook(pumps_off, "pumps", priority=0)
    ks.arm()

Read DESIGN.md before touching the wiring assumptions.
"""

from .backends import GpioBackend, GpioUnavailable, SimBackend
from .core import (
    InterlockOpen,
    KillEvent,
    KillSwitch,
    KillSwitchLatched,
    TriggerSource,
    clear_latch,
    latch_info,
)

__all__ = [
    "KillSwitch",
    "KillEvent",
    "TriggerSource",
    "KillSwitchLatched",
    "InterlockOpen",
    "GpioBackend",
    "GpioUnavailable",
    "SimBackend",
    "clear_latch",
    "latch_info",
]

__version__ = "0.1.0"
