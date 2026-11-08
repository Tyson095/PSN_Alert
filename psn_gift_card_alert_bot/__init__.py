"""PlayStation gift card monitoring package.

``core`` holds the shared monitoring logic used by both the GitHub Actions
one-shot runner (``github_runner.py`` at the repository root) and the optional
long-running local process (``bot.py``).
"""

from __future__ import annotations

__all__ = ["core"]
