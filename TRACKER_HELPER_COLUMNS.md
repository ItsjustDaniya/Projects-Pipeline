# Project_Clearance_Tracker: helper columns from the cron

## What feeds the tracker today

```
Metabase 6241 / 6242 / 13022 ──► project_clearance_refresh.py (this repo, 4x a day)
                                   └─► output sheet 1Vec4… tabs Tracker_Raw_SS / Tracker_Raw_SQL (A:S)
                                         └─► tracker "Raw - SS" / "Raw -SQL" A1 = IMPORTRANGE(… A1:S)
                                               T:AR  = per-row formulas, dragged down by hand
                                                  ├─ XLOOKUP Activity tab        (static paste, last done 3 Jun 2026)
                                                  ├─ XLOOKUP Placement tab       (IMPORTRANGE FlyWheel "Prog<>Placement")
                                                  ├─ XLOOKUP ALL Bible tab       (IMPORTRANGE FlyWheel "All Bible")
                                                  ├─ XLOOKUP 1st Submission tab  (IMPORTRANGE submissions_ranked, capped at row 5000)
                                                  └─ hand-typed Persona (SS col AB)
                                               ▼
              Monthly Target (Auto), Monthly Target Batchwise, BW_WOW_Ageing Trend (Auto),
              BW_# Sub Vs clearance, Repeat…, Max Capability, Copy of Max Capability, Both Cleared
```

Every summary tab counts rows of Raw - SS / Raw -SQL. Columns A:S are fresh, but T onward
depend on formulas someone has to drag down for each new batch, plus one stale paste:

| Problem found (3 Nov 2026) | Effect |
|---|---|
| Raw - SS helper formulas end at row 1976; data ends at 1978 | next batch's rows get no batch name / placement flag / ageing week, so they drop out of every summary tab |
| Oct 2025 batch: Clearance Time (AM) blank on all 37 cleared rows; May 2026: 24 blank | ageing tab under-counts clearance for those batches (May 2026, cleared by W12: 44 students counted vs 66 actual) |
| Activity tab last pasted 3 Jun 2026 | "Activity = Yes" filters in Max Capability use 5-month-old data |
| SS "Persona" (AB) is typed by hand next to an IMPORTRANGE | the import re-sorts, the typed column doesn't: a 60-row check matched the Acad Interview form on 14 rows |
| "1st Submission" QUERY `WHERE Col1=1` doesn't filter; tab lists attempts newest-first | "WOW Submission" uses each student's latest evaluated submission, not the first |
| Raw -SQL 1st-submission XLOOKUP points at the SS block's feedback column | never matches; always falls back to column J |
| Week (V) hard-coded to October 2026; Expected / Week Target (W, X) and Attendance / Assignment / MC (AE:AG) return #N/A on every row | none of these are read by any summary tab |

## What the cron does now

`tracker_helpers.py` computes the same columns, same positions, same headers, and the cron
appends them to Tracker_Raw_SS (now A:AR) and Tracker_Raw_SQL (now A:AP). Sources:

| Column(s) | Source |
|---|---|
| Z Activity, SS AB Persona | new Metabase question `sql/tracker_student_lookups.sql` (5 activity sources from card 11385 + Acad Interview form 4541) |
| AA / AC / AD placement status, enrolled status, phase | FlyWheel `Prog<>Placement` (read directly, cols A, T, F, E) |
| SQL AB Persona | FlyWheel `All Bible` (cols A, F), unchanged |
| SS AL / SQL AJ 1st submission | `submissions_ranked` (whole tab; rule set by `FIRST_SUBMISSION_RULE`) |
| everything else | the original formula, re-implemented (guarded variant where rows had drifted) |

Checked against the live sheet on 3 Nov 2026 (1,977 SS + 1,931 SQL rows): every column matches
the sheet wherever the sheet's own formula is intact; the differences are the rows listed above.
With `FIRST_SUBMISSION_RULE = "latest_evaluated"` the ageing-tab cells match except where the
sheet has blank/broken rows. Dates (AH, AI, AM / AF, AG, AK) are written as date serials so
`MODE(...)`/`ISNUMBER(...)` on the deadline column keep working.

If any lookup fails (card error, sheet not shared, empty result) the run writes every other tab,
**leaves Tracker_Raw_SS / Tracker_Raw_SQL as the last good run left them**, and exits non-zero
so GitHub emails you.

## Switch-on steps

1. **Metabase** — new native question on *Newton School*, paste `sql/tracker_student_lookups.sql`,
   save as "PC - Tracker Student Lookups" (runs ~1–3 min). Put its ID in `TRACKER_LOOKUPS_CARD_ID`
   and set `TRACKER_HELPER_COLS = True` in `project_clearance_refresh.py`.
2. **Sharing** — the cron's service account needs Viewer on the Placements FlyWheel sheet
   (`1Ue49…`) and the submissions_ranked sheet (`1CbJL4…`). If it lacks access, the run log
   prints the exact email to share with.
3. **Run once** — Actions → Project Clearance Refresh → Run workflow. Check Tracker_Raw_SS now
   ends at AR and Tracker_Raw_SQL at AP.
4. **Tracker (needs an editor of Project_Clearance_Tracker)**
   - `Raw - SS`: clear **T1:AR** (headers included), then set A1 to
     `=IMPORTRANGE("https://docs.google.com/spreadsheets/d/1Vec4-7mmLqtXMz9-rTZEvIxsV3nVgjm1KVjhOH_JcCc","Tracker_Raw_SS!A1:AR")`
   - `Raw -SQL`: clear **T1:AP**, then set A1 to `…"Tracker_Raw_SQL!A1:AP")`
   - Any cell left in those ranges blocks the import with `#REF!`.
5. **Optional, decide with the tracker owner** — set `FIRST_SUBMISSION_RULE = "first"` to make
   WOW Submission use the real first submission. On today's SS data, students counted as
   submitted by W1 go from 553 to 878; clearance numbers don't change.

The Activity, Placement, ALL Bible, 1st Submission and ' Dump - SP' tabs are no longer needed by
the Raw tabs after step 4 (Both Cleared still reads Placement).
