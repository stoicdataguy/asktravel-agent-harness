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

## 4. Deliver it with `present_itinerary`

Don't write the day-by-day plan as text. Call `present_itinerary` with the full plan; it is
validated and shown to the user as a formatted itinerary.

- One `DayPlan` per day, numbered from 1, with `trip_length_days` equal to the number of days.
- Each activity: `time_of_day` (morning / afternoon / evening / full day), a short title,
  1–2 sentences of details, the location, and a `source` citation like
  "Lonely Planet Hawaii, p. 90" when it came from the guidebook.
- Put meals in as activities where they matter (a market lunch, a recommended dinner).
- `stays`: each base with its number of nights and why it suits the plan.
- `assumptions`: anything you assumed (pace, budget, travellers).
- `bookings_to_make`: tours, permits, trains or restaurants to reserve ahead.
- `caveats`: possibly outdated prices, hours or closures, and conditions to check.

If the tool returns a validation error, fix the plan and call it again.

After the tool call, reply with at most one or two sentences, e.g. offering a concrete
adjustment (slower pace, a different base, a day trip). Don't repeat the plan. To change an
existing itinerary, call `present_itinerary` again with the complete updated plan. If budget or
getting around is a big part of the request, also load `budget-planner` or `local-transport`.
