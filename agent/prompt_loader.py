"""Load and fill the Markdown files the agent uses.

  agent/prompts/    what the LLM reads        (system, trigger_message, forced_decision)
  agent/templates/  layouts code fills in     (escalation_report, pr_body)

Both are plain Markdown with {placeholders}. Keeping them out of the code means
they can be read, edited and diffed on their own. render() refuses to run if a
placeholder has no value, so a typo fails loudly, not silently.
"""

import re
from pathlib import Path

FOLDERS = (Path(__file__).parent / "prompts", Path(__file__).parent / "templates")
_PLACEHOLDER = re.compile(r"{(\w+)}")


def load(name: str) -> str:
    for folder in FOLDERS:
        if (folder / f"{name}.md").exists():
            return (folder / f"{name}.md").read_text()
    raise FileNotFoundError(f"no prompt or template named {name!r}")


def placeholders(name: str) -> set[str]:
    return set(_PLACEHOLDER.findall(load(name)))


def render(name: str, **values: object) -> str:
    missing = placeholders(name) - values.keys()
    if missing:
        raise KeyError(f"prompt {name!r} needs values for: {sorted(missing)}")
    return load(name).format(**values)
