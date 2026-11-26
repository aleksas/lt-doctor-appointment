# lt-doctor-appointment

An agent skill (Claude Code / Claude.ai / any agent that reads `SKILL.md`) for finding open doctor appointments anywhere in Lithuania and choosing a doctor:

- **Searches IPR (ipr.esveikata.lt)**, the national booking system, for any municipality or the whole country. You can filter by profession, sub-service (age group, sub-specialty, care level), date window, **with or without a siuntimas** (referral), **state-funded or paid**, and **in person, by video or by phone**.
- **Lists actual free times** with visit type, clinic comment, room, registration desk, phone and the green-corridor flag.
- **Cross-checks every doctor** against **pincetas.lt** and **manodaktaras.lt**:
  - reviews weighted by recency and trustworthiness, with reviews that need a second look flagged;
  - detailed scores, licence, what the doctor treats, languages, prices, and free times on manodaktaras.lt.
- **Guides the agent** to ask about the problem first, to read review texts skeptically, and to check community forums (tevu-darzelis.lt, supermama.lt, Reddit) for personal experiences, read as context rather than ratings.

Site structure (API paths, every search filter and its options, endpoints, clinic addresses) is read from the live sites rather than hardcoded, and cached per site release. If discovery fails, the skill tells the agent how to fetch the options itself.

## Install

```bash
# Claude Code, personal skills
git clone https://github.com/aleksas/lt-doctor-appointment ~/.claude/skills/lt-doctor-appointment
```

Requires Python 3.9+ and no third-party packages.

## Use

Ask your agent, e.g. *"I need a dermatologist in Kaunas for a mole that changed, I have a siuntimas"* or *"earliest paid cardiologist anywhere near Klaipėda, video is fine"*. The script can also be run directly:

```bash
python3 scripts/ltdoc.py professions --q derma
python3 scripts/ltdoc.py services --profession 221210
python3 scripts/ltdoc.py municipalities --q kaun
python3 scripts/ltdoc.py filters
python3 scripts/ltdoc.py report --profession 221210 --municipality "Kauno m. sav." --service melanoma
python3 scripts/ltdoc.py report --profession 221210 --method REMOTE_APPOINTMENT_VIDEO   # whole country
python3 scripts/ltdoc.py --help
```

Tests (offline): `python3 -m unittest discover tests`

## Notes

- Not medical advice. The skill never books anything; the user books through esveikata.lt or the clinic.
- Reviews are opinions. The report's numbers say which reviews to read, not who is a good doctor. See [references/sources.md](references/sources.md).
- Waiting lists (*eilės*) need the patient's login and are not covered.
- The script sends few requests, waits between them, and identifies itself in its User-Agent. Reviewer IPs or identities are never stored or shown.
- **What goes where:**
  - The profession, place and filters go to IPR.
  - Doctors' names go to the review sites.
  - The patient's problem never leaves the agent.
  - Only a cache is written, in `~/.cache/lt-doctor-appointment`.

  See "What this skill sends and stores" in [SKILL.md](SKILL.md).

## License

MIT
