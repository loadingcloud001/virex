# SPDX-License-Identifier: AGPL-3.0
"""Shared YAML helpers used by both `worker/config.py` and
`worker/config_hot.py`. Keeping these in one module avoids the
duplicated `_expand_env` we had pre-cleanup."""

from __future__ import annotations

import os


def expand_env(text: str) -> str:
    """Expand `${VAR}` and `${VAR:-default}` references in `text`.

    - `${VAR}` — replaced by `os.environ[VAR]`. Missing vars are left
      literal (`${MISSING}`) so a forgotten env var surfaces during
      pydantic validation instead of silently turning into empty.
    - `${VAR:-default}` — replaced by `os.environ[VAR]` if set,
      otherwise by the literal default. Missing env var with a default
      does NOT surface as a validation error (it's intentional that
      the operator can leave it blank in `.env.example`).

    Manual expansion (rather than `os.path.expandvars`) gives us one
    fail-safe behaviour across both loaders.
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
            spec = text[i + 2 : end]
            if ":-" in spec:
                name, default = spec.split(":-", 1)
                out.append(os.environ.get(name, default))
            else:
                name = spec
                out.append(os.environ.get(name, f"${{{name}}}"))
            i = end + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


__all__: tuple[str, ...] = ("expand_env",)
