"""Exceptions an integration may raise with a message meant to be shown.

Not part of Contract 1 (``base.py``): an optional helper. By default the poll collector
shows only an exception's type name, because exception text can carry URLs, headers or
response bodies. :class:`PollFailure` is the one explicit opt-in: its message is shown
as the instance status ``error``.
"""


class PollFailure(Exception):
    """``poll()`` got no usable answer, and the message says why in words an admin can use.

    The collector shows ``str(exc)`` as the instance status ``error`` (and logs it), after
    scrubbing decrypted secret values and capping the length. The integration author
    writes the message and is responsible for it: fixed text plus, at most, the host as
    configured or a checked address. Never a secret, another exception's text, a URL with
    a query string, or device-supplied text. The scrub is a last line of defence and
    only an exact, case-sensitive match of each decrypted secret value: a secret that
    is encoded or transformed (base64, URL-encoded, upper-cased, hashed, split) is not
    caught, and keeping it out is the author's responsibility.

    Subclass it with a distinct name (``NoReply``), so tests and logs can tell failures
    apart. An empty message falls back to ``poll failed (<Type>)``.
    """
