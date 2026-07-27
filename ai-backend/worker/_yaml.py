# SPDX-License-Identifier: AGPL-3.0
"""Shared YAML helpers used by both `worker/config.py` and
`worker/config_hot.py`. Keeping these in one module avoids the
duplicated `_expand_env` we had pre-cleanup."""

from __future__ import annotations

import os


def expand_env(text: str) -> str:
    """Expand `${VAR}` references in `text`.

    Missing environment variables are left literal (e.g. `${MISSING}`).
    Manual expansion (rather than `os.path.expandvars`) gives us one
    fail-safe behaviour across both loaders: ${MISSING} stays visible
    in the parsed config so a forgotten env var surfaces during
    validation instead of silently turning into an empty string.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "$" and i + 1 < len(text) and text[i + 1] == "{":
            end = text.find("}", i + 2)
            if end == -1:
                out.append(text[i])
                i += 1
                continue
            name = text[i + 2 : end]
            out.append(os.environ.get(name, f"${{{name}}}"))
            i = end + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


__all__: tuple[str, ...] = ("expand_env",)
