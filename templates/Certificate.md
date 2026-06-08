# Reproduction Certificate

*Live-Lastprognose-Challenge — organized by Prof. Dr. T. Bartz-Beielstein*

The reviewing team **[Team B — your team name]** certifies that the forecasting
software published by **[Team A — certified team name]** was **downloaded** and
**executed**, and that the results claimed by Team A on the challenge leaderboard
(<https://bartzbeielstein.github.io/challenge-leaderboard/>) for the date below
were **reproduced**.

## Subject of the certification

| Field | Value |
|-------|-------|
| Certified team (Team A) | `[Team A display name]` |
| Software ZIP (from the "Software" column in *About the Models*) | `[paste the software_link URL]` |
| Date of the claimed results (`xx.yy.zz`) | `[YYYY-MM-DD]` |

## Reproduced results

Confirm that the metric(s) published by Team A for that date were reproduced
within an acceptable tolerance. Fill in what you measured:

| Metric | Claimed by Team A | Reproduced by Team B |
|--------|-------------------|----------------------|
| Mean MAE [MW] | `[value]` | `[value]` |
| Mean RMSE [MW] | `[value]` | `[value]` |
| (other, optional) | `[value]` | `[value]` |

Notes on environment / deviations (optional):

> `[e.g. OS, Python version, any data substitutions, rounding differences]`

## Reviewing team (Team B)

| Field | Value |
|-------|-------|
| Team name | `[Team B display name]` |
| Reviewer GitHub handle(s) | `[@handle1, @handle2, ...]` |
| Date of review | `[YYYY-MM-DD]` |

Signature: ______________________________

---

### How to submit this certificate

1. Fill in **every** `[...]` placeholder above.
2. **Compile this Markdown file to a PDF** (e.g. via your editor, `pandoc`, or a
   Markdown-to-PDF tool).
3. **Email the PDF** to the challenge organizer, **Prof. Dr. Bartz-Beielstein**.

The organizer verifies the certificate and then sets `certified: "Yes"` for
Team A in `teams.yml`; the leaderboard's *About the Models* table then shows a
✅ in the **Certified** column for that team.
