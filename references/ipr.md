# IPR (ipr.esveikata.lt) notes

IPR (*Išankstinės pacientų registracijos sistema*) is Lithuania's national system for booking appointments ahead of time. It covers every municipality. Its patient portal is an Angular app backed by a JSON API, and the public search parts need no login.

## Discovery instead of hardcoding

`ltdoc.py` reads `https://ipr.esveikata.lt/`, finds the `main.<hash>.js` bundle, and extracts:

- **API paths:** `apiUrl` and the path constants it needs: `professionsSearchPath`, `municipalitiesSearchPath`, `institutionsSearchPath`, `healthcareServicesSearchFilterPath`, `appointmentTimeGroupsSearchPath`, `appointmentsGroupTimesSearchPath`, `publicDates` and `publicAppointmentsPath`.
- **Every filter of the search form**, from its checkbox-group config `{label:"…",key:"<param>",options:[{name:"…",value:<Enum>.<MEMBER>}]}`. Each option's number comes from the compiled TypeScript enum. Two compiled shapes occur and both are handled. At the time of writing:
  - `paymentType`: Patient=2 "Pacientas", VLK=1 "Ligonių kasos"
  - `referralNeedType`: REQUIRED=1 "Su siuntimu", NOT_REQUIRED=2 "Be siuntimo"
  - `appointmentMethodId`: INSTITUTION=1 "Įstaigoje", REMOTE_APPOINTMENT_VIDEO=3 "Nuotolinis: vaizdo skambutis", REMOTE_APPOINTMENT_PHONE=4 "Nuotolinis: telefonas"

  New filters IPR adds appear in `filters` automatically and can be used with `--filter key=value`.
- **A sanity check** that the request parameter names still appear in the bundle. It prints a warning if they don't.

The result is cached per bundle hash, so a new IPR release triggers rediscovery. If parsing fails, the script warns and uses the last-known values in `FALLBACK`. Those can be wrong: `SKILL.md` ("When the script can't discover something") tells the agent how to fetch the live values itself.

**Institution list (cached 7 days):**
- **Clinic location:** each clinic's municipality, city and street address come from its `nameExt` field (`<name>, <city>, <address>`).
- **Generic clinic words:** any word that appears in more than 1% of the ~2,400 names (UAB, VšĮ, poliklinika, chain names, …) is ignored when matching a doctor's clinic to review-site workplaces.

## Calls

| Purpose | Path (as discovered) | Params |
|---|---|---|
| Professions | `/searchesNew/professions` | — → `{code, name, id}` |
| Municipalities | `/searchesNew/municipalities` | — → `{admId, pavadinimas}` |
| Sub-services | `/searchesNew/healthcare-filter-services` | `professionId` |
| Institutions | `/searchesNew/institutions` | — → `{istgId, istgPavadinimas, nameExt, municipalityId}` |
| Slot groups | `/searches/appointments/times` | `professionCode`, `municipalityId` (omit for the whole country), `healthcareServiceId`, filter keys, `leftBound`/`rightBound` (epoch ms), `page`, `size` |
| Doctors in a group | `/searches/appointments/times/details` | `organizationId`, `healthcareServiceId`, `fundType`, `referralNeedType`, `professionCode`, `appointmentMethodId`, bounds, paging |
| Dates with free times | `/appointments/dates/public` | `practitionerId`, `serviceId`, `organizationId`, `leftBound`, `rightBound` |
| Free times on a date | `/appointments/public` | the same, plus `date` (epoch ms of the day) |

- **Funding and referral codes:** group results report them as `{type, name}`, where `type` is a letter (`N` = *ne*/no, `T` = *taip*/yes) and `name` is the label. The details call takes the letters back; the script passes them through unchanged.
- **Paging:** `meta.totalPages` is always 0, and asking for the page after the last one can hang. The script stops at the first page shorter than the page size.
- **What each free time includes:** visit type (*pirminis apsilankymas* = first visit), the clinic's comment, funding, referral, the green-corridor flag (*žaliasis koridorius*, the fast track for suspected cancer), visit method, room, registration desk address and phone.
- **Times** are epoch milliseconds; the script converts them to Europe/Vilnius time.
- **Care levels** appear in service names: `II lygis` (specialist) and `III lygis` (university hospital).

## Not available without login

- Waiting-list (*eilės*) registration and queue lengths (`/searches/appointments/patient/queues/...`).
- The patient's own referrals.

## Etiquette

The script identifies itself in its User-Agent, waits ≥0.35 s between requests to the same host, backs off on 429 or 5xx responses, and uses 30 s timeouts.
