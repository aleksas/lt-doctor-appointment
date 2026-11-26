# lt-doctor-appointment

An agent skill (Claude Code / Claude.ai / any agent that reads `SKILL.md`) for finding open doctor appointments in Lithuania and choosing a doctor:

- searches the national booking system **IPR (ipr.esveikata.lt)** by profession, municipality, sub-service (age group, sub-specialty, care level), **with or without a siuntimas** (referral) and **state-funded or paid**;
- cross-checks every doctor found against **pincetas.lt** and **manodaktaras.lt** reviews, weighting by recency and trustworthiness and flagging reviews that need a second look;
- tells the agent to ask about the problem first, to read review texts skeptically, and to check community forums (tevu-darzelis.lt, supermama.lt, Reddit) for anecdotes.

It reads site structure (API paths, filter codes, labels, endpoints) from the live sites rather than hardcoding it, and caches what it finds per site release.

## Install

```bash
# Claude Code, personal skills
git clone https://github.com/aleksas/lt-doctor-appointment ~/.claude/skills/lt-doctor-appointment
# or with the skills CLI
npx skills add aleksas/lt-doctor-appointment
```

Requires Python 3.9+ and no third-party packages.

## Use

Ask your agent, e.g. *"I need a dermatologist in Vilnius for a mole that changed, I have a siuntimas"*. The script can also be run directly:

```bash
python3 scripts/ltdoc.py professions --q derma
python3 scripts/ltdoc.py services --profession 221210
python3 scripts/ltdoc.py report --profession 221210 --municipality "Vilniaus m. sav." --service melanoma
python3 scripts/ltdoc.py reviews --name "Vardas Pavardė" --clinic "Clinic name" --texts
python3 scripts/ltdoc.py --help
```

Tests (offline): `python3 -m unittest discover tests`

## Notes

- Not medical advice. The skill never books anything; the user books through esveikata.lt or the clinic.
- Reviews are opinions. The report's numbers say which reviews to read, not who is a good doctor. See [references/sources.md](references/sources.md).
- Waiting lists (*eilės*) need the patient's login and are not covered.
- The script sends few requests, waits between them, and identifies itself in its User-Agent. Reviewer IPs or identities are never stored or shown.

## License

MIT
