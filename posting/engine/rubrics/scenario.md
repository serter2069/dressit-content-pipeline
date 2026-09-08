# Scenario judge rubric — faceless fashion Shorts script

Judge a 15–25 second YouTube Shorts script (JSON with hook, beats, cta,
music_mood) for a US fashion-finds audience.

Score 0–10. Start at 10 and subtract for every breach:

- Hook: must be <= 8 words and punchy enough to stop the scroll in 2s. (-2)
- Length: 3–5 beats; total spoken words (hook + beat texts) <= 55. (-2)
- Structure: every beat has text, visual, asset (product|stock|gen) and a
  usable query; at most 1 "gen" beat. (-2)
- Compliance: NO income claims ("make money", "invest", "get rich"),
  NO sexualization ("sexy", "seductive"), NO brand bashing ("scam",
  "rip-off", "sucks"). Any single compliance breach = automatic fail
  (score <= 3). (-7)
- CTA: present, uses the sponsor CTA verbatim, no invented links. (-1)
- Tone: energetic, concrete product names/prices, no generic filler. (-1)

pass = true only if score >= 7 and there are zero compliance breaches.
List every concrete violation in "violations" (empty list if none).
