---
name: lt-doctor-appointment
description: Find open doctor appointments anywhere in Lithuania through the national e-health booking system (IPR, ipr.esveikata.lt) for a health problem, with and without a referral (siuntimas), state-funded (Ligonių kasos) or paid, in person or remote, narrowed to the right sub-specialty and age group, and cross-check the doctors against patient reviews (pincetas.lt, manodaktaras.lt, community forums) weighted by recency and read with healthy skepticism. Use when someone in Lithuania wants to see a specialist (e.g. dermatologist, cardiologist, ENT) and asks where, how soon, with or without siuntimas, or which doctor to pick.
---

# Lithuanian doctor appointments + review cross-check

`scripts/ltdoc.py` (Python 3.9+, standard library only; paths are relative to this skill's directory) reads live data and works for any municipality, or the whole country:

- **IPR** (ipr.esveikata.lt): the national booking system's public search API. Its API paths and **every filter its search form offers** (keys, codes, labels) are read from IPR's own web-app bundle. Clinic cities and addresses come from its institution list. Free times come with visit type, comment, room, registration desk and phone. Discovered values are cached per IPR release in `~/.cache/lt-doctor-appointment`.
- **pincetas.lt**: open community reviews, 1–5 scores on seven questions, licence issue date and status, rating per workplace.
- **manodaktaras.lt**: a booking platform with verified-visit reviews. Also what each doctor treats, languages, education, prices and its own free times (often paid slots that aren't in IPR).

[references/sources.md](references/sources.md) explains how much weight each source deserves. [references/ipr.md](references/ipr.md) describes the API.

## 1. Ask about the problem before calling anything

Ask briefly, in the user's language, only what you don't already know:

1. **What is the problem?** In their own words. It decides the profession and sub-service.
2. **Who is the patient?** Adult or child. Services are often split into `(Suaugusiems)` (adults) and `(Vaikams)` (children).
3. **Do they have a siuntimas** (referral)? It's visible in the esveikata.lt patient portal under e. siuntimai. If they don't have one, would they get one from their family doctor?
4. **State-funded or paid?** State-funded (Ligonių kasos) visits are free with PSDF insurance but often months away. Paid (Pacientas) visits are often days away.
5. **Where?** Their municipality, or several nearby ones, or anywhere in Lithuania. City and district are separate municipalities (e.g. "Kauno m. sav." for the city and "Kauno r. sav." for the district). **Remote is OK?** Some consultations are by video or phone.
6. **How soon?** A date window, and any days or times that don't work.
7. Anything else that matters to them, such as the language the doctor speaks or a budget.

If the description suggests an emergency (heavy bleeding, sudden severe pain, breathing problems, signs of stroke), tell them to call 112 or go to the emergency department instead. If it sounds urgent but not an emergency, say a family doctor can issue an urgent referral. For suspected cancer, that can include the **green corridor** (*žaliasis koridorius*), which the free-times list marks per slot. Still run the search. Don't diagnose.

## 2. Map the problem to IPR's own options

Always take options from these commands. Don't assume them from memory or from examples:

```bash
python3 scripts/ltdoc.py professions --q <word>         # e.g. derma, kardio, ausu -> profession code
python3 scripts/ltdoc.py services --profession <code>   # sub-services: age group, sub-specialty, care level
python3 scripts/ltdoc.py municipalities --q <place>     # municipality ids and exact names
python3 scripts/ltdoc.py filters                        # every search-form filter and its options
```

- **Pick services by reading their names.** `II lygis` is specialist level and `III lygis` is university-hospital level. III usually expects a referral from a II-level specialist. If nothing fits clearly, leave `--service` out and say so.
- **Pick filter options from `filters`.** Use `--referral`, `--payment` and `--method` for referral, payment and visit method. Any other filter IPR adds later works as `--filter key=value`.

## 3. Search: with and without a siuntimas

```bash
python3 scripts/ltdoc.py report --profession <code> [--municipality "<name or id>" ...] \
  [--service <id or name part> ...] [--from YYYY-MM-DD --to YYYY-MM-DD] \
  --json-out /tmp/ltdoc.json > /tmp/ltdoc.md
```

- **Where:** leave out `--municipality` to search the whole country, and repeat it for several places. Each row shows the clinic's city.
- **Both referral cases:** with no `--referral`, the report starts with a **"With or without a siuntimas"** table giving the earliest state-funded and paid time in each case. Always present both. A *Be siuntimo* slot can be booked either way; a *Su siuntimu* slot needs a referral. Narrow with the options from `filters` when the user's situation is clear.
- **Doctor details:** after the tables comes a "Doctor details" section with:
  - IPR's actual free times for the first date (more with `--times-days N`, none with `--times-days 0`): visit type (first or follow-up), the clinic's comment, room, registration desk, phone and green-corridor flag;
  - licence, detailed scores and per-workplace ratings from pincetas.lt;
  - what the doctor treats, languages, education, prices and free times from manodaktaras.lt.
- **Run time:** it depends on how many doctors match; expect about 3–5 s per doctor. For a quick look use `--no-reviews --times-days 0`. A whole-country search can return hundreds of rows, so narrow it first.
- **Other commands:** `slots` (raw JSON), `times --specialist-id … --service-id … --clinic-id …` (more dates for one doctor) and `reviews --name … --clinic … --texts` (one doctor in full).
- **Confirm referral needs:** the referral flag is whatever each clinic configured, and a clinic can still ask for a siuntimas at a state-funded visit. Tell the user to confirm.

## 4. Read the reviews: take them with a grain of salt

Reviews are written by people, often right after a good or bad day. Use the numbers to decide what to read, not as a verdict.

- **Recency first.** *weighted %* counts each review half as much every 3 years (`--half-life`), and less again if anonymous, posted in a burst, near-duplicate, very short or shouting. *last 24m* gives the last 24 months. *eff. n* shows how much evidence there is: below ~5, say there's too little to judge. If old and recent reviews disagree, say which way the doctor has changed.
- **pincetas.lt hides the text of negative reviews older than ~3 years**, but still counts them in its %. See `negative_text_hidden_by_site`.
- **pincetas.lt's detailed scores** (waiting, diagnosis, treatment, answers, time given, follow-up, price) are all-time averages. Use them to see where a doctor is strong or weak, not as a ranking.
- **manodaktaras.lt is close to 100% for nearly everyone.** Its reviews come from real bookings, so read them as "sees many patients through this platform" plus useful text, not as a rating that separates doctors.
- **Read the texts** of recent and flagged reviews in the `--json-out` file (`reviews.<doctor>.sources[].reviews[]`). Discount reviews that sound off:
  - personal attacks or ALL CAPS with no account of what happened;
  - complaints about registration, queues, prices or administrators rather than the doctor;
  - unrealistic expectations;
  - generic praise with no detail, especially in bursts or from single-review accounts;
  - reviews that seem to be about a namesake (check `match`, `listed as`, `clinic not on profile`).

  Give more weight to specific, balanced accounts and to themes repeated across independent reviews.

## 5. Community sources, in parallel

Search the web for each shortlisted doctor and read the results as personal experiences: context, not ratings. A single story can't be counted, verified or scored, and finding nothing tells you nothing. Sources: `"<Vardas Pavardė>" site:tevu-darzelis.lt`, `site:supermama.lt` (parent forums), and Reddit (r/lithuania and the relevant city's subreddit, often in English). Ignore clinic testimonial pages, medical-tourism directories and "top 10 doctors" lists. Google Maps reviews describe a clinic, not a doctor.

## When the script can't discover something

The script prints `warning: … discovery failed … using last-known values` when a site has changed. A filter or option the user needs may also simply be missing. In either case, don't trust built-in values; fetch the live ones yourself:

1. Run `python3 scripts/ltdoc.py --refresh config` and check what was found.
2. **IPR options:** call the reference endpoints directly. Paths are in `config`, under `https://ipr.esveikata.lt` plus `apiUrl` plus the path:
   - professions: `/searchesNew/professions`
   - municipalities: `/searchesNew/municipalities`
   - sub-services: `/searchesNew/healthcare-filter-services?professionId=<id>`
   - institutions: `/searchesNew/institutions`
3. **IPR filters:** fetch `https://ipr.esveikata.lt/`, open the `main.<hash>.js` it references, and search for `key:"` near `options:[`. Each `{name:"…",value:Enum.MEMBER}` option's number is in that enum's definition (`n[n.MEMBER=<number>]`). Pass it with `--filter key=<number>`.
4. **Review sites:** open the doctor's page yourself (the URL is in the JSON) and read it.
5. Tell the user what was fetched by hand, and suggest opening an issue on the skill's repository so the parser can be fixed.

## What this skill sends and stores

- **To ipr.esveikata.lt:** the profession, municipality, filter choices and date window, plus clinic, service and doctor ids, through public, unauthenticated calls.
- **To pincetas.lt and manodaktaras.lt:** the names of doctors found in IPR, to look up their public pages.
- **To the web search tool you use for step 5:** doctors' names.
- **Never sent anywhere:** the patient's description of their problem, their name, personal code, referral or any login. The script has no login and asks for no credentials.
- **Written to disk:** only a discovery cache in `~/.cache/lt-doctor-appointment` (or `$XDG_CACHE_HOME`) and any file you name with `--json-out`. Deleting the cache is always safe.
- **Commands run:** only `python3 scripts/ltdoc.py …`. There are no installs and no other shell commands.

## 6. Answer

- Lead with the answer to their situation, for example: "with your siuntimas, the earliest state-funded slot near you is …; without one, …; by video, …".
- Give a short table: earliest time, doctor, clinic and city, funding, referral, visit method, and a one-line review read.
- Add what helps them choose: the green corridor if relevant, the clinic's comment on the slot, languages, price, and how long the doctor has been licensed.
- Give the doctors' names, clinics, registration phone and review-page links. Never show reviewer IPs or identities.
- Say when the data was fetched. Slots change by the minute.
- Book only if the user asks, and only through esveikata.lt or the clinic themselves. This skill never books.
- IPR waiting lists (*eilės*) need the patient's login and can't be checked here. If nothing fits, suggest joining one.
