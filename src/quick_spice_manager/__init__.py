import importlib

__version__ = importlib.metadata.version("quick_spice_manager")


from .spice_manager import SpiceManager

__all__ = ["SpiceManager"]
