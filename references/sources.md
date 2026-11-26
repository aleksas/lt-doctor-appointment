# Review sources: what each is worth

Checked 2026-11. Re-check now and then; sites change.

| Source | Who writes | Who runs it | Per-doctor? | Used how |
|---|---|---|---|---|
| **pincetas.lt** | Anyone, anonymous or registered | Independent review site | Yes, plus licence, detailed scores, per-workplace ratings | Main review signal; scripted |
| **manodaktaras.lt** | Only patients who booked through it | Booking company paid by clinics | Yes, plus what the doctor treats, languages, education, prices, its own free times | Verified-visit volume and texts; scripted, but not used to separate doctors |
| tevu-darzelis.lt, supermama.lt forums | Parents | Parenting portals | In threads | Personal experiences via web search (context, not ratings) |
| reddit.com/r/lithuania and city subreddits | Residents, expats | Community | In threads | Personal experiences via web search (context, not ratings) |
| Google Maps | Anyone | Google | Clinic only | Only when the clinic is the question |
| Clinic testimonial pages (e.g. hila.lt/atsiliepimai) | Patients, as chosen by the clinic | The clinic | Yes | Ignore: the clinic selects them |
| whatclinic.com, bookimed.com | Few | Medical-tourism businesses | Clinic | Ignore: thin and commercial |

## pincetas.lt

- Each review has a date, recommend / not recommend / neutral, and registered or anonymous. Registered reviewers show how many reviews they've written. Anonymous reviews show the IP in the page; the script drops it.
- **Old negative texts are hidden.** Negative reviews older than about 3 years show "Neigiamas atsiliepimas parašytas prieš N m., todėl nebeaktualus ir nerodomas" ("this negative review was written N years ago, so it is no longer relevant and not shown") instead of the text. The vote still counts toward the site's percentage. Profile owners can also hide the text of reviews older than 5 years.
- The site's % is all-time, so a doctor who improved (or got worse) years ago carries the old record. That's why the script reports recency-weighted and last-24-months figures alongside it.
- **Detailed scores:** 1–5 averages on seven questions (waiting, diagnosis accuracy, treatment effect, answers, time given, follow-up, price) are shown as `detailed_ratings`. They're all-time, like the %.
- **Licence:** title, issue date and status (`licences`), which tells you roughly how long the doctor has practised. Also each workplace's own % and review count (`workplace_stats`).
- There is also a per-year cumulative "% recommend" chart (`cumulative_recommend_pct_by_year`). It's cumulative, so year-on-year changes are damped.
- Specialty labels can differ from IPR's (e.g. a dermatologist listed under "Chirurgija" (surgery)). The report flags this as `listed as: …`.

## manodaktaras.lt

- Only people who booked a visit through the platform can review ("Atsiliepimus gali palikti tik pas šį gydytoją apsilankę manodaktaras.lt vartotojai"), so the reviews are verified.
- The platform is paid by clinics, and almost every profile scores 100%. Example: Vilma Markevičienė has 33 of 33 positive here and 56% of 98 on pincetas.lt. Use it for the texts and the volume of recent verified visits, not as a rating.
- **Structured data** on the page gives prices per service and clinic, and its own free times (often paid slots that aren't in IPR). The "about" block lists what the doctor treats, licences, languages, education and training. The script keeps the site's own section labels rather than assuming them.
- It lists sub-specialty tags (Onkodermatologas, Apgamų šalinimo specialistas, Veido estetikos specialistas, …), which help narrow the choice of doctor.

## Forums and Reddit

- These are the most community-driven sources, but you can't query them well with a script: Reddit's JSON API returns 403 to anonymous clients, and tevu-darzelis.lt uses Google Custom Search in the browser. The agent should search the web for the doctor's name and read the threads.
- Expect few hits per doctor. Absence means nothing. Posts are personal experiences, not ratings, and are often years old, so check dates.
