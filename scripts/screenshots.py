"""Regenerate the README screenshots in docs/.

    uv run scripts/screenshots.py

Each one is the real TUI, run headlessly over the starter prompts from `pil examples`.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SIZE = (112, 32)

# What to press before each picture is taken.
SHOTS = {
    "browser": ["down", "down", "down"],
    "form": [
        "down",
        "enter",
        *"ABC-123",
        "tab",
        "enter",
        "down",
        "enter",
        "tab",
        *"the flaky login test",
    ],
    "editor": ["escape", "e"],
}


async def main() -> None:
    from pil import config
    from pil.examples import EXAMPLES
    from pil.store import Library
    from pil.tui.app import PilApp

    library = Library(config.prompts_dir())
    for name, text in EXAMPLES.items():
        path = library.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    app = PilApp(library, config.Config())
    async with app.run_test(size=SIZE) as pilot:
        for name, keys in SHOTS.items():
            await pilot.press(*keys)
            await pilot.pause(0.2)
            app.save_screenshot(str(DOCS / f"{name}.svg"))
            print(f"wrote docs/{name}.svg")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as home:
        os.environ["PIL_HOME"] = home
        asyncio.run(main())
