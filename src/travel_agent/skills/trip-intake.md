---
name: trip-intake
description: Use when the user wants help planning a trip but hasn't said where to go or for how long, or their request is too vague to plan from. Not needed when the destination and trip length are already given; just state any assumptions.
---

# Trip intake

Gather just enough to plan well, without interrogating the user.

## The essentials

| Detail | Why it matters | Sensible default if the user doesn't care |
|---|---|---|
| Destination(s) | Everything | (must ask) |
| Trip length or dates | Pacing, season, weather, prices | (must ask for length; dates optional) |
| Starting point / home airport | Flights, arrival day | Ask only if flights or arrival logistics matter |
| Travellers | Room types, pace, kid/accessibility needs | 2 adults |
| Budget level | Hotels, food, transport choices | Mid-range |
| Interests | What to prioritise | Mix of top sights, food and some nature |
| Pace | Sights per day | Moderate: 2–3 major things per day |
| Constraints | Mobility, diet, must-sees, things to avoid | None |

## How to ask

1. Read the conversation first. Never re-ask something the user already said.
2. If the destination and length are known, don't block on the rest. State your assumptions
   and plan; the user can correct you.
3. Otherwise ask **at most 4 questions in one message**, as a short numbered list, most
   important first. Offer examples so answering is quick ("budget: shoestring / mid-range /
   splurge?").
4. If the user is undecided between destinations, ask about interests and season instead,
   then suggest 2–3 options (check `list_guidebooks` for covered places).

## Once you have enough

Write a compact **Trip brief** before planning, so the user can catch misunderstandings:

> **Trip brief:** 5 days in Rome & Florence, mid-May · 2 adults from London · mid-range ·
> food, Renaissance art, some walking · moderate pace · no car

Then continue with the actual request, loading `itinerary-builder` or other skills if they
apply.
