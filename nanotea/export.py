"""The board as one markdown file, for keeping or sharing: each message as sent, with the owner's answer."""

from datetime import datetime

from nanotea.index import thread_key
from nanotea.store import Message, Store, show_markers


def markdown_of(store: Store, msgs: list[Message], heading: str, who) -> str:
    """msgs, in the order given (the caller picks oldest first); who(sender): the name to show."""
    out = [f"# {heading}", "", f"{len(msgs)} message{'' if len(msgs) == 1 else 's'}, exported "
           f"{datetime.now().astimezone().isoformat(timespec='minutes')}.", ""]
    for msg in msgs:
        title = msg.title_hint or msg.title or msg.id
        kind = "Question" if msg.ask else "Message"
        where = f" in #{msg.channel}" if msg.channel else ""
        out += [f"## {title}", "", f"{kind} from {who(msg.sender)}{where}, {msg.created}. Id {msg.id}.", ""]
        if msg.re:
            to = "the owner" if msg.re.get("kind") == "owner" else who(msg.re["sender"])
            out += [f"Replying to {to}: \"{msg.re['title'] or 'a recording'}\" (id {msg.re['id']}, thread "
                    f"{thread_key(msg.re)}).", ""]
        text = store.read_text(msg.id, "original.md") or ""
        out += [show_markers(text, msg.attachments).rstrip(), ""]
        if msg.error:
            out += [f"Failed: {msg.error}", ""]
        reply = store.reply(msg.id)
        if reply:
            said = reply.get("text") or reply.get("transcript") or "(a recording with no transcript)"
            files = [f["name"] for f in reply.get("files") or []]
            out += [f"Answer, {reply['at']}:", "", said.rstrip(), ""]
            if files:
                out += [f"Attached: {', '.join(files)}", ""]
    return "\n".join(out)
