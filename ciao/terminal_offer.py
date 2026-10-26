"""The suggested terminal command a chat may carry — and nothing else.

A terminal offer is *guidance*: plain text the assistant proposes so the user
can read it, copy it and run it themselves. It is never executed, never
typed into a shell and never sent to a host terminal. A command can name a
credential, a token or a private path, so the state that holds one is
transient and in memory only: a restart, an archive, a new provider session,
a handover or an explicit dismissal ends it, and the command never reaches
Ciaobot's own chat registry, its audit log or a backup of either. It can
still appear in the provider's tool-call transcript, which is the provider's
record of the arguments it was handed and not a record the engine rewrites.

This module is the *text* half and it is deliberately pure: it validates and
normalizes a command, and it mints the id that names one. It knows nothing
about chats, events, persistence or the wire.

**The text is untrusted.** It is model-authored, and a model can be talked
into emitting a right-to-left override, a zero-width space or a control
character. Those are invisible on the card and change what the user copies
into their shell, so they are refused rather than displayed. What survives is
plain, printable text.

**The refusal says nothing about the text.** A rejected command is not
echoed into the error, the log or any telemetry: it can carry a secret, and
an error string is exactly where a secret would leak. The refusal carries a
closed reason instead, so a caller can map it to a status without parsing
prose.

**The id is random, not a counter.** A dismissal quotes the id it saw, so
two devices can race: one dismisses, the assistant offers the same command
again, and the other device's stale dismissal must not remove the new
offer. A counter that restarts with the process would repeat itself across
a restart and hand exactly that race a win. A fresh 128-bit random id per
accepted change never repeats.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import overload

#: The largest command that is accepted, in UTF-8 bytes, before and after
#: normalization. A command is a copyable snippet, not a file: a bound
#: keeps one offer from carrying an arbitrary payload through the chat
#: payload, the event and every client that caches it.
TERMINAL_OFFER_MAX_BYTES = 4096

#: The id is an identifier, not a credential: 16 random bytes (128 bits) is
#: unguessable enough that a stale client cannot name someone else's offer.
_TERMINAL_OFFER_ID_BYTES = 16

#: The characters a command may contain besides ordinary printable text.
#: A line feed ends a line of a multi-line command; a tab indents one; a
#: carriage return is a line ending that is normalized away below.
_ALLOWED_CONTROL_CHARACTERS = frozenset("\n\r\t")


class TerminalOfferError(ValueError):
    """A typed refusal from the terminal-offer text contract.

    ``reason`` is a closed vocabulary, so a caller can map it to a status
    code without parsing prose:

    - ``invalid_type``: the value is not text at all.
    - ``invalid_encoding``: the text has no UTF-8 encoding (a lone
      surrogate).
    - ``too_long``: the command is over the byte bound.
    - ``empty``: nothing is left after normalization.
    - ``invalid_characters``: a control or otherwise non-printable
      character.

    The message is the reason and nothing else. The rejected command never
    appears in it: a command can carry a credential, and an error string
    ends up in a log line and a telemetry field.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_terminal_offer(command: str) -> str:
    """Validate ``command`` and return the text an offer may hold.

    Line endings are normalized (CRLF and a bare CR become a line feed) and
    the outer whitespace is stripped, so two spellings of the same command
    are the same command. The bound is counted in UTF-8 bytes, before and
    after normalization, because that is how the text is stored and copied.

    Raises :class:`TerminalOfferError` with a static reason for anything
    else. The rejected text is never part of the error.
    """
    if not isinstance(command, str):
        raise TerminalOfferError("invalid_type")
    try:
        raw = command.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate: the text has no UTF-8 form, so it cannot be
        # copied into a shell or stored as bytes.
        raise TerminalOfferError("invalid_encoding") from None
    if len(raw) > TERMINAL_OFFER_MAX_BYTES:
        raise TerminalOfferError("too_long")
    for character in command:
        if character in _ALLOWED_CONTROL_CHARACTERS:
            continue
        if not character.isprintable():
            # C0 and C1 controls, DEL, and every invisible or
            # direction-changing character (Bidi controls, format
            # characters, line and paragraph separators).
            raise TerminalOfferError("invalid_characters")
    normalized = command.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise TerminalOfferError("empty")
    if len(normalized.encode("utf-8")) > TERMINAL_OFFER_MAX_BYTES:
        raise TerminalOfferError("too_long")
    return normalized


@dataclass(frozen=True, slots=True)
class TerminalOffer:
    """One suggested command, named by a random id.

    Frozen because it is shared state: a caller that could edit it in place
    would change what every other reader of the offer sees.

    The command is kept out of the repr. A repr is what ends up in a log
    line, a traceback and a debugger, so an offer that is printed carries
    its id — which names nothing — and not the command, which can name a
    credential.
    """

    id: str
    command: str = field(repr=False)


def new_terminal_offer(command: str) -> TerminalOffer:
    """Normalize ``command`` and mint a fresh offer for it.

    The id is random per call, never derived from the text: the same command
    offered again (after a dismissal, or after a restart) is a *different*
    offer, and a stale client must not be able to dismiss it by quoting the
    id it saw last time.
    """
    return TerminalOffer(
        id=secrets.token_hex(_TERMINAL_OFFER_ID_BYTES),
        command=normalize_terminal_offer(command),
    )


# The payload is spelled three times so each caller gets the shape it can
# rely on: an offer always has a payload, no offer never does, and a value
# that may be either says so. Without this, a caller holding an offer would
# still have to narrow the result by hand.
@overload
def terminal_offer_payload(offer: TerminalOffer) -> dict[str, str]: ...


@overload
def terminal_offer_payload(offer: None) -> None: ...


@overload
def terminal_offer_payload(
    offer: TerminalOffer | None,
) -> dict[str, str] | None: ...


def terminal_offer_payload(offer: TerminalOffer | None) -> dict[str, str] | None:
    """The wire shape of an offer: ``{id, command}`` or nothing.

    A fresh dict for every caller, so a consumer that edits the payload
    cannot reach the state through it, and two callers never share one.
    """
    if offer is None:
        return None
    return {"id": offer.id, "command": offer.command}
