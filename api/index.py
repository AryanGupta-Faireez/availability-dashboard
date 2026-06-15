"""Vercel serverless entry point for the Availability Dashboard."""

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

ROOT     = Path(__file__).parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR_OVERRIDE", str(ROOT / "data")))
STAMP    = DATA_DIR / ".last_refresh"

DAY_ORDER = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

app = FastAPI(title="Faireez Availability Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_df: Optional[pd.DataFrame] = None


def _load() -> pd.DataFrame:
    global _df
    if _df is not None:
        return _df
    path = DATA_DIR / "availability.csv"
    if not path.exists():
        raise FileNotFoundError("availability.csv missing — run refresh_data.py first")
    df = pd.read_csv(path, low_memory=False)
    for col in ["effective_bookable_hours", "effective_remaining_hours",
                "effective_Slots_less_than_1hr", "effective_Slots_1hr_to_2hr",
                "effective_Slots_more_than_2hr", "bookable_hours",
                "remaining_avail", "Slots_less_than_1hr", "Slots_1hr_to_2hr",
                "Slots_more_than_2hr"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    _df = df
    return _df


def _last_refresh() -> str:
    try:
        return STAMP.read_text().strip() if STAMP.exists() else "never"
    except Exception:
        return "unknown"


def _filter(df: pd.DataFrame, country=None, city=None, state=None,
            project=None, neighbourhood=None) -> pd.DataFrame:
    if country and country != "all":
        df = df[df["Country"] == country]
    if city and city != "all":
        df = df[df["City"] == city]
    if state and state != "all":
        df = df[df["State"] == state]
    if project and project != "all":
        df = df[df["Project"] == project]
    if neighbourhood and neighbourhood != "all":
        df = df[df["Neighbourhood"] == neighbourhood]
    return df


def _kpis(df: pd.DataFrame, prefix: str) -> dict:
    """Compute KPI dict for either 'effective_' (normalised) or raw columns."""
    bookable  = float(df[f"{prefix}bookable_hours"].sum())
    remaining = float(df[f"{prefix}remaining_hours"].sum()) if f"{prefix}remaining_hours" in df.columns else float(df["remaining_avail"].sum())
    booked    = bookable - remaining
    efficiency = round(booked / bookable * 100, 1) if bookable > 0 else 0
    return {
        "bookable":    round(bookable, 1),
        "hours_left":  round(remaining, 1),
        "hours_booked": round(booked, 1),
        "efficiency":  efficiency,
        "slots_lt1":   round(float(df[f"{prefix}Slots_less_than_1hr"].sum()), 1),
        "slots_1to2":  round(float(df[f"{prefix}Slots_1hr_to_2hr"].sum()), 1),
        "slots_gt2":   round(float(df[f"{prefix}Slots_more_than_2hr"].sum()), 1),
    }


# ── Serve frontend ─────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "index.html"))


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/api/status")
def status():
    return {"last_refresh": _last_refresh()}


# ── Filters ────────────────────────────────────────────────────────────────────
@app.get("/api/filters")
def get_filters():
    df = _load()
    return {
        "countries":      sorted(df["Country"].dropna().unique().tolist()),
        "cities":         sorted(df["City"].dropna().unique().tolist()),
        "states":         sorted(s for s in df["State"].dropna().unique().tolist() if s),
        "projects":       sorted(df["Project"].dropna().unique().tolist()),
        "neighbourhoods": sorted(n for n in df["Neighbourhood"].dropna().unique().tolist() if n),
    }


# ── Section 1: Overall summary (raw values, all buildings) ────────────────────
@app.get("/api/availability/overall")
def overall(
    country: Optional[str] = None, city: Optional[str] = None,
    state: Optional[str] = None, project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
):
    df = _filter(_load(), country, city, state, project, neighbourhood)
    if df.empty:
        return {"kpis": {}, "buildings": []}

    # Normalised KPIs
    bookable  = float(df["effective_bookable_hours"].sum())
    remaining = float(df["effective_remaining_hours"].sum())
    booked    = bookable - remaining
    kpis = {
        "bookable":     round(bookable, 1),
        "hours_left":   round(remaining, 1),
        "hours_booked": round(booked, 1),
        "efficiency":   round(booked / bookable * 100, 1) if bookable > 0 else 0,
        "slots_lt1":    round(float(df["effective_Slots_less_than_1hr"].sum()), 1),
        "slots_1to2":   round(float(df["effective_Slots_1hr_to_2hr"].sum()), 1),
        "slots_gt2":    round(float(df["effective_Slots_more_than_2hr"].sum()), 1),
    }

    # Per-building efficiency for bar chart (normalised)
    bld = (
        df.groupby(["LocationId", "Project"], as_index=False)
        .agg(bookable=("effective_bookable_hours", "sum"), remaining=("effective_remaining_hours", "sum"))
    )
    bld["efficiency"] = (
        (bld["bookable"] - bld["remaining"]) / bld["bookable"] * 100
    ).where(bld["bookable"] > 0, 0).round(1)
    bld = bld.sort_values("efficiency", ascending=False)

    buildings = [
        {
            "location_id": int(r["LocationId"]),
            "project":     r["Project"],
            "bookable":    round(float(r["bookable"]), 1),
            "remaining":   round(float(r["remaining"]), 1),
            "efficiency":  float(r["efficiency"]),
        }
        for _, r in bld.iterrows()
    ]

    return {"kpis": kpis, "buildings": buildings}


# ── Section 2 & 3: Calendar (normalised or actual) ────────────────────────────
@app.get("/api/availability/calendar")
def calendar(
    mode: str = "normalised",  # "normalised" | "actual"
    country: Optional[str] = None, city: Optional[str] = None,
    state: Optional[str] = None, project: Optional[str] = None,
    neighbourhood: Optional[str] = None,
):
    df = _filter(_load(), country, city, state, project, neighbourhood)
    if df.empty:
        return {"kpis": {}, "weeks": [], "calendar": {}}

    if mode == "normalised":
        rem_col   = "effective_remaining_hours"
        book_col  = "effective_bookable_hours"
        lt1_col   = "effective_Slots_less_than_1hr"
        m12_col   = "effective_Slots_1hr_to_2hr"
        gt2_col   = "effective_Slots_more_than_2hr"
    else:
        rem_col   = "remaining_avail"
        book_col  = "bookable_hours"
        lt1_col   = "Slots_less_than_1hr"
        m12_col   = "Slots_1hr_to_2hr"
        gt2_col   = "Slots_more_than_2hr"

    bookable  = float(df[book_col].sum())
    remaining = float(df[rem_col].sum())
    booked    = bookable - remaining
    kpis = {
        "bookable":     round(bookable, 1),
        "hours_left":   round(remaining, 1),
        "hours_booked": round(booked, 1),
        "efficiency":   round(booked / bookable * 100, 1) if bookable > 0 else 0,
        "slots_lt1":    round(float(df[lt1_col].sum()), 1),
        "slots_1to2":   round(float(df[m12_col].sum()), 1),
        "slots_gt2":    round(float(df[gt2_col].sum()), 1),
    }

    # Calendar: sum per (WeekStartDate, Day)
    cal = (
        df.groupby(["WeekStartDate", "Day"], as_index=False)
        .agg(
            hours_left=(rem_col, "sum"),
            bookable_h=(book_col, "sum"),
            slots_lt1=(lt1_col, "sum"),
            slots_1to2=(m12_col, "sum"),
            slots_gt2=(gt2_col, "sum"),
        )
    )
    cal["hours_left"]  = cal["hours_left"].round(1)
    cal["bookable_h"]  = cal["bookable_h"].round(1)
    cal["slots_lt1"]   = cal["slots_lt1"].round(1)
    cal["slots_1to2"]  = cal["slots_1to2"].round(1)
    cal["slots_gt2"]   = cal["slots_gt2"].round(1)
    cal["pct_booked"] = (
        (cal["bookable_h"] - cal["hours_left"]) / cal["bookable_h"] * 100
    ).where(cal["bookable_h"] > 0, 0).round(1)

    weeks = sorted(cal["WeekStartDate"].unique().tolist())

    # Build nested dict: calendar[day][week] = {hours_left, pct_booked, slots_*}
    calendar_data: dict = {day: {} for day in DAY_ORDER}
    for _, row in cal.iterrows():
        day = str(row["Day"]).upper().strip()
        if day in calendar_data:
            calendar_data[day][row["WeekStartDate"]] = {
                "hours_left": float(row["hours_left"]),
                "pct_booked": float(row["pct_booked"]),
                "slots_lt1":  float(row["slots_lt1"]),
                "slots_1to2": float(row["slots_1to2"]),
                "slots_gt2":  float(row["slots_gt2"]),
            }

    return {"kpis": kpis, "weeks": weeks, "calendar": calendar_data}
