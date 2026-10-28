---
name: itinerary-builder
description: Use when the user asks for a day-by-day itinerary, schedule or route for a trip, or wants an existing plan reworked, shortened, extended or reordered.
---

# Itinerary builder

## 1. Make sure you have a brief

You need at least the destination and number of days. If those are missing, load
`trip-intake` first. Otherwise state assumptions in one line and proceed.

## 2. Research before you write

For destinations covered by the guidebooks:

1. Search for the book's own suggested routes: `search_guidebooks("itineraries <N> days")`,
   then `read_guidebook_pages` on the hits. These are the strongest starting point.
2. Search each region or interest separately (e.g. "Kona snorkel", "Uffizi tickets",
   "night market Chiang Mai") rather than one vague search.
3. Read the pages around the best hits for opening days, travel times and practical tips.

For uncovered destinations, use general knowledge and say so.

## 3. Shape the days

- **Cluster geographically.** Group sights by neighbourhood or region; never zig-zag across a
  city or island in one day.
- **Pace:** 2–3 major activities per day at a moderate pace (1–2 relaxed, 3–4 packed).
- **Arrival and departure days are light:** assume half a day at most.
- **Travel days count.** A 4-hour transfer leaves room for one thing at the other end.
- **Sequence by time of day:** popular sights early or late; museums for midday heat or rain;
  sunsets, markets and night activities in the evening.
- **Respect constraints from the guidebook:** closing days, booking requirements, permits,
  tide or weather dependence.
- **Base changes:** prefer 2+ nights per base; flag one-night stays as tiring.
- Leave one flexible half-day in trips of 5+ days.

## 4. Format

```
## Day 1 · <area / theme>
- **Morning:** … (Lonely Planet <Destination>, p. N)
- **Afternoon:** …
- **Evening:** … dinner idea …
- **Sleep:** <base>
```

After the days, add a short **Notes** section: bookings to make ahead, things that might be
outdated (prices, hours, closures), and one or two swaps if the user's tastes differ.

Keep it skimmable. End by offering one or two concrete adjustments (slower pace, different
base, adding a day trip). If budget or getting around is a big part of the request, also load
`budget-planner` or `local-transport`.
