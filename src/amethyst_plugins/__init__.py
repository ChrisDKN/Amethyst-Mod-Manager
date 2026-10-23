"""Protocol client for Amethyst manager plugins."""

from .client import Client, PluginError
from .i18n import Translator

__all__ = ["Client", "PluginError", "Translator"]
