"""
Thin GPIO abstraction so the kill switch works alongside either control script.

chamber.py drives GPIO through lgpio; bapp.py through gpiozero. Both can host
the kill switch, and the logic is testable on a laptop through SimBackend.
"""

from __future__ import annotations

from typing import Dict, Optional


class GpioUnavailable(RuntimeError):
    """No usable GPIO backend, and simulation was not explicitly allowed."""


class GpioBackend:
    """Raw-level GPIO access. read()/write() always speak physical pin levels."""

    name = "base"

    def claim_input(self, pin: int, pull: str = "up") -> None:
        raise NotImplementedError

    def claim_output(self, pin: int, level: int = 0) -> None:
        raise NotImplementedError

    def read(self, pin: int) -> int:
        raise NotImplementedError

    def write(self, pin: int, level: int) -> None:
        raise NotImplementedError

    def release(self, pin: int) -> None:
        pass

    def close(self) -> None:
        pass


class LgpioBackend(GpioBackend):
    """Preferred on the Pi. Can share an already-open chip handle with the host
    script so two gpiochip handles are not fighting over the same chip."""

    name = "lgpio"

    def __init__(self, chip: int = 0, handle: Optional[int] = None):
        import lgpio  # noqa: F401  (raises ImportError off-Pi)

        self._lg = lgpio
        self._owns_handle = handle is None
        self._h = lgpio.gpiochip_open(chip) if handle is None else handle
        self._claimed = []

    def claim_input(self, pin: int, pull: str = "up") -> None:
        flags = {
            "up": self._lg.SET_PULL_UP,
            "down": self._lg.SET_PULL_DOWN,
            "none": self._lg.SET_PULL_NONE,
        }[pull]
        self._lg.gpio_claim_input(self._h, pin, flags)
        self._claimed.append(pin)

    def claim_output(self, pin: int, level: int = 0) -> None:
        self._lg.gpio_claim_output(self._h, pin, level)
        self._claimed.append(pin)

    def read(self, pin: int) -> int:
        return int(self._lg.gpio_read(self._h, pin))

    def write(self, pin: int, level: int) -> None:
        self._lg.gpio_write(self._h, pin, int(level))

    def release(self, pin: int) -> None:
        try:
            self._lg.gpio_free(self._h, pin)
        except Exception:
            pass
        if pin in self._claimed:
            self._claimed.remove(pin)

    def close(self) -> None:
        for pin in list(self._claimed):
            self.release(pin)
        if self._owns_handle:
            try:
                self._lg.gpiochip_close(self._h)
            except Exception:
                pass


class GpiozeroBackend(GpioBackend):
    """For hosts already using gpiozero (bapp.py)."""

    name = "gpiozero"

    def __init__(self):
        from gpiozero import InputDevice, OutputDevice  # noqa: F401

        self._InputDevice = InputDevice
        self._OutputDevice = OutputDevice
        self._dev: Dict[int, object] = {}
        self._pull: Dict[int, str] = {}

    def claim_input(self, pin: int, pull: str = "up") -> None:
        pull_up = {"up": True, "down": False, "none": None}[pull]
        self._dev[pin] = self._InputDevice(pin, pull_up=pull_up)
        self._pull[pin] = pull

    def claim_output(self, pin: int, level: int = 0) -> None:
        self._dev[pin] = self._OutputDevice(pin, initial_value=bool(level))

    def read(self, pin: int) -> int:
        dev = self._dev[pin]
        # InputDevice.value is 1 when *active*; with pull_up=True active is low.
        value = int(dev.value)
        return (1 - value) if self._pull.get(pin) == "up" else value

    def write(self, pin: int, level: int) -> None:
        dev = self._dev[pin]
        dev.on() if level else dev.off()

    def release(self, pin: int) -> None:
        dev = self._dev.pop(pin, None)
        self._pull.pop(pin, None)
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

    def close(self) -> None:
        for pin in list(self._dev):
            self.release(pin)


class SimBackend(GpioBackend):
    """In-memory pins for development and the test suite. Never selected
    automatically unless config.ALLOW_SIMULATION is set."""

    name = "sim"

    def __init__(self, initial: Optional[Dict[int, int]] = None):
        self.levels: Dict[int, int] = dict(initial or {})
        self.writes = []          # (pin, level) history, for assertions
        self.fail_reads = False   # flip to simulate a GPIO read failure
        self.closed = False

    def claim_input(self, pin: int, pull: str = "up") -> None:
        self.levels.setdefault(pin, 1 if pull == "up" else 0)

    def claim_output(self, pin: int, level: int = 0) -> None:
        self.levels[pin] = int(level)
        self.writes.append((pin, int(level)))

    def read(self, pin: int) -> int:
        if self.fail_reads:
            raise OSError("simulated GPIO read failure")
        return int(self.levels[pin])

    def write(self, pin: int, level: int) -> None:
        self.levels[pin] = int(level)
        self.writes.append((pin, int(level)))

    def release(self, pin: int) -> None:
        self.levels.pop(pin, None)

    def close(self) -> None:
        self.closed = True


def open_backend(cfg, gpio_handle: Optional[int] = None) -> GpioBackend:
    """Build the backend named by cfg.GPIO_BACKEND ("auto" tries lgpio first)."""
    choice = getattr(cfg, "GPIO_BACKEND", "auto")

    if choice == "sim":
        if not cfg.ALLOW_SIMULATION:
            raise GpioUnavailable(
                "GPIO_BACKEND='sim' requires ALLOW_SIMULATION (set KILL_SWITCH_SIM=1). "
                "A simulated kill switch protects nothing."
            )
        return SimBackend()

    if choice in ("auto", "lgpio"):
        try:
            return LgpioBackend(chip=cfg.GPIO_CHIP, handle=gpio_handle)
        except Exception as exc:
            if choice == "lgpio":
                raise GpioUnavailable(f"lgpio backend unavailable: {exc!r}") from exc
            lgpio_err = exc
    else:
        lgpio_err = None

    if choice in ("auto", "gpiozero"):
        try:
            return GpiozeroBackend()
        except Exception as exc:
            if choice == "gpiozero":
                raise GpioUnavailable(f"gpiozero backend unavailable: {exc!r}") from exc
            gpiozero_err = exc
    else:
        gpiozero_err = None

    if cfg.ALLOW_SIMULATION:
        return SimBackend()

    raise GpioUnavailable(
        "No GPIO backend available (lgpio: {}; gpiozero: {}).\n"
        "On the Pi:  sudo apt install python3-lgpio\n"
        "Off-Pi, to run the logic without hardware:  KILL_SWITCH_SIM=1".format(
            repr(lgpio_err), repr(gpiozero_err)
        )
    )
