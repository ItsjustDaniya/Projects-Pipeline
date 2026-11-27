"""
Helper columns for Project_Clearance_Tracker "Raw - SS" (T:AR) and "Raw -SQL" (T:AP).

Until now these were per-row formulas in the tracker, dragged down by hand next to the
IMPORTRANGE of Tracker_Raw_SS / Tracker_Raw_SQL (A:S). Every new batch added rows below the
last dragged formula, so the helper cells went blank and the summary tabs quietly under-counted
(this is what broke F38 on the ageing tab in Oct 2026).

This module rebuilds the same columns in Python, at the same positions and with the same
headers, so the tracker can import A:AR / A:AP in one IMPORTRANGE and drop the dragged formulas.

Lookups the formulas used, and where they come from now:
  Activity tab (Z)            -> Metabase card "PC - Tracker Student Lookups" (sql/tracker_student_lookups.sql)
  Persona, Raw - SS (AB)      -> same card, Acad Interview persona (was a hand-typed column)
  Persona, Raw -SQL (AB)      -> Placements FlyWheel sheet, "All Bible" (cols A, F), as before
  Placement tab (AA/AC/AD)    -> Placements FlyWheel sheet, "Prog<>Placement" (cols A, T, F, E)
  1st Submission tab (SS AL / SQL AJ) -> submissions_ranked sheet (Projects---Daily-Update cron)

Columns whose formulas were already broken in the tracker are kept as empty columns so every
other column keeps its position (W, X, AE, AF, AG, AP; AE on SQL). See BROKEN_COLUMNS.

Dates are written as Google Sheets serial numbers (days since 1899-12-30), so they import as
real dates: MODE()/ISNUMBER() on the deadline column keep working, and the tracker's existing
column formats display them.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime

import pandas as pd

SHEETS_EPOCH = datetime(1899, 12, 30)

SS_HELPER_HEADERS = [
    "Batch", "Days", "Week", "Expected ", "Week Target", "Final feedback month", "Activity",
    "", "Persona", "", "", "Attendance", "Assignment", "MC", "project_deadline_date - 1",
    "Submission Time - 1", "WOW Submission", "Sumission Month", "1st Submisison", "Clearance Time",
    "WOW Clearance", "Clearance Month", "", "Submission Month", "Clearance month",
]  # T..AR (25 columns)

SQL_HELPER_HEADERS = [
    "Batch", "Days", "Week", "Expected ", "Week Target", "Final feedback month", "Activity",
    "", "Persona", "", "", "C%", "project_deadline_date - 1", "Submission Time - 1",
    "WOW Submission", "Sumission Month", "1st Submisison", "Clearance Time", "WOW Clearance",
    "Clearance Month", "Clearance week", "Submission Month", "Clearance month",
]  # T..AP (23 columns)

# Formulas that were already returning #N/A / #REF! for every row in the tracker. Left blank.
BROKEN_COLUMNS = {
    "Expected / Week Target (W, X)": "look up a target table that no longer exists (#N/A / #REF!)",
    "Attendance / Assignment / MC (SS: AE, AF, AG)": "XLOOKUP into ' Dump - SP' on the wrong key "
                                                   "columns, #N/A on every row; nothing reads them",
    "C% (SQL: AE)": "empty in the tracker",
}


# ----------------------------------------------------------------------------- parsing
def uid_key(v) -> str:
    """'2,091,517' / 2091517.0 / '2091517' -> '2091517'. Blank for anything else."""
    s = str(v).replace(",", "").strip()
    if not s or s.lower() in ("nan", "none", "null", "<na>"):
        return ""
    try:
        return str(int(float(s)))
    except ValueError:
        return s


def _date10(v) -> date | None:
    """DATEVALUE(LEFT(v,10)) for 'YYYY-MM-DD...' text; None for 'null' / blank / junk."""
    s = str(v).strip()
    if len(s) < 10:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _datetime19(v) -> datetime | None:
    """DATEVALUE(LEFT(v,10)) + TIMEVALUE(MID(v,12,8)) for 'YYYY-MM-DD HH:MM:SS...'."""
    s = str(v).strip()
    try:
        return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        d = _date10(s)
        return datetime(d.year, d.month, d.day) if d else None


def serial(v: date | datetime | None):
    if v is None:
        return ""
    if not isinstance(v, datetime):
        v = datetime(v.year, v.month, v.day)
    x = (v - SHEETS_EPOCH).total_seconds() / 86400
    return int(x) if x == int(x) else round(x, 6)


def month_text(v: date | datetime | None) -> str:
    return v.strftime("%B %Y") if v else ""


def _roundup(x: float) -> int:
    """Sheets ROUNDUP(x, 0): away from zero."""
    return math.ceil(x) if x >= 0 else -math.ceil(-x)


_MONTH_BATCH = re.compile(r"((?:January|February|March|April|May|June|July|August|September|"
                          r"October|November|December)(?: [AB])? \d{4})\s*$")


def short_batch(full: str) -> str:
    """=TRIM(MID(A, FIND("-", A) + 1, LEN(A)))  ('DS Spreadsheets - July A 2026' -> 'July A 2026').
    Names without a '-' ('DS SQL November 2025') errored in the sheet and were typed over by
    hand with the trailing month; do the same here."""
    s = str(full)
    i = s.find("-")
    if i >= 0:
        return s[i + 1:].strip()
    m = _MONTH_BATCH.search(s)
    return m.group(1) if m else s.strip()


# ----------------------------------------------------------------------------- per-row rules
def days_to_final_feedback(re_eval, first_fb, last_fb):
    """U  =IF(S=0, IF(OR(M="",L=""),"", INT(DATEVALUE(LEFT(M,10))-DATEVALUE(LEFT(L,10)))), 0)"""
    if str(re_eval).strip() not in ("0", "0.0", ""):
        return 0
    l, m = _date10(first_fb), _date10(last_fb)
    return (m - l).days if (l and m) else ""


def week_of_current_month(last_fb, today: date):
    """V  'W1'..'W5' when the final feedback falls in the current month (the tracker had the
    month hard-coded, last set to October 2026)."""
    m = _date10(last_fb)
    if not m or (m.year, m.month) != (today.year, today.month):
        return ""
    return f"W{min(5, _roundup(m.day / 7))}"


def wow_submission(first_sub: date | None, deadline: date | None):
    """SS AJ / SQL AH  =IF(AI<=AH,0,IF(ROUNDUP((AI-AH)/7,0)>12,"",ROUNDUP((AI-AH)/7,0)))"""
    if not first_sub or not deadline:
        return ""
    d = (first_sub - deadline).days
    if d <= 0:
        return 0
    w = _roundup(d / 7)
    return w if w <= 12 else ""


def wow_clearance(clear_at: datetime | None, deadline: date | None):
    """SS AN / SQL AL  =IF(AM<AH,0,IF(INT((AM-AH)/7)+1>12,"",INT((AM-AH)/7)+1))"""
    if not clear_at or not deadline:
        return ""
    d = (clear_at - datetime(deadline.year, deadline.month, deadline.day)).total_seconds() / 86400
    if d < 0:
        return 0
    w = int(d // 7) + 1
    return w if w <= 12 else ""


def submission_month_bucket(first_sub: date | None, deadline: date | None):
    """SS AQ / SQL AO  =IF(AI<AH-14,"",IF(AI<=AH,0,ROUNDUP((AI-AH)/29,0)))"""
    if not first_sub or not deadline:
        return ""
    d = (first_sub - deadline).days
    if d < -14:
        return ""
    return 0 if d <= 0 else _roundup(d / 29)


def clearance_month_bucket_ss(cleared: bool, clear_at: datetime | None, deadline: date | None):
    """SS AR  =IF(R="Cleared",IF(AM<=AH,0,IF(ROUNDUP((AM-AH)/30,0)>12,"",ROUNDUP((AM-AH)/30,0))),"")"""
    if not cleared or not clear_at or not deadline:
        return ""
    d = (clear_at.date() - deadline).days
    if d <= 0:
        return 0
    m = _roundup(d / 30)
    return m if m <= 12 else ""


def clearance_month_bucket_sql(cleared: bool, clear_at: datetime | None, deadline: date | None):
    """SQL AP  =IF(R="Cleared",IF(AK<AF-14,"",IF(AK<=AF,0,IF(ROUNDUP((AK-AF)/29,0)>12,"",ROUNDUP(...)))),"")"""
    if not cleared or not clear_at or not deadline:
        return ""
    d = (clear_at.date() - deadline).days
    if d < -14:
        return ""
    if d <= 0:
        return 0
    m = _roundup(d / 29)
    return m if m <= 12 else ""


def clearance_week_sql(marks, last_fb):
    """SQL AN  =IF(P>=8, MIN(5, INT((DAY(DATEVALUE(LEFT(M,10)))-1)/6)+1), "")"""
    try:
        p = float(marks)
    except (TypeError, ValueError):
        return ""
    m = _date10(last_fb)
    if p < 8 or not m:
        return ""
    return min(5, (m.day - 1) // 6 + 1)


# ----------------------------------------------------------------------------- lookups
class Lookups:
    """All per-student lookups, keyed by uid_key(user_id). Missing keys -> blank cells."""

    def __init__(self, activity30=None, placement=None, persona_ss=None, persona_sql=None,
                 first_sub_ss=None, first_sub_sql=None):
        self.activity30 = activity30 or {}        # uid -> 'Yes' / 'No'
        self.placement = placement or {}          # uid -> (status, enrolled_status, phase)
        self.persona_ss = persona_ss or {}        # uid -> Acad Interview persona (Raw - SS AB)
        self.persona_sql = persona_sql or {}      # uid -> 'All Bible' persona (Raw -SQL AB)
        self.first_sub_ss = first_sub_ss or {}    # uid -> first submission time (text)
        self.first_sub_sql = first_sub_sql or {}


def student_lookups_from_card(df: pd.DataFrame) -> tuple[dict, dict]:
    """'PC - Tracker Student Lookups' card -> ({uid: 'Yes'/'No' 30-day activity},
    {uid: Acad Interview persona})."""
    need = {"user_id", "activity_last_30days", "acad_interview_persona"}
    if df is None or df.empty or not need <= set(df.columns):
        raise KeyError(f"student lookups card must return {sorted(need)}; got "
                       f"{[] if df is None else list(df.columns)}")
    activity, persona = {}, {}
    for u, a, p in zip(df["user_id"], df["activity_last_30days"], df["acad_interview_persona"]):
        k = uid_key(u)
        if not k or k in activity:
            continue
        activity[k] = str(a).strip()
        if pd.notna(p) and str(p).strip():
            persona[k] = str(p).strip()
    return activity, persona


def placement_lookup(rows: list[list[str]]) -> dict:
    """'Prog<>Placement' values (header row is the one whose col A is 'UserID').
    XLOOKUP semantics: first row for a user wins. Returns uid -> (T Status, F Enrolled, E Phase)."""
    start = next((i + 1 for i, r in enumerate(rows[:10]) if r and str(r[0]).strip().lower() == "userid"), 1)
    out = {}
    for r in rows[start:]:
        r = list(r) + [""] * (20 - len(r))
        k = uid_key(r[0])
        if k and k not in out:
            out[k] = (str(r[19]).strip(), str(r[5]).strip(), str(r[4]).strip())
    return out


def persona_lookup(rows: list[list[str]]) -> dict:
    """'All Bible' values: col A user id, col F Persona. First row for a user wins."""
    out = {}
    for r in rows[1:]:
        r = list(r) + [""] * (6 - len(r))
        k = uid_key(r[0])
        if k and k not in out:
            out[k] = str(r[5]).strip()
    return out


FIRST_SUBMISSION_RULES = ("first", "latest_evaluated")


def first_submission_lookup(rows: list[list[str]], module: str, rule: str = "first") -> dict:
    """submissions_ranked values -> {uid: Submission Time} for one module. Reads the whole tab
    (the tracker's IMPORTRANGE stopped at row 5000).

    rule="first"            submission_rank = 1, i.e. the student's first evaluated submission.
                            This is what the tracker's '1st Submission' QUERY (WHERE Col1=1) was
                            written to do.
    rule="latest_evaluated" first row for the student in tab order, ignoring rank. The tracker's
                            QUERY was not actually filtering to rank 1 and the tab lists ranks
                            newest-first, so this reproduces the numbers the tracker shows today.
    """
    if rule not in FIRST_SUBMISSION_RULES:
        raise ValueError(f"rule must be one of {FIRST_SUBMISSION_RULES}, got {rule!r}")
    if not rows:
        return {}
    h = [str(c).strip().lower() for c in rows[0]]
    ix = {name: h.index(name) for name in ("submission_rank", "user_id", "module_name", "submission time")}
    out = {}
    for r in rows[1:]:
        r = list(r) + [""] * (len(h) - len(r))
        if rule == "first" and str(r[ix["submission_rank"]]).strip() not in ("1", "1.0"):
            continue
        if str(r[ix["module_name"]]).strip() != module:
            continue
        k = uid_key(r[ix["user_id"]])
        if k and k not in out:
            out[k] = str(r[ix["submission time"]]).strip()
    return out


# ----------------------------------------------------------------------------- builders
def _common(row, lk: Lookups, today: date, persona: dict):
    uid = uid_key(row["user_id"])
    status, enrolled, phase = lk.placement.get(uid, ("", "", ""))
    first_fb, last_fb = row["first_feedback_given_time"], row["latest_feedback_given_time"]
    no_eval = _date10(first_fb) is None
    cleared = str(row["Clearance"]).strip() == "Cleared"
    last_fb_d = _date10(last_fb)
    return uid, [
        short_batch(row["Batch"]),                                            # T  Batch
        days_to_final_feedback(row["re_evaluation_flag"], first_fb, last_fb), # U  Days
        week_of_current_month(last_fb, today),                                # V  Week
        "",                                                                   # W  Expected (broken)
        "Not submitted/Not Evaluated" if no_eval else "",                     # X  Week Target
        month_text(last_fb_d) if cleared else "",                             # Y  Final feedback month
        lk.activity30.get(uid, ""),                                           # Z  Activity
        status,                                                               # AA Placement status
        persona.get(uid, ""),                                                 # AB Persona
        enrolled,                                                             # AC Enrolled status
        phase,                                                                # AD Phase
    ], cleared


def build_ss_helpers(raw: pd.DataFrame, lk: Lookups, today: date) -> pd.DataFrame:
    """Raw - SS T:AR for each row of Tracker_Raw_SS (same order)."""
    out = []
    for _, row in raw.iterrows():
        uid, cols, cleared = _common(row, lk, today, lk.persona_ss)
        deadline = _date10(row["project_deadline_date"])
        first_sub_txt = lk.first_sub_ss.get(uid) or row["Submission Time"]
        first_sub = _date10(first_sub_txt)
        clear_at = _datetime19(row["latest_feedback_given_time"])
        cols += [
            "", "", "",                                                       # AE AF AG (broken)
            serial(deadline) if deadline else row["project_deadline_date"],   # AH deadline
            serial(first_sub),                                                # AI 1st submission date
            wow_submission(first_sub, deadline),                              # AJ WOW Submission
            month_text(first_sub),                                            # AK Submission month
            first_sub_txt,                                                    # AL 1st Submission
            serial(clear_at),                                                 # AM Clearance Time
            wow_clearance(clear_at, deadline),                                # AN WOW Clearance
            month_text(clear_at),                                             # AO Clearance Month
            "",                                                               # AP
            submission_month_bucket(first_sub, deadline),                     # AQ Submission Month
            clearance_month_bucket_ss(cleared, clear_at, deadline),           # AR Clearance month
        ]
        out.append(cols)
    return pd.DataFrame(out, columns=[f"_h{i}" for i in range(len(SS_HELPER_HEADERS))], index=raw.index)


def build_sql_helpers(raw: pd.DataFrame, lk: Lookups, today: date) -> pd.DataFrame:
    """Raw -SQL T:AP for each row of Tracker_Raw_SQL (same order)."""
    out = []
    for _, row in raw.iterrows():
        uid, cols, cleared = _common(row, lk, today, lk.persona_sql)
        deadline = _date10(row["project_deadline_date"])
        first_sub_txt = lk.first_sub_sql.get(uid) or row["Submission Time"]
        first_sub = _date10(first_sub_txt)
        clear_at = _datetime19(row["latest_feedback_given_time"])
        cols += [
            "",                                                               # AE C% (empty)
            serial(deadline) if deadline else row["project_deadline_date"],   # AF deadline
            serial(first_sub),                                                # AG 1st submission date
            wow_submission(first_sub, deadline),                              # AH WOW Submission
            month_text(first_sub),                                            # AI Submission month
            first_sub_txt,                                                    # AJ 1st Submission
            serial(clear_at),                                                 # AK Clearance Time
            wow_clearance(clear_at, deadline),                                # AL WOW Clearance
            month_text(clear_at),                                             # AM Clearance Month
            clearance_week_sql(row["marks_obtained"], row["latest_feedback_given_time"]),  # AN
            submission_month_bucket(first_sub, deadline),                     # AO Submission Month
            clearance_month_bucket_sql(cleared, clear_at, deadline),          # AP Clearance month
        ]
        out.append(cols)
    return pd.DataFrame(out, columns=[f"_h{i}" for i in range(len(SQL_HELPER_HEADERS))], index=raw.index)
