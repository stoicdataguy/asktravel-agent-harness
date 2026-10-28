"""Skills: markdown playbooks the agent loads on demand (progressive disclosure).

Each `<name>.md` in this directory starts with YAML frontmatter holding `name` and `description`.
Only names and descriptions go into the system prompt; the agent calls `load_skill(name)` to pull
a skill's full instructions into the conversation when a request matches its description.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from langchain.tools import tool

SKILLS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


def parse_skill(text: str) -> Skill:
    if not text.startswith("---\n"):
        raise ValueError("Skill file must start with '---' YAML frontmatter")
    frontmatter, body = text[4:].split("\n---\n", 1)
    meta = yaml.safe_load(frontmatter)
    return Skill(name=meta["name"], description=meta["description"].strip(), body=body.strip())


@lru_cache
def load_skills(skills_dir: Path = SKILLS_DIR) -> dict[str, Skill]:
    skills = {}
    for path in sorted(skills_dir.glob("*.md")):
        skill = parse_skill(path.read_text(encoding="utf-8"))
        if skill.name != path.stem:
            raise ValueError(f"{path.name}: frontmatter name {skill.name!r} must match filename")
        skills[skill.name] = skill
    return skills


def skills_index() -> str:
    """Bullet list of skill names and descriptions for the system prompt."""
    return "\n".join(f"- {s.name}: {s.description}" for s in load_skills().values())


@tool
def load_skill(name: str) -> str:
    """Load a skill's full instructions by name (see the skills list in your instructions).

    Call this before answering whenever the user's request matches a skill's description, then
    follow the returned instructions.
    """
    skills = load_skills()
    if skill := skills.get(name.strip().lower()):
        return skill.body
    return f"No skill named {name!r}. Available skills: {', '.join(skills)}"
