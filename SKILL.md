---
name: lt-doctor-appointment
description: Find open doctor appointments in Lithuania's public e-health booking system (IPR, ipr.esveikata.lt) for a health problem, with and without a referral (siuntimas), state-funded (Ligonių kasos) or paid, narrowed to the right sub-specialty and age group, and cross-check the doctors against patient reviews (pincetas.lt, manodaktaras.lt, community forums) weighted by recency and read with healthy skepticism. Use when someone in Lithuania wants to see a specialist (e.g. dermatologist, cardiologist, ENT) and asks where, how soon, with or without siuntimas, or which doctor to pick.
---

# Lithuanian doctor appointments + review cross-check

`scripts/ltdoc.py` (Python 3.9+, standard library only; paths are relative to this skill's directory) reads live data:

- **IPR** (ipr.esveikata.lt): the national booking system's public search API. API paths, filter codes and their labels are discovered from IPR's own web-app bundle on each new release and cached in `~/.cache/lt-doctor-appointment`, so the script follows the site rather than hardcoded values. Run `python3 scripts/ltdoc.py config` to see what was discovered; add `--refresh` to any command to re-read.
- **Review sites**: pincetas.lt (open community reviews) and manodaktaras.lt (booking platform, verified-visit reviews). See [references/sources.md](references/sources.md) for why each one is weighted the way it is.

## 1. Ask about the problem before calling anything

Do not search right away. Ask briefly, in the user's language, only what you don't already know:

1. **What is the problem?** In their own words (e.g. "a mole that changed", "child's eczema", "acne"). It decides the profession and sub-service.
2. **Who is the patient?** Adult or child. IPR splits most services by `(Suaugusiems)` for adults and `(Vaikams)` for children.
3. **Do they have a siuntimas** (referral)? If they're unsure, they can see it under e. siuntimai in the esveikata.lt patient portal. If they don't have one, would they get one from their family doctor?
4. **State-funded or paid?** State-funded (Ligonių kasos) visits are free with PSDF insurance but waits are often months. Paid (Pacientas) visits are usually days away.
5. **Where and how soon?** Municipality or municipalities (Vilnius city and Vilnius district are separate), how far they'd travel, and any deadline.

If the description suggests an emergency (heavy bleeding, sudden severe pain, breathing problems, signs of stroke), tell them to call 112 or go to the emergency department instead of booking. If it sounds urgent but not an emergency (e.g. a fast-changing mole), say a family doctor can issue an urgent referral, and still run the search. Don't diagnose.

## 2. Map the problem to IPR terms

```bash
python3 scripts/ltdoc.py professions --q derma        # -> 221210  Gydytojas dermatovenerologas
python3 scripts/ltdoc.py services --profession 221210 # sub-services with ids
python3 scripts/ltdoc.py municipalities --q viln      # -> 66 Vilniaus m. sav., 19 Vilniaus r. sav.
python3 scripts/ltdoc.py filters                      # referral and payment options as IPR names them
```

Pick the services by reading their names; don't guess:
- **Age group:** use `(Vaikams)` services for children and `(Suaugusiems)` for adults. General ones such as `(Bendroji dermatologija)` cover both.
- **Sub-specialty:** for example `(Melanoma, odos vėžys, apgamai, dariniai)` for moles and skin cancer.
- **Care level:** `II lygis` = specialist level, `III lygis` = university hospital level. III usually expects a referral from a II-level specialist, so start with II or a general service unless the user already has that referral.

If no sub-service clearly fits, leave `--service` out and say so. The report's *Profile tags* column (e.g. Onkodermatologas = skin-cancer specialist, Apgamų šalinimo specialistas = mole removal, Trichologas = hair and scalp) helps narrow further after the search.

## 3. Search: with and without a siuntimas

```bash
python3 scripts/ltdoc.py report --profession 221210 --municipality "Vilniaus m. sav." \
  --service 146 --service 147 --json-out /tmp/ltdoc.json > /tmp/ltdoc.md
```

- With no `--referral` / `--payment`, the report covers everything and starts with a **"With or without a siuntimas"** table: how many rows each case can book and the earliest state-funded and paid time. Always present both cases. A slot marked *Be siuntimo* (no referral) can be booked either way; a slot marked *Su siuntimu* needs a referral.
- Narrow with `--referral NOT_REQUIRED` (they have no referral and won't get one), `--referral REQUIRED`, `--payment VLK` (state-funded) or `--payment Patient` (paid). The values come from `filters`.
- About 30 doctors with reviews take around 2 minutes. Use `--no-reviews` for a quick availability check, and `slots` for raw JSON.
- The referral flag is whatever each clinic configured. A clinic can still ask for a siuntimas at a state-funded visit, so tell the user to confirm when booking.

## 4. Read the reviews: take them with a grain of salt

Reviews are written by people, often right after a good or bad day. Use the numbers to decide what to read, not as a verdict.

- **Recency first.** The report gives *weighted %*: each review counts half as much every 3 years (`--half-life`), and less again if anonymous, posted in a burst, near-duplicate, very short or shouting. It also gives *last 24m* (counts from the last 24 months) and *eff. n* (how much evidence there is). Below ~5 effective reviews, say there's too little to judge. A doctor with an old bad score and good recent reviews (or the reverse) has changed; say which way.
- **pincetas.lt hides the text of negative reviews older than ~3 years.** The vote still counts in the site's %. `negative_text_hidden_by_site` in the JSON shows how many.
- **manodaktaras.lt is close to 100% for nearly everyone** (same doctor: 56% on pincetas.lt, 33/33 positive there). Its reviews come from real bookings, so read them as "this doctor sees many patients through this platform" plus useful text, not as a rating that separates doctors.
- **Read the actual texts** of recent and flagged reviews in the `--json-out` file (`reviews.<doctor>.sources[].reviews[]`: `text`, `flags`, `weight`). Discount reviews that sound off:
  - personal attacks, insults or ALL CAPS with no account of what happened;
  - complaints about things the doctor doesn't control: registration, queues, prices, parking, the clinic's administrators;
  - unrealistic expectations, e.g. angry that nothing was prescribed, or that a chronic condition wasn't cured in one visit;
  - generic praise with no detail ("Super!", "Geriausia"), especially several in a few days or from single-review accounts;
  - reviews that seem to be about a different doctor or specialty (namesakes exist: check `match`, `listed as`, `clinic not on profile`).
- **Give more weight to specific, balanced accounts:** what was examined, how it was explained, whether there was follow-up. Weigh repeated themes across independent reviews over any single story.

## 5. Community sources, in parallel

Search the web for each shortlisted doctor and read what you find as anecdotes. Run these searches in parallel, not as a gate before the report:
- `"<Vardas Pavardė>" site:tevu-darzelis.lt` and `site:supermama.lt`: parent forums with doctor-recommendation threads, good for children's doctors.
- `"<Vardas Pavardė>"` on reddit.com/r/lithuania or r/vilnius: expat experiences, often in English.

Ignore clinic testimonial pages (the clinic picks which ones to show), medical-tourism directories, and SEO "top 10 doctors" lists. Google Maps reviews describe a clinic, not a doctor; mention them only if the clinic itself is the question.

## 6. Answer

- Lead with the answer to their situation, for example: "with your siuntimas, the earliest free state-funded slot is …; without one, …".
- Give a short table: earliest time, doctor, clinic, funding, referral flag, and a one-line review read for each doctor, e.g. "recent reviews positive (5 in 2 years), older ones mixed".
- Give the doctors' names, clinics and review-page links. Never show reviewer IPs or identities.
- Say when the data was fetched. Slots change by the minute.
- Book only if the user asks, and only through esveikata.lt or the clinic themselves. This skill never books.
- IPR waiting lists (*eilės*) need the patient's login, so they can't be checked here. If nothing fits, suggest joining one on ipr.esveikata.lt.

See [references/ipr.md](references/ipr.md) for how IPR's API and discovery work.
