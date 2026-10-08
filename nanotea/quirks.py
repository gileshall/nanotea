"""What differs between harnesses, as data: where nanotea is tuned for each.

nanotea mcp --harness NAME takes its defaults from that harness's quirks, and nanotea setup NAME writes the flag,
so a quirk added here reaches every session at its next upgrade (docs/service.md#upgrading). Each says how it was
checked: against the live harness, or only from its docs.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Quirks:
    idle: str = "finish"  # with nothing to do: "hook" where nanotea setup installs a wake hook
    wait_s: int = 50  # wait's default timeout: under the 60 s many harnesses allow a tool call
    instructions_max: int | None = None  # characters of a server's instructions the model gets; None: all of them


QUIRKS: dict[str, Quirks] = {
    # idle: setup installs the rewake hook. instructions_max: live, its MCP log says "Server instructions truncated
    # from 2960 to 2048 chars", and the model gets none of the rest.
    "claude": Quirks(idle="hook", instructions_max=2048),
    "codex": Quirks(),
    "cursor": Quirks(),
    # setup gives the server a 600 s timeout (gemini mcp add --timeout 600000). From the docs.
    "gemini": Quirks(wait_s=300),
    "opencode": Quirks(),
    # Goose allows a tool call 300 s. From the docs.
    "goose": Quirks(wait_s=240),
    "vscode": Quirks(),
    "zed": Quirks(),
    "cline": Quirks(),
    "amp": Quirks(),
    "other": Quirks(),
}


def of(harness: str | None) -> Quirks:
    """harness's quirks; with none named, the defaults, which suit a harness that keeps everything."""
    return Quirks() if harness is None else QUIRKS[harness]
