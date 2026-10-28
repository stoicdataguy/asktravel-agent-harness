"""Structured itinerary output: the schema the agent fills via `present_itinerary`, and its
markdown rendering for chat UIs.

Field descriptions double as instructions to the LLM, since the schema is sent as the tool's
argument spec.
"""

from datetime import date, timedelta
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


class FlightLeg(BaseModel):
    from_airport: str = Field(description="IATA code, e.g. 'SFO'.")
    to_airport: str
    departs: str = Field(description="Local departure, e.g. '2026-12-28 16:55'.")
    arrives: str = Field(description="Local arrival, e.g. '2026-12-28 20:47'.")
    duration_minutes: int | None = None
    aircraft: str | None = None


class FlightOption(BaseModel):
    """A flight from `search_flights`. Prices are totals for all travellers."""

    airline: str
    price: int | None = Field(default=None, description="Total for all travellers.")
    currency: str = "USD"
    travellers: int = 1
    legs: list[FlightLeg] = Field(min_length=1)

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    def summary(self) -> str:
        first, last = self.legs[0], self.legs[-1]
        stops = (
            "nonstop" if not self.stops else f"{self.stops} stop" + ("s" if self.stops > 1 else "")
        )
        via = f" via {', '.join(leg.to_airport for leg in self.legs[:-1])}" if self.stops else ""
        price = f" · ${self.price:,} total" if self.price is not None else ""
        return (
            f"{self.airline} · {stops}{via} · {first.from_airport} {first.departs} → "
            f"{last.to_airport} {last.arrives}{price}"
        )


class HotelOption(BaseModel):
    """A place to stay from `search_hotels`. Prices are per night, as Google Hotels shows them."""

    name: str
    price_per_night: int | None = None
    currency: str = "USD"
    rating: float | None = Field(default=None, description="Guest rating out of 5.")
    reviews: str | None = Field(default=None, description="Review count as shown, e.g. '7.2K'.")
    hotel_class: int | None = Field(default=None, description="Star class, e.g. 4.")
    amenities: list[str] = Field(default_factory=list)
    deal: str | None = Field(
        default=None, description="Google's tag, e.g. 'DEAL 18% less than usual'."
    )
    vacation_rental: bool = False

    def summary(self) -> str:
        parts = [self.name]
        if self.price_per_night is not None:
            parts.append(f"${self.price_per_night:,}/night")
        if self.hotel_class:
            parts.append(f"{self.hotel_class}-star")
        if self.rating is not None:
            parts.append(f"{self.rating}★" + (f" ({self.reviews} reviews)" if self.reviews else ""))
        return " · ".join(parts)


class Itinerary(BaseModel):
    """A complete day-by-day trip plan."""

    title: str = Field(description="e.g. '3 days on the Big Island'.")
    destination: str
    trip_length_days: int = Field(ge=1)
    travellers: str | None = Field(default=None, description="e.g. '2 adults, mid-range'.")
    start_date: date | None = Field(
        default=None, description="First day of the trip (YYYY-MM-DD), if the user gave dates."
    )
    trip_id: str | None = Field(
        default=None,
        description="Id of the trip this plan is for, from the Trips list in your instructions. "
        "Leave empty for a new trip.",
    )
    summary: str = Field(description="2-3 sentences on the shape of the trip.")
    assumptions: list[str] = Field(
        default_factory=list, description="Assumptions made where the user gave no details."
    )
    stays: list[Stay] = Field(default_factory=list)
    hotels: list[HotelOption] = Field(
        default_factory=list, description="Hotels the user chose, from search_hotels results."
    )
    flights: list[FlightOption] = Field(
        default_factory=list, description="Flights the user chose, from search_flights results."
    )
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
    if itinerary.start_date:
        end = itinerary.start_date + timedelta(days=itinerary.trip_length_days - 1)
        meta.append(f"{itinerary.start_date:%b %-d, %Y} – {end:%b %-d, %Y}")
    if itinerary.travellers:
        meta.append(itinerary.travellers)
    lines += [f"*{' · '.join(meta)}*", "", itinerary.summary, ""]
    if itinerary.assumptions:
        lines += ["**Assumptions**", ""] + [f"- {a}" for a in itinerary.assumptions] + [""]
    if itinerary.stays:
        lines += ["**Where to stay**", ""]
        lines += [f"- **{s.area}** ({s.nights} nights): {s.why}" for s in itinerary.stays]
        lines.append("")
    if itinerary.hotels:
        lines += ["**Hotels**", ""] + [f"- {h.summary()}" for h in itinerary.hotels] + [""]
    if itinerary.flights:
        lines += ["**Flights**", ""] + [f"- {f.summary()}" for f in itinerary.flights] + [""]
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
