"""The prompts the owner can change. Each has a built-in text in the package; the owner's own, if they save one,
is kept in data/prompts/<name>.md and used instead, until they go back to the built-in. {owner} in a prompt is
replaced with the owner's name. Read when used, so a change reaches the next message."""

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
NAMES = {"rewrite": "rewrite_prompt.md"}
MAX_CHARS = 20000
# What the worker checks the rewrite's script for, so a prompt that never mentions it fails every message
# with a clip instead of the first one that has one.
REQUIRED_WORDS = {"rewrite": ("[[audio",)}


class PromptError(ValueError):
    pass


class Prompts:
    def __init__(self, root: Path, owner: str):
        self.dir = root / "prompts"
        self.owner = owner

    def _known(self, name: str) -> None:
        if name not in NAMES:
            raise PromptError(f"no prompt {name!r}; prompts: {', '.join(NAMES)}")

    def path(self, name: str) -> Path:
        """Where the owner's version of this prompt is kept, whether or not there is one."""
        self._known(name)
        return self.dir / f"{name}.md"

    def builtin(self, name: str) -> str:
        self._known(name)
        return (PACKAGE / NAMES[name]).read_text()

    def custom(self, name: str) -> str | None:
        """The owner's text, or None while the built-in is in use."""
        path = self.path(name)
        return path.read_text() if path.exists() else None

    def template(self, name: str) -> str:
        """What is in use, with {owner} not yet filled in."""
        custom = self.custom(name)
        return self.builtin(name) if custom is None else custom

    def text(self, name: str) -> str:
        """What is in use, as the model gets it."""
        return self.template(name).replace("{owner}", self.owner)

    def check(self, name: str, text) -> str:
        if not isinstance(text, str):
            raise PromptError("send the prompt as text")
        text = text.replace("\r\n", "\n").strip() + "\n"
        if len(text) <= 1:
            raise PromptError("the prompt is empty; to go back to the built-in one, use the built-in")
        if len(text) > MAX_CHARS:
            raise PromptError(f"the prompt is {len(text)} characters; the most is {MAX_CHARS}")
        for word in REQUIRED_WORDS.get(name, ()):
            if word not in text:
                raise PromptError(f"the {name} prompt must say what to do with audio markers such as [[audio 1]]: "
                                  f"keep the paragraph about them, or every message with a clip fails")
        return text

    def change(self, name: str, text) -> None:
        """Keep the owner's text, once checked."""
        text = self.check(name, text)
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)

    def reset(self, name: str) -> None:
        """Go back to the built-in text."""
        self.path(name).unlink(missing_ok=True)
