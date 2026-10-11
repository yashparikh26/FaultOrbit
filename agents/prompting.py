"""Load agent prompts from agents/prompts/<name>.md.

Prompts live in files, not code, so they can be reviewed and versioned on their
own. complete_json logs a short hash of each system prompt (its version), so a
change in output can be traced to a change in the prompt.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name, **values):
    """Return the prompt text, with {placeholders} filled from `values`."""
    path = PROMPTS_DIR / f"{Path(name).name}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt named {name!r} in {PROMPTS_DIR}")
    text = path.read_text(encoding="utf-8")
    return text.format(**values) if values else text
