#!/usr/bin/env python3
"""
Project Clearance pipeline: Metabase -> Google Sheet (-> Looker Studio).

Runs from GitHub Actions 4x/day. Each run:
  1. Exports Metabase cards via API:
       #6241 DS-Project-1    (older course structures, incl. non-submitters)
       #6959 Project-1-1     (newer course structures, submitters from 2025)
       #6579 submission vs evaluations - 2 (random-question projects: ALL 2026 batches)
       #7577 Placement_grooming_sessions ("groomer")
  2. Reads the student master data from a Google Sheet.
  3. Builds tabs: Raw_SS, Raw_SQL, Both_Cleared, Batch_Summary, Groomer, Master, _Refresh_Log
  4. Overwrites those tabs in the output Google Sheet (Looker Studio reads from it).

Secrets (only these two come from GitHub secrets):
  METABASE_API_KEY      Metabase API key (Admin > Settings > Authentication > API keys)
  GCP_SA_JSON           full JSON of the Google service account key
Everything else is in the CONFIG block below.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import gspread
import pandas as pd
import requests
from google.oauth2.service_account import Credentials

# ============================== CONFIG ======================================
METABASE_URL = "https://YOUR-METABASE-HOST"      # <-- EDIT: your Metabase base URL (no trailing /)
PASS_MARK = None                                  # <-- EDIT: marks out of 10 needed to clear, e.g. 6

OUTPUT_SHEET_ID = "1Vec4-7mmLqtXMz9-rTZEvIxsV3nVgjm1KVjhOH_JcCc"   # Looker Studio reads this
MASTER_SHEET_ID = "1a6pdd4M3gKTUdRpb9HHzMAVnkrPwr-YPNwoaPT01Ghw"   # Master Data
MASTER_TAB = "Master Data 2023-2026"             # falls back to the first tab if not found

# Master columns: set exact header names, or leave None to auto-detect from the candidates
MASTER_KEY_COL = None          # student id column
MASTER_BATCH_COL = None        # batch column
MASTER_ACCOUNTABLE_COL = None  # TRUE/Yes/1 = Accountable student
MASTER_KEY_CANDIDATES = ["user_id", "userid", "student_id", "uid", "id"]
MASTER_BATCH_CANDIDATES = ["batch", "batch_name", "au_batch", "course", "course_title"]
MASTER_ACCOUNTABLE_CANDIDATES = ["accountable", "is_accountable", "accountable_flag"]

SS_MODULE_REGEX = r"spreadsheet|excel"   # DS 02 Spreadsheets
SQL_MODULE_REGEX = r"sql"                # DS 04 SQL
# ===========================================================================

IST = timezone(timedelta(hours=5, minutes=30))
CARDS = {"project_v1": 6241, "project_v2": 6959, "project_random": 6579, "groomer": 7577}
WEEK_CHECKPOINTS = range(0, 9)    # W0..W8 (W0 = at 1st-submission deadline)
MONTH_CHECKPOINTS = range(1, 7)   # M1..M6 after deadline
WRITE_CHUNK_ROWS = 5000
SHEET_CELL_LIMIT = 10_000_000


def log(msg: str) -> None:
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


def norm_col(c: str) -> str:
    """'User ID' -> 'user_id', 'Submission Time 1' -> 'submission_time_1'."""
    return re.sub(r"[^0-9a-z]+", "_", str(c).strip().lower()).strip("_")


# --------------------------------------------------------------------------- Metabase
def fetch_card(base_url: str, api_key: str, card_id: int, retries: int = 3) -> pd.DataFrame:
    url = f"{base_url}/api/card/{card_id}/query/csv"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            log(f"Metabase card #{card_id}: export attempt {attempt}")
            r = requests.post(
                url,
                headers={"x-api-key": api_key},
                data={"parameters": "[]", "format_rows": "false"},
                timeout=1200,
            )
            r.raise_for_status()
            body = r.content.decode("utf-8-sig")
            # Metabase sometimes returns 2xx with a JSON error body when the query fails
            if body.lstrip().startswith("{") and '"error"' in body[:2000]:
                raise RuntimeError(f"Query error: {body[:500]}")
            df = pd.read_csv(io.StringIO(body), low_memory=False)
            df.columns = [norm_col(c) for c in df.columns]
            log(f"Metabase card #{card_id}: {len(df):,} rows x {len(df.columns)} cols")
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  failed: {e}")
            time.sleep(30 * attempt)
    raise RuntimeError(f"Card #{card_id} failed after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------- Google Sheets
def gsheet_client(sa_json: str) -> gspread.Client:
    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly"],
    )
    return gspread.authorize(creds)


def read_master(gc: gspread.Client, sheet_id: str, tab: str) -> pd.DataFrame:
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.get_worksheet(0)
        log(f"Master tab '{tab}' not found; using first tab '{ws.title}'")
    rows = ws.get_all_values()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df = df.loc[:, [c for c in df.columns if str(c).strip()]]  # drop blank header cols
    df.columns = [norm_col(c) for c in df.columns]
    log(f"Master: {len(df):,} rows x {len(df.columns)} cols")
    return df


def to_values(df: pd.DataFrame) -> list[list]:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M:%S")
    out = out.astype(object).where(pd.notna(out), "")
    return [list(out.columns)] + out.values.tolist()


def write_tab(sh: gspread.Spreadsheet, title: str, df: pd.DataFrame) -> None:
    values = to_values(df)
    n_rows, n_cols = len(values), max(len(values[0]), 1)
    if n_rows * n_cols > SHEET_CELL_LIMIT * 0.9:
        log(f"WARNING: {title} is {n_rows*n_cols:,} cells, close to the 10M Sheets limit")
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=n_rows, cols=n_cols)
    ws.clear()
    ws.resize(rows=max(n_rows, 2), cols=n_cols)
    # RAW so marks bands like "6-7" and IDs are never auto-converted to dates
    for i in range(0, n_rows, WRITE_CHUNK_ROWS):
        ws.update(range_name=f"A{i + 1}", values=values[i:i + WRITE_CHUNK_ROWS],
                  value_input_option="RAW")
    log(f"Wrote {title}: {n_rows - 1:,} rows")


# --------------------------------------------------------------------------- Transforms
def assign_track(module: str, ss_re: str, sql_re: str) -> str:
    m = str(module).lower()
    if re.search(ss_re, m):
        return "Excel"
    if re.search(sql_re, m):
        return "SQL"
    return "Other"


def _ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert(IST).dt.tz_localize(None)


def collapse_random(sub: pd.DataFrame, pass_mark: float) -> pd.DataFrame:
    """#6579 is one row per submission; collapse to one row per student x project x question
    in the same shape as #6241/#6959. Clear time = evaluation time of the FIRST submission
    whose submission-level marks >= pass mark (falls back to final question marks)."""
    if sub.empty:
        return sub
    s = sub.copy()
    s["_sub_t"] = _ts(s["submission_time"])
    s["_fb_t"] = _ts(s["feedback_given_time"])
    s["_sub_marks"] = pd.to_numeric(s.get("marks_submission_level"), errors="coerce")
    s["_pass_t"] = s["_fb_t"].where(s["_sub_marks"].ge(pass_mark))
    key = ["user_id", "batch", "project", "question_id"]
    g = s.groupby(key, dropna=False)
    out = g.agg(
        name=("student_name", "first"),
        module_name=("module_name", "first"),
        question_title=("question_title", "first"),
        project_release_date=("project_release_date", "first"),
        project_deadline_date=("project_deadline_date", "first"),
        attempt_status=("attempt_status", "first"),
        submission_status=("submission_status", "max"),   # 'Submitted' > 'Not Submitted'
        marks_obtained=("marks_obtained", "max"),
        number_of_submissions=("submission_id", "nunique"),
        first_submission=("_sub_t", "min"),
        recent_submission=("_sub_t", "max"),
        first_feedback_given_time=("_fb_t", "min"),
        latest_feedback_given_time=("_fb_t", "max"),
        first_pass_eval_time=("_pass_t", "min"),
        evaluated_submissions=("_fb_t", "count"),
    ).reset_index()
    out["submission_time"] = out["recent_submission"]
    for c in ["first_submission", "recent_submission", "first_feedback_given_time",
              "latest_feedback_given_time", "first_pass_eval_time", "submission_time"]:
        out[c] = out[c].dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")  # tz-aware text, re-parsed below
    out["source_card"] = CARDS["project_random"]
    return out


def build_projects(v1: pd.DataFrame, v2: pd.DataFrame, vr: pd.DataFrame, pass_mark: float,
                   ss_re: str, sql_re: str) -> pd.DataFrame:
    v1 = v1.assign(source_card=CARDS["project_v1"])
    v2 = v2.assign(source_card=CARDS["project_v2"])
    vr = collapse_random(vr, pass_mark)
    df = pd.concat([vr, v2, v1], ignore_index=True, sort=False)
    # Priority on overlap: #6579 (random, current batches) > #6959 > #6241 (older / non-submitters)
    key = [c for c in ["user_id", "batch", "project", "question_id"] if c in df.columns]
    df = df.drop_duplicates(subset=key, keep="first")

    for c in ["project_release_date", "project_deadline_date"]:  # date-only, no tz shift
        if c in df.columns:
            d = pd.to_datetime(df[c], errors="coerce")
            df[c] = d.dt.tz_convert(IST).dt.tz_localize(None) if d.dt.tz is not None else d
    for c in ["first_submission",
              "recent_submission", "first_feedback_given_time",
              "latest_feedback_given_time", "submission_time", "first_pass_eval_time"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce", utc=True).dt.tz_convert(IST).dt.tz_localize(None)

    df["track"] = df["module_name"].map(lambda m: assign_track(m, ss_re, sql_re))
    marks = pd.to_numeric(df.get("marks_obtained"), errors="coerce")
    df["is_submitted"] = df["submission_status"].eq("Submitted")
    df["is_cleared"] = marks.ge(pass_mark)
    # Clear date proxy: evaluation time of the passing evaluation, else last submission
    if "first_pass_eval_time" not in df.columns:
        df["first_pass_eval_time"] = pd.NaT
    clear_dt = (df["first_pass_eval_time"].fillna(df["latest_feedback_given_time"])
                .fillna(df["recent_submission"]).fillna(df["submission_time"]))
    df["cleared_date"] = clear_dt.where(df["is_cleared"])
    df["first_submission_date"] = (df["first_submission"].fillna(df["submission_time"])
                                   .where(df["is_submitted"]))
    return df


def attach_master(df: pd.DataFrame, master: pd.DataFrame, key_col: str) -> pd.DataFrame:
    if master.empty or key_col not in master.columns:
        return df
    m = master.drop_duplicates(subset=[key_col]).copy()
    m["_mk"] = m[key_col].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    m = m.drop(columns=[key_col]).rename(columns={c: f"master_{c}" for c in m.columns if c not in (key_col, "_mk")})
    out = df.copy()
    out["_k"] = out["user_id"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    return out.merge(m, how="left", left_on="_k", right_on="_mk").drop(columns=["_k", "_mk"])


def build_both_cleared(df: pd.DataFrame) -> pd.DataFrame:
    cleared = df[df["is_cleared"] & df["track"].isin(["Excel", "SQL"])]
    per = (cleared.groupby(["user_id", "track"])["cleared_date"].min()
           .unstack("track").reindex(columns=["Excel", "SQL"]))
    per.columns = ["excel_cleared_date", "sql_cleared_date"]
    info = (df.sort_values("track").groupby("user_id")
            .agg(name=("name", "first"), batch=("batch", "first")))
    both = info.join(per, how="inner").reset_index()
    both["both_cleared_date"] = both[["excel_cleared_date", "sql_cleared_date"]].max(axis=1, skipna=False)
    both["is_both_cleared"] = both["both_cleared_date"].notna()
    return both


def build_batch_summary(df: pd.DataFrame, master: pd.DataFrame, batch_col: str | None,
                        acc_col: str | None, now: datetime) -> pd.DataFrame:
    today = pd.Timestamp(now.replace(tzinfo=None))
    d = df[df["track"].isin(["Excel", "SQL"])].copy()
    rows = []
    for (batch, track), g in d.groupby(["batch", "track"]):
        g = g.drop_duplicates("user_id")  # one row per student per track
        deadline = g["project_deadline_date"].min()
        row = {
            "batch": batch, "track": track,
            "project_release_date": g["project_release_date"].min(),
            "project_deadline_date": deadline,
            "students_in_raw": g["user_id"].nunique(),
            "submitted": int(g["is_submitted"].sum()),
            "cleared_mtd": int(g["is_cleared"].sum()),
            "avg_submissions_per_clear": round(
                pd.to_numeric(g.loc[g["is_cleared"], "number_of_submissions"], errors="coerce").mean(), 2)
            if g["is_cleared"].any() else None,
        }
        if pd.notna(deadline):
            cutoff0 = deadline + pd.Timedelta(days=1)  # end of deadline day
            for w in WEEK_CHECKPOINTS:
                cp = cutoff0 + pd.Timedelta(days=7 * w)
                row[f"sub_W{w}"] = int((g["first_submission_date"] < cp).sum()) if cp <= today + pd.Timedelta(days=1) else None
                row[f"clr_W{w}"] = int((g["cleared_date"] < cp).sum()) if cp <= today + pd.Timedelta(days=1) else None
            row["clr_M0"] = row["clr_W0"]
            for mth in MONTH_CHECKPOINTS:
                cp = cutoff0 + pd.DateOffset(months=mth)
                row[f"clr_M{mth}"] = int((g["cleared_date"] < cp).sum()) if cp <= today + pd.Timedelta(days=1) else None
        rows.append(row)
    summary = pd.DataFrame(rows)

    # Accountable denominator from master data, if configured
    if not summary.empty and batch_col and acc_col and {batch_col, acc_col} <= set(master.columns):
        flag = master[acc_col].astype(str).str.strip().str.lower().isin(["true", "yes", "y", "1"])
        acc = master[flag].groupby(batch_col).size().rename("accountable")
        summary = summary.merge(acc, how="left", left_on="batch", right_index=True)
        summary["clearance_pct_mtd"] = (summary["cleared_mtd"] / summary["accountable"]).round(4)
        summary["submission_pct_mtd"] = (summary["submitted"] / summary["accountable"]).round(4)
    summary["refreshed_at_ist"] = now.strftime("%Y-%m-%d %H:%M:%S")
    return summary


# --------------------------------------------------------------------------- Main
def pick_col(cols, explicit, candidates, label):
    if explicit:
        c = norm_col(explicit)
        if c not in cols:
            log(f"WARNING: master column '{explicit}' ({label}) not found in {list(cols)[:20]}")
            return None
        return c
    for cand in candidates:
        if cand in cols:
            log(f"Master {label} column auto-detected: '{cand}'")
            return cand
    log(f"Master {label} column not found (set MASTER_{label.upper()}_COL). Columns: {list(cols)[:20]}")
    return None


def main() -> int:
    env = os.environ
    for k in ["METABASE_API_KEY", "GCP_SA_JSON"]:
        if not env.get(k):
            log(f"Missing required secret: {k}")
            return 1
    if "YOUR-METABASE-HOST" in METABASE_URL or PASS_MARK is None:
        log("Edit METABASE_URL and PASS_MARK in the CONFIG block first.")
        return 1

    now = datetime.now(IST)
    mb_url, mb_key = METABASE_URL.rstrip("/"), env["METABASE_API_KEY"]
    pass_mark = float(PASS_MARK)
    ss_re, sql_re = SS_MODULE_REGEX, SQL_MODULE_REGEX

    gc = gsheet_client(env["GCP_SA_JSON"])

    v1 = fetch_card(mb_url, mb_key, CARDS["project_v1"])
    v2 = fetch_card(mb_url, mb_key, CARDS["project_v2"])
    vr = fetch_card(mb_url, mb_key, CARDS["project_random"])
    groomer = fetch_card(mb_url, mb_key, CARDS["groomer"])
    master = read_master(gc, MASTER_SHEET_ID, MASTER_TAB) if MASTER_SHEET_ID else pd.DataFrame()
    cols = set(master.columns)
    key_col = pick_col(cols, MASTER_KEY_COL, MASTER_KEY_CANDIDATES, "key") or "user_id"
    batch_col = pick_col(cols, MASTER_BATCH_COL, MASTER_BATCH_CANDIDATES, "batch")
    acc_col = pick_col(cols, MASTER_ACCOUNTABLE_COL, MASTER_ACCOUNTABLE_CANDIDATES, "accountable")

    projects = build_projects(v1, v2, vr, pass_mark, ss_re, sql_re)
    other = projects[projects["track"] == "Other"]["module_name"].dropna().unique()
    if len(other):
        log(f"NOTE: modules not matched to Excel/SQL (excluded): {list(other)[:10]}")

    both = attach_master(build_both_cleared(projects), master, key_col)
    summary = build_batch_summary(projects, master, batch_col, acc_col, now)
    projects = attach_master(projects, master, key_col)

    tabs = {
        "Raw_SS": projects[projects["track"] == "Excel"],  # DS 02 Spreadsheets
        "Raw_SQL": projects[projects["track"] == "SQL"],
        "Both_Cleared": both,
        "Batch_Summary": summary,
        "Groomer": groomer,
    }
    if not master.empty:
        tabs["Master"] = master

    sh = gc.open_by_key(OUTPUT_SHEET_ID)
    for title, df in tabs.items():
        write_tab(sh, title, df)

    # Append-only run log (Looker can show "last refreshed")
    try:
        log_ws = sh.worksheet("_Refresh_Log")
    except gspread.WorksheetNotFound:
        log_ws = sh.add_worksheet(title="_Refresh_Log", rows=2, cols=len(tabs) + 1)
        log_ws.update(range_name="A1", values=[["refreshed_at_ist", *tabs.keys()]])
    log_ws.append_row([now.strftime("%Y-%m-%d %H:%M:%S"), *[len(d) for d in tabs.values()]],
                      value_input_option="RAW")
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
