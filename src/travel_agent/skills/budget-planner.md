---
name: budget-planner
description: Use when the user asks how much a trip will cost, wants a budget or cost breakdown, asks if a trip fits a budget, or wants to save money on a trip.
---

# Budget planner

## Inputs

You need: destination(s), number of days, number of travellers and budget level (shoestring /
mid-range / splurge). Ask only for what's missing and matters; otherwise assume mid-range for
2 adults and say so.

## Gather price signals

- Guidebooks have budget sections and price bands: search "costs", "daily costs",
  "budget", "price ranges", "need to know" and read those pages. Restaurant and hotel listings
  also carry prices.
- **Guidebook prices are several years old.** Present them as a baseline and adjust upward,
  stating that you did (e.g. "book prices from ~2016, so expect 20–40% more today").
- For flights, call `search_flights` with the user's airports, dates and number of travellers
  and use the real fares (they're totals for all travellers). If dates or airports are unknown,
  ask, or give a range clearly labelled as an estimate.
- There's no live hotel price tool yet: use guidebook price bands and label them as estimates.
- Never present an estimate as a live or exact price.

## Break it down

Give a per-person table, then a trip total:

| Category | Per day | Days | Subtotal | Notes |
|---|---|---|---|---|
| Accommodation | | | | per person, based on shared room |
| Food & drink | | | | |
| Local transport | | | | passes, car hire, fuel, transfers |
| Activities & entry fees | | | | list the big-ticket ones |
| Flights / long-distance | — | — | | one-off |
| Buffer (10–15%) | | | | |

Use the destination's currency and the user's home currency if known (approximate rate,
labelled as such).

## Then advise

- Name the 2–3 biggest cost drivers and a concrete saving for each (cheaper base
  neighbourhood, rail pass vs single tickets, lunch menus instead of dinners, free days at
  museums from the guidebook).
- If the user gave a budget, say plainly whether the plan fits and what to cut or upgrade.
