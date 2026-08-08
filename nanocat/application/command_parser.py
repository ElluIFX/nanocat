"""Pure slash-command classifier and parser."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from nanocat.core.commands import CommandClassification, ParsedCommand

_COMMAND_RE = re.compile(
    r"^/(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:@(?P<bot>[A-Za-z0-9_-]+))?(?:\s+(?P<args>.*))?$",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ClassifiedText:
    """Classification result before registry lookup."""

    kind: CommandClassification
    text: str
    name: str | None = None


class CommandParseError(ValueError):
    """Raised when command syntax cannot be parsed deterministically."""


class CommandParser:
    """Parse command syntax without reading state or executing side effects."""

    @staticmethod
    def classify(text: str) -> ClassifiedText:
        stripped = text.lstrip()
        if stripped.startswith("//"):
            return ClassifiedText(CommandClassification.ESCAPED_TEXT, stripped[1:])
        if not stripped.startswith("/"):
            return ClassifiedText(CommandClassification.ORDINARY_TEXT, text)
        match = _COMMAND_RE.match(stripped)
        if not match:
            return ClassifiedText(CommandClassification.COMMAND, stripped, "")
        return ClassifiedText(
            CommandClassification.COMMAND,
            stripped,
            match.group("name").lower(),
        )

    @classmethod
    def parse(cls, text: str) -> ParsedCommand:
        classified = cls.classify(text)
        if classified.kind is not CommandClassification.COMMAND:
            raise CommandParseError("text is not a command candidate")
        match = _COMMAND_RE.match(classified.text)
        if not match:
            raise CommandParseError("command name must start with an ASCII letter")

        raw_args = match.group("args") or ""
        try:
            tokens = shlex.split(raw_args, posix=True)
        except ValueError as exc:
            raise CommandParseError(str(exc)) from exc

        positional: list[str] = []
        options: dict[str, str | bool] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("--") and len(token) > 2:
                key, separator, value = token[2:].partition("=")
                if separator:
                    options[key.lower()] = value
                elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                    options[key.lower()] = tokens[index + 1]
                    index += 1
                else:
                    options[key.lower()] = True
            else:
                positional.append(token)
            index += 1

        return ParsedCommand(
            raw_text=text,
            name=match.group("name").lower(),
            canonical_name=match.group("name").lower(),
            subcommand=positional[0].lower() if positional else None,
            positional_args=tuple(positional[1:] if positional else ()),
            options=options,
        )
