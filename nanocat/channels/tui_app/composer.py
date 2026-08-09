"""Chat composer: multi-line input with attachment/paste staging."""

from __future__ import annotations

import re
from collections.abc import Callable

from rich.style import Style
from textual import events
from textual.message import Message
from textual.widgets import TextArea

from nanocat.channels.tui_app.events import grab_clipboard_images

_HISTORY_LIMIT = 200
_PASTE_BUFFER_LIMIT = 32
_MEDIA_BUFFER_LIMIT = 16


class ChatInput(TextArea):
    """Multi-line chat input: Enter submits, Shift/Alt+Enter inserts a newline.

    Up/Down navigate input history when the text is single-line (no newlines).
    """

    _NEWLINE_KEYS = frozenset({"shift+enter", "alt+enter", "ctrl+enter", "ctrl+j"})

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    # Placeholder token regexes (capture group 1 = 1-based index into the buffer).
    _IMAGE_RE = r"\[Image (\d+)\]"
    _PASTE_RE = r"\[Pasted text #(\d+) \+\d+ lines\]"

    def on_mount(self) -> None:
        self._history: list[str] = []
        self._hist_idx: int = -1  # -1 = live input, 0..n-1 = navigating
        self._draft: str = ""  # saved text before entering history nav
        self._pending_media: list[str] = []  # clipboard images staged for the next send
        self._pending_pastes: list[str] = []  # large pasted text blocks staged for the send
        self._reconciling = False  # guard against the text-rewrite re-entering reconcile

    def attach_clipboard_image(self) -> bool:
        """If the OS clipboard holds an image, stage it and insert an
        ``[Image N]`` placeholder. Returns True when an image was attached
        (caller should swallow the paste); False lets normal text paste run."""
        paths = grab_clipboard_images()
        if not paths:
            return False
        for path in paths:
            if len(self._pending_media) >= _MEDIA_BUFFER_LIMIT:
                break
            self._pending_media.append(path)
            self.insert(f"[Image {len(self._pending_media)}] ")
        return True

    def take_pending_media(self) -> list[str]:
        """Return staged media whose ``[Image N]`` placeholder is still in the
        text (so deleting a placeholder drops its image), then reset."""
        text = self.text
        media = [p for i, p in enumerate(self._pending_media, 1) if f"[Image {i}]" in text]
        self._pending_media = []
        return media

    def take_pending_pastes(self, text: str) -> str:
        """Expand surviving ``[Pasted text #N ...]`` placeholders in *text* back
        to the full stored block (so the agent gets the real content), then
        reset. Deleted placeholders simply drop their block."""

        def _expand(m: "re.Match[str]") -> str:
            i = int(m.group(1))
            return self._pending_pastes[i - 1] if 1 <= i <= len(self._pending_pastes) else m[0]

        expanded = re.sub(self._PASTE_RE, _expand, text)
        self._pending_pastes = []
        return expanded

    def _reconcile_kind(
        self, buffer: list, pattern: str, renumber: "Callable[[int, re.Match[str]], str]"
    ) -> list:
        """Drop buffer entries whose placeholder was deleted from the text and
        renumber the survivors so the visible ids stay contiguous (delete 2 of
        1,2,3 → remaining become 1,2). The buffer is the source of truth; only
        its still-referenced entries survive, in order. Rewrites the text only
        when a placeholder actually vanished. Returns the trimmed buffer."""
        if self._reconciling or not buffer:
            return buffer
        text = self.text
        n = len(buffer)
        present = {int(m.group(1)) for m in re.finditer(pattern, text) if 1 <= int(m.group(1)) <= n}
        if len(present) == n:
            return buffer  # every staged entry still referenced — nothing deleted
        survivors = [i for i in range(1, n + 1) if i in present]
        remap = {old: new for new, old in enumerate(survivors, 1)}
        new_buffer = [buffer[old - 1] for old in survivors]
        new_text = re.sub(
            pattern,
            lambda m: renumber(remap[int(m.group(1))], m) if int(m.group(1)) in remap else m[0],
            text,
        )
        if new_text != text:
            cursor = self.cursor_location  # renumbered tokens sit after it → col stays valid
            self._reconciling = True
            try:
                self.text = new_text
                self.move_cursor(cursor)
            finally:
                self._reconciling = False
        return new_buffer

    def _reconcile_placeholders(self) -> None:
        self._pending_media = self._reconcile_kind(
            self._pending_media, self._IMAGE_RE, lambda new, m: f"[Image {new}]"
        )
        self._pending_pastes = self._reconcile_kind(
            self._pending_pastes,
            self._PASTE_RE,
            # Replace only the #N index, preserving the "+M lines" suffix.
            lambda new, m: re.sub(r"#\d+", f"#{new}", m[0]),
        )

    def _active_token_spans(self, line: str) -> list[tuple[int, int]]:
        """Char spans of *real* placeholder tokens (images + pasted blocks) on a
        line — only those whose index is currently staged, so hand-typed lookalikes
        stay ordinary text. The buffers are the single source of truth."""
        spans: list[tuple[int, int]] = []
        for pat, buf in (
            (self._IMAGE_RE, self._pending_media),
            (self._PASTE_RE, self._pending_pastes),
        ):
            n = len(buf)
            spans += [
                (m.start(), m.end())
                for m in re.finditer(pat, line)
                if 1 <= int(m.group(1)) <= n
            ]
        return spans

    def _build_highlight_map(self) -> None:
        # Runs after every edit; tint real placeholder tokens so they read as
        # attachments, not prose (images blue, pasted blocks cyan). render_line
        # wants BYTE offsets, so re-derive spans from the UTF-8 line (the
        # placeholders are ASCII, landing on clean codepoint boundaries even
        # when CJK text precedes them).
        super()._build_highlight_map()
        theme = getattr(self, "_theme", None)  # unset during __init__'s first call
        if theme is None:
            return
        theme.syntax_styles.setdefault("nanocat_image", Style(color="bright_blue", bold=True))
        theme.syntax_styles.setdefault("nanocat_paste", Style(color="bright_cyan", bold=True))
        kinds = (
            (rb"\[Image (\d+)\]", len(self._pending_media), "nanocat_image"),
            (
                rb"\[Pasted text #(\d+) \+\d+ lines\]",
                len(self._pending_pastes),
                "nanocat_paste",
            ),
        )
        for row, line in enumerate(self.document.lines):
            lb = line.encode("utf-8")
            for pat, n, style in kinds:
                for m in re.finditer(pat, lb):
                    if 1 <= int(m.group(1)) <= n:
                        self._highlights[row].append((m.start(), m.end(), style))

    # Treat real placeholder tokens as atomic: left/right cursor movement and
    # backspace/delete (which both derive their target from these two hooks)
    # jump over / remove the whole placeholder instead of one char at a time.
    # Cursor columns are character indices, so str-regex spans align directly.
    def get_cursor_left_location(self) -> "tuple[int, int]":
        row, col = self.cursor_location
        for start, end in self._active_token_spans(self.document.lines[row]):
            if start < col <= end:
                return (row, start)
        return super().get_cursor_left_location()

    def get_cursor_right_location(self) -> "tuple[int, int]":
        row, col = self.cursor_location
        for start, end in self._active_token_spans(self.document.lines[row]):
            if start <= col < end:
                return (row, end)
        return super().get_cursor_right_location()

    async def _on_paste(self, event: events.Paste) -> None:
        # Terminal bracketed paste: an image on the clipboard yields no text,
        # so peek the OS clipboard first and attach it instead of inserting.
        if self.attach_clipboard_image():
            event.stop()
            event.prevent_default()
            return
        # A multi-line block is collapsed to a [Pasted text #N +M lines]
        # token; the full text is staged and re-expanded on submit. Single-line
        # pastes insert normally. Bracketed paste uses CR for line breaks, so
        # normalize before deciding and staging.
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" in text.strip():
            event.stop()
            event.prevent_default()
            if len(self._pending_pastes) >= _PASTE_BUFFER_LIMIT:
                self._pending_pastes.pop(0)
            self._pending_pastes.append(text)
            lines = text.strip().count("\n") + 1
            self.insert(f"[Pasted text #{len(self._pending_pastes)} +{lines} lines] ")
            return
        await super()._on_paste(event)

    def action_paste(self) -> None:
        # Ctrl+V binding: same image-first check before the normal paste.
        if self.attach_clipboard_image():
            return
        super().action_paste()

    def _push_history(self, text: str) -> None:
        if not text.strip():
            return
        self._history.append(text)
        if len(self._history) > _HISTORY_LIMIT:
            del self._history[: len(self._history) - _HISTORY_LIMIT]

    def submit_current(self) -> None:
        """Submit the current text (shared by Enter and the Send button)."""
        text = self.text
        self._push_history(text)
        self._hist_idx = -1
        self._draft = ""
        self.post_message(self.Submitted(text))

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+c" and not self.selected_text:
            # No selection: Ctrl+C is the quit gesture (double-press), not copy.
            event.stop()
            event.prevent_default()
            quit_hint = getattr(self.app, "action_quit_hint", None)
            if callable(quit_hint):
                quit_hint()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.submit_current()
            return
        if event.key in self._NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key in ("up", "down") and "\n" not in self.text:
            if event.key == "up" and self._history:
                event.stop()
                event.prevent_default()
                if self._hist_idx == -1:
                    self._draft = self.text
                    self._hist_idx = len(self._history) - 1
                elif self._hist_idx > 0:
                    self._hist_idx -= 1
                self.text = self._history[self._hist_idx]
                self.move_cursor((0, len(self.text)))
                return
            if event.key == "down" and self._hist_idx != -1:
                event.stop()
                event.prevent_default()
                self._hist_idx += 1
                if self._hist_idx >= len(self._history):
                    self._hist_idx = -1
                    self.text = self._draft
                else:
                    self.text = self._history[self._hist_idx]
                self.move_cursor((0, len(self.text)))
                return
        await super()._on_key(event)
