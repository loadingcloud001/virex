# SPDX-License-Identifier: Apache-2.0
"""Edge-agent YAML helpers.

Kept separate from ai-backend/worker/_yaml.py because the two projects
ship in separate containers and don't share a Python module path.
The behaviour here MUST stay in sync with `worker._yaml.expand_env()`
so both loaders see the same effective YAML — otherwise pydantic
validation in the worker can succeed (env-expanded values) while
edge-agent's config_watcher fails (raw `${VAR}` placeholders), which
silently breaks Tier A/B reloads.
"""

from __future__ import annotations

import os


def expand_env(text: str) -> str:
    """Expand `${VAR}` and `${VAR:-default}` references in `text`.

    - `${VAR}` — replaced by `os.environ[VAR]`. Missing vars stay literal
      (`${MISSING}`) so the pydantic validator surfaces a forgotten
      env var instead of silently turning it into an empty string.
    - `${VAR:-default}` — replaced by `os.environ[VAR]` if set, else
      by the literal default. Operator can leave the var blank in
      `.env.example` when a sensible default is available.

    Manual expansion (rather than `os.path.expandvars`) gives us one
    fail-safe behaviour across all loaders.

    Keep this function byte-for-byte equivalent to
    `ai-backend/worker/_yaml.py:expand_env` — see the docstring there
    for the full design rationale.
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