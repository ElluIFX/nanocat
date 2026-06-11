"""Chat channels module with plugin architecture."""

from nanocat.channels.base import BaseChannel
from nanocat.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
