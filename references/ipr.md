# IPR (ipr.esveikata.lt) notes

IPR (*Išankstinės pacientų registracijos sistema*) is Lithuania's national system for booking appointments ahead of time. Its patient portal is an Angular app backed by a JSON API. The public search parts need no login.

## Discovery instead of hardcoding

`ltdoc.py` reads `https://ipr.esveikata.lt/`, finds the `main.<hash>.js` bundle, and extracts:

- `apiUrl` and the `*Path` constants it needs: `municipalitiesSearchPath`, `professionsSearchPath`, `institutionsSearchPath`, `healthcareServicesSearchFilterPath`, `appointmentTimeGroupsSearchPath`, `appointmentsGroupTimesSearchPath`.
- The filter enums the search form uses, with their values and Lithuanian labels. It finds them by the `e.<param>===<Enum>.All` pattern and the `new X(<Enum>.<member>,"<label>")` label constructors:
  - `referralNeedType`: All=0 "Visi", REQUIRED=1 "Su siuntimu" (with referral), NOT_REQUIRED=2 "Be siuntimo" (without referral)
  - `paymentType`: All=0 "Visi", VLK=1 "Ligonių kasos" (state-funded), Patient=2 "Pacientas" (patient pays)
- A sanity check that the request parameter names still appear in the bundle. It prints a warning if they don't.

The result is cached per bundle hash in `~/.cache/lt-doctor-appointment/ipr-<bundle>.json`, so a new IPR release triggers rediscovery automatically. If parsing fails, the script warns and uses the last known values (`FALLBACK` in the script).

Generic words in clinic names (UAB, VšĮ, poliklinika, chain names with many branches, …) are learned from IPR's institution list: any word that appears in more than 1% of the ~2,400 names. Those words are ignored when matching a doctor's clinic to review-site workplaces. This list is cached for 7 days.

## Calls

| Purpose | Path (as discovered) | Params |
|---|---|---|
| Professions | `/searchesNew/professions` | — → `{code, name, id}` |
| Municipalities | `/searchesNew/municipalities` | — → `{admId, pavadinimas}` |
| Sub-services | `/searchesNew/healthcare-filter-services` | `professionId` |
| Institutions | `/searchesNew/institutions` | — |
| Slot groups | `/searches/appointments/times` | `municipalityId`, `professionCode`, `healthcareServiceId`, `referralNeedType`, `paymentType`, `page`, `size` |
| Doctors in a group | `/searches/appointments/times/details` | `organizationId`, `healthcareServiceId`, `fundType`, `referralNeedType`, `professionCode`, `appointmentMethodId`, `page`, `size` |

- Group results report funding and referral as `{type, name}`, where `type` is a letter (`N` = *ne*/no, `T` = *taip*/yes) and `name` is the label. The details call takes those letters back. The script passes them through unchanged and shows the labels.
- Paging: `meta.totalPages` is always 0, so the script keeps requesting pages until one comes back empty.
- Times are epoch milliseconds; the script converts them to Europe/Vilnius time.
- Care levels appear in service names: `II lygis` (specialist) and `III lygis` (university hospital).

## Not available without login

- Waiting-list (*eilės*) registration and queue lengths (`/searches/appointments/patient/queues/...`).
- The patient's own referrals (`Referrals.Active` / `Unused`).

## Etiquette

The script identifies itself in its User-Agent, waits ≥0.35 s between requests to the same host, and backs off on 429 or 5xx responses. A full report makes about 100 IPR requests and about 4 review-site requests per doctor.
