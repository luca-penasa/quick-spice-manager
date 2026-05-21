import sys
import warnings

from loguru import logger as log

import importlib.metadata as metadata


__version__ = metadata.version("quick_spice_manager")

log.disable("quick_spice_manager")


def log_enable(
    level: str = "INFO", mod: str = "quick_spice_manager", remove_handlers: bool = True
) -> None:
    """Enable logging for a given module at specific level, by default it operates on the whole module."""
    if remove_handlers:
        log.remove()
    log.enable(mod)
    log.add(sys.stderr, level=level)


def log_enable_debug() -> None:
    """Enable debug logging for a given module, by default it operates on the whole module."""
    log_enable(level="DEBUG")


def log_disable(mod: str = "quick_spice_manager") -> None:
    """Totally disable logging from this module, by default it operates on the whole module."""
    log.disable(mod)


from .spice_manager import QuickSpiceManager

__all__ = ["QuickSpiceManager", "SpiceManager"]


def __getattr__(name: str) -> object:
    if name == "SpiceManager":
        warnings.warn(
            "SpiceManager is deprecated and will be removed in a future release. "
            "Use QuickSpiceManager instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from .spice_manager import SpiceManager
        return SpiceManager
    raise AttributeError(f"module 'quick_spice_manager' has no attribute {name!r}")
