"""Textual user-interface package for the local TUI channel.

`nanocat.channels.tui` stays the thin channel adapter (bus bridge, log sink,
control-port wiring); everything under this package is Textual UI code and is
never imported by the runtime when the TUI is not used.
"""
