"""Keep CLI reports printable on legacy Windows consoles and redirected cp1252 streams."""

import sys


def configure_console() -> None:
    # Preserve the terminal's encoding; unsupported mathematical symbols are escaped, not a fatal error.
    # Saved Markdown/JSON reports remain UTF-8. Captured streams (e.g. tests) may not support reconfigure.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
