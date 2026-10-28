"""Structured itinerary output: the schema the agent fills via `present_itinerary`, and its
markdown rendering for chat UIs.

Field descriptions double as instructions to the LLM, since the schema is sent as the tool's
argument spec.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

TimeOfDay = Literal["morning", "afternoon", "evening", "full day"]


class Activity(BaseModel):
    time_of_day: TimeOfDay
    title: str = Field(description="Short name, e.g. 'Snorkel Kealakekua Bay'.")
    details: str = Field(description="1-2 sentences: what to do, tips, timing.")
    location: str | None = Field(default=None, description="Neighbourhood, town or site.")
    source: str | None = Field(
        default=None, description="Guidebook citation, e.g. 'Lonely Planet Hawaii, p. 90'."
    )


class DayPlan(BaseModel):
    day: int = Field(ge=1, description="Day number, starting at 1.")
    title: str = Field(description="Theme or area of the day, e.g. 'Kona coast'.")
    activities: list[Activity] = Field(min_length=1)
    base: str | None = Field(default=None, description="Where the traveller sleeps that night.")


class Stay(BaseModel):
    area: str = Field(description="Town or neighbourhood to base yourself in.")
    nights: int = Field(ge=1)
    why: str = Field(description="One line on why this base suits the plan.")


class Itinerary(BaseModel):
    """A complete day-by-day trip plan."""

    title: str = Field(description="e.g. '3 days on the Big Island'.")
    destination: str
    trip_length_days: int = Field(ge=1)
    travellers: str | None = Field(default=None, description="e.g. '2 adults, mid-range'.")
    summary: str = Field(description="2-3 sentences on the shape of the trip.")
    assumptions: list[str] = Field(
        default_factory=list, description="Assumptions made where the user gave no details."
    )
    stays: list[Stay] = Field(default_factory=list)
    days: list[DayPlan] = Field(min_length=1)
    bookings_to_make: list[str] = Field(
        default_factory=list, description="Things to reserve ahead (tours, permits, trains)."
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Possibly outdated info (old guidebook prices/hours), conditions to check.",
    )

    @model_validator(mode="after")
    def days_cover_the_trip(self) -> Self:
        numbers = [d.day for d in self.days]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"days must be numbered 1..N in order, got {numbers}")
        if len(numbers) != self.trip_length_days:
            raise ValueError(
                f"trip_length_days is {self.trip_length_days} but {len(numbers)} days were planned"
            )
        return self


def _activity_line(activity: Activity) -> str:
    where = f" ({activity.location})" if activity.location else ""
    source = f" *({activity.source})*" if activity.source else ""
    heading = f"{activity.time_of_day.capitalize()}: {activity.title}"
    return f"- **{heading}**{where}: {activity.details}{source}"


def render_itinerary(itinerary: Itinerary) -> str:
    """Markdown for chat UIs, following the itinerary-builder skill's layout."""
    lines = [f"# {itinerary.title}", ""]
    meta = [itinerary.destination, f"{itinerary.trip_length_days} days"]
    if itinerary.travellers:
        meta.append(itinerary.travellers)
    lines += [f"*{' · '.join(meta)}*", "", itinerary.summary, ""]
    if itinerary.assumptions:
        lines += ["**Assumptions**", ""] + [f"- {a}" for a in itinerary.assumptions] + [""]
    if itinerary.stays:
        lines += ["**Where to stay**", ""]
        lines += [f"- **{s.area}** ({s.nights} nights): {s.why}" for s in itinerary.stays]
        lines.append("")
    for day in itinerary.days:
        lines += [f"## Day {day.day} · {day.title}", ""]
        lines += [_activity_line(a) for a in day.activities]
        if day.base:
            lines.append(f"- **Sleep:** {day.base}")
        lines.append("")
    if itinerary.bookings_to_make:
        lines += ["## Book ahead", ""] + [f"- {b}" for b in itinerary.bookings_to_make] + [""]
    if itinerary.caveats:
        lines += ["## Check before you go", ""] + [f"- {c}" for c in itinerary.caveats] + [""]
    return "\n".join(lines).strip()
