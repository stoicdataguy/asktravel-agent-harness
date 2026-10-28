import pytest

from travel_agent.agent.graph import render_system_prompt
from travel_agent.skills import load_skill, load_skills, parse_skill

EXPECTED = {
    "budget-planner",
    "food-and-culture",
    "itinerary-builder",
    "local-transport",
    "packing-list",
    "trip-intake",
    "visa-and-entry",
}


def test_all_skills_load_with_matching_names():
    skills = load_skills()
    assert set(skills) == EXPECTED
    for skill in skills.values():
        assert skill.description.startswith("Use when")
        assert skill.body.startswith("# ")


def test_parse_skill_frontmatter():
    skill = parse_skill("---\nname: demo\ndescription: Use when testing.\n---\n\n# Demo\nBody")
    assert (skill.name, skill.description, skill.body) == (
        "demo",
        "Use when testing.",
        "# Demo\nBody",
    )


def test_parse_skill_requires_frontmatter():
    with pytest.raises(ValueError):
        parse_skill("# No frontmatter")


def test_load_skill_returns_body():
    body = load_skill.invoke({"name": "packing-list"})
    assert body.startswith("# Packing list")
    assert "name:" not in body.split("\n", 1)[0]


def test_load_skill_unknown_lists_available():
    out = load_skill.invoke({"name": "scuba"})
    assert "No skill named 'scuba'" in out
    assert "trip-intake" in out


def test_system_prompt_lists_every_skill():
    prompt = render_system_prompt()
    for name in EXPECTED:
        assert f"- {name}: Use when" in prompt
