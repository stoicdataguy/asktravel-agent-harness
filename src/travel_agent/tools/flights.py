"""Flight search over Google Flights via the `fast-flights` library (no API key).

Google's prices are totals for all passengers; round-trip searches list outbound flights at the
round-trip price, with returns chosen on Google Flights itself.
"""

import asyncio
import logging
import re
from datetime import date
from typing import Literal

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights
from langchain.tools import tool

from travel_agent.agent.schemas import FlightLeg, FlightOption

logger = logging.getLogger(__name__)

MAX_RESULTS = 10
_IATA = re.compile(r"^[A-Z]{3}$")


def _when(simple_datetime) -> str:
    year, month, day = simple_datetime.date
    hour, minute = (list(simple_datetime.time) + [0, 0])[:2]
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"


def to_option(result, travellers: int, currency: str) -> FlightOption:
    """Convert one fast-flights result into our model."""
    return FlightOption(
        airline=", ".join(result.airlines) or result.type,
        price=result.price or None,
        currency=currency,
        travellers=travellers,
        legs=[
            FlightLeg(
                from_airport=leg.from_airport.code,
                to_airport=leg.to_airport.code,
                departs=_when(leg.departure),
                arrives=_when(leg.arrival),
                duration_minutes=leg.duration,
                aircraft=leg.plane_type or None,
            )
            for leg in result.flights
        ],
    )


def _duration(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def format_option(n: int, option: FlightOption) -> str:
    first, last = option.legs[0], option.legs[-1]
    stops = (
        "nonstop"
        if not option.stops
        else f"{option.stops} stop{'s' if option.stops > 1 else ''} via "
        + ", ".join(leg.to_airport for leg in option.legs[:-1])
    )
    next_day = " (+1)" if last.arrives[:10] > first.departs[:10] else ""
    total_minutes = sum(leg.duration_minutes or 0 for leg in option.legs)
    route = f"{first.from_airport} {first.departs[11:]} → {last.to_airport} {last.arrives[11:]}"
    parts = [route + next_day, stops]
    if total_minutes and not option.stops:
        parts.append(_duration(total_minutes))
    aircraft = {leg.aircraft for leg in option.legs if leg.aircraft}
    if aircraft and not option.stops:
        parts.append(", ".join(sorted(aircraft)))
    if option.price is None:
        price = "price unavailable"
    elif option.travellers > 1:
        price = f"${option.price:,} total (~${round(option.price / option.travellers):,}/person)"
    else:
        price = f"${option.price:,}"
    return f"{n}. {price} · {option.airline} · " + " · ".join(parts)


@tool
async def search_flights(
    origin: str,
    destination: str,
    depart_date: date,
    return_date: date | None = None,
    adults: int = 1,
    children: int = 0,
    cabin: Literal["economy", "premium-economy", "business", "first"] = "economy",
    max_stops: int | None = None,
    max_results: int = 5,
) -> str:
    """Search live flights and prices on Google Flights.

    Use this whenever the user asks about flights, fares or flight times; never guess prices.

    Args:
        origin: Departure airport IATA code, e.g. "SFO". Use the main airport for a city
            (e.g. "LHR" for London) unless the user named one.
        destination: Arrival airport IATA code, e.g. "KOA" for Kona.
        depart_date: Departure date (YYYY-MM-DD).
        return_date: Return date for a round trip; omit for one-way.
        adults: Number of adults.
        children: Number of children.
        cabin: Cabin class.
        max_stops: 0 for nonstop only; omit for any.
        max_results: How many options to return (max 10).

    Returns options cheapest first. Prices are totals for all travellers. For round trips, only
    outbound flights are listed, priced for the round trip. Ends with a Google Flights link the
    user can open to book. To remember a flight the user picks, save it to their trip with
    save_trip_details(transport=...).
    """
    origin, destination = origin.strip().upper(), destination.strip().upper()
    if not (_IATA.match(origin) and _IATA.match(destination)):
        return "Use 3-letter IATA airport codes, e.g. SFO and KOA."
    if depart_date < date.today():
        return f"{depart_date} is in the past; ask the user for an upcoming date."
    if return_date and return_date < depart_date:
        return "The return date is before the departure date."
    travellers = adults + children
    if adults < 1 or travellers > 9:
        return "Searches need 1-9 travellers including at least one adult."

    legs = [
        FlightQuery(
            date=depart_date.isoformat(),
            from_airport=origin,
            to_airport=destination,
            max_stops=max_stops,
        )
    ]
    if return_date:
        legs.append(
            FlightQuery(
                date=return_date.isoformat(),
                from_airport=destination,
                to_airport=origin,
                max_stops=max_stops,
            )
        )
    query = create_query(
        flights=legs,
        trip="round-trip" if return_date else "one-way",
        seat=cabin,
        passengers=Passengers(adults=adults, children=children),
        currency="USD",
        language="en-US",
        max_stops=max_stops,
    )
    try:
        # fast-flights is synchronous; keep the event loop free
        results = await asyncio.to_thread(get_flights, query)
    except FlightsNotFound:
        return f"Google Flights found no flights {origin} → {destination} on {depart_date}."
    except Exception as exc:  # network errors, Google layout changes
        logger.warning("flight search failed: %s", exc)
        return f"Flight search failed ({type(exc).__name__}). Suggest checking {query.url()}"

    options = [to_option(r, travellers, "USD") for r in results if r.flights]
    if not options:
        return f"Google Flights found no flights {origin} → {destination} on {depart_date}."
    options.sort(key=lambda o: (o.price is None, o.price or 0))
    shown = options[: max(1, min(max_results, MAX_RESULTS))]
    trip = f"round trip, back {return_date:%a %b %-d}" if return_date else "one-way"
    who = f"{adults} adult{'s' if adults > 1 else ''}" + (
        f", {children} children" if children else ""
    )
    header = (
        f"Flights {origin} → {destination}, {depart_date:%a %b %-d, %Y}, {trip}, {who}, {cabin}. "
        "Prices in USD, totals for all travellers"
        + (" for the round trip (outbound shown)" if return_date else "")
        + ":"
    )
    lines = [header, *(format_option(i, o) for i, o in enumerate(shown, 1))]
    lines.append(f"See all options and book: {query.url()}")
    return "\n".join(lines)
