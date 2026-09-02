"""
Portfolio version of 1C timesheet processing.

Purpose:
    transform a semi-structured management Excel report into normalized
    employee/shift records that can be joined to warehouse KPI data.

Pipeline:
    raw 1C Excel
    -> remove repeated report headers
    -> extract employee and job title
    -> identify shift-code rows and hour rows
    -> parse regular/night hours
    -> reshape to long format
    -> produce employee/shift records

The script keeps the real preparation logic while excluding company-specific
paths and downstream payroll/reporting code.
"""

from __future__ import annotations

import argparse
import calendar
import re
from pathlib import Path

import numpy as np
import pandas as pd


HEADER_MARKER = "Номер \nпо \nпоряд- \nку"

EMPLOYEE_COL = "Unnamed: 2"
HEADER_SEARCH_COL = "Unnamed: 1"
ABSENCE_CODE_COL = "Unnamed: 54"
ABSENCE_HOURS_COL = "Unnamed: 56"

REPORT_DATE_CREATED_COL = "Unnamed: 44"
REPORT_DATE_START_COL = "Unnamed: 50"
REPORT_DATE_END_COL = "Unnamed: 54"

ABSENCE_CODES = ["Б", "НН", "ДО"]

# Columns containing the day grid in the current 1C T-13 layout.
DAY_SOURCE_COLS = [
    "Unnamed: 8", "Unnamed: 10", "Unnamed: 12", "Unnamed: 13",
    "Unnamed: 15", "Unnamed: 17", "Unnamed: 19", "Unnamed: 21",
    "Unnamed: 23", "Unnamed: 25", "Unnamed: 27", "Unnamed: 29",
    "Unnamed: 31", "Unnamed: 33", "Unnamed: 36", "Unnamed: 37",
]


def normalize_employee_name(value: object) -> object:
    """
    Convert a full employee name to a compact matching key: 'Surname N'.

    Example:
        'Иванов Иван Иванович' -> 'Иванов И'
    """
    if pd.isna(value):
        return value

    parts = str(value).strip().split()
    if not parts:
        return value

    surname = parts[0].capitalize()
    first_initial = ""

    if len(parts) > 1:
        first_initial = parts[1].replace(".", "").upper()[:1]

    return f"{surname} {first_initial}".strip()


def parse_hours(value: object) -> float:
    """
    Parse hours from the 1C day grid.

    Regular shift:
        '11' -> 11

    Night shift stored as hours before/after midnight:
        '4/7' -> 11
    """
    if pd.isna(value):
        return np.nan

    value = str(value).strip()

    if "/" in value:
        parts = pd.to_numeric(value.split("/"), errors="coerce")
        return float(np.nansum(parts))

    parsed = pd.to_numeric(value, errors="coerce")
    return float(parsed) if pd.notna(parsed) else np.nan


def read_report_period(raw: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    """
    Read start/end dates from the metadata block of the 1C report.

    The source report stores metadata on row 10.
    """
    date_start = pd.to_datetime(
        raw.loc[10, REPORT_DATE_START_COL],
        format="%d.%m.%Y",
    )
    date_end = pd.to_datetime(
        raw.loc[10, REPORT_DATE_END_COL],
        format="%d.%m.%Y",
    )
    return date_start, date_end


def remove_report_headers(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Remove repeated T-13 header blocks and report footer.

    Every repeated marker occupies the marker row + six following rows.
    The workbook also contains a fixed top metadata block and footer.
    """
    marker_mask = raw[HEADER_SEARCH_COL].eq(HEADER_MARKER).to_numpy()
    marker_idx = np.where(marker_mask)[0]

    drop_mask = np.zeros(len(raw), dtype=bool)

    for idx in marker_idx:
        drop_mask[idx: idx + 7] = True

    cleaned = raw.loc[~drop_mask].reset_index(drop=True)

    # Remove the static metadata/header area and report footer.
    cleaned = cleaned.iloc[12:].reset_index(drop=True)
    cleaned = cleaned.iloc[:-6].reset_index(drop=True)

    return cleaned


def extract_employee_structure(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract employee name, position and absence totals from the 4-row
    employee blocks used by the 1C report.
    """
    result = df.copy()

    result[["ФИО", "Должность"]] = (
        result[EMPLOYEE_COL]
        .astype(str)
        .str.extract(r"^(.*?)\s*\n\s*\((.*?)\)$")
    )

    # Employee details are printed only on the first line of the block.
    result[["ФИО", "Должность"]] = (
        result[["ФИО", "Должность"]]
        .ffill(limit=3)
    )

    absence_hours = (
        result[ABSENCE_HOURS_COL]
        .astype(str)
        .str.extract(r"\((\d+)\)")[0]
        .astype(float)
    )

    for code in ABSENCE_CODES:
        result[code] = np.nan

    first_rows = (
        result.dropna(subset=["ФИО"])
        .groupby("ФИО", sort=False)
        .head(1)
        .index
    )
    first_row_map = dict(
        zip(result.loc[first_rows, "ФИО"], first_rows)
    )

    for code in ABSENCE_CODES:
        mask = result[ABSENCE_CODE_COL].eq(code)

        for idx, row in result.loc[mask].iterrows():
            employee = row["ФИО"]
            hours = absence_hours.loc[idx]

            if pd.notna(employee) and employee in first_row_map:
                result.loc[first_row_map[employee], code] = hours

    result[ABSENCE_CODES] = result[ABSENCE_CODES].ffill(limit=3)

    keep_cols = [
        "ФИО",
        "Должность",
        *ABSENCE_CODES,
        *[col for col in DAY_SOURCE_COLS if col in result.columns],
    ]

    return result[keep_cols].copy()


def select_half_month(
    df: pd.DataFrame,
    date_end: pd.Timestamp,
) -> pd.DataFrame:
    """
    Select the correct two rows from each 4-row employee block and map
    generic 'Unnamed' columns to calendar days.

    The T-13 report changes row layout between the first and second
    half of the month.
    """
    result = df.copy()

    year = date_end.year
    month = date_end.month
    days_in_month = calendar.monthrange(year, month)[1]

    unnamed_cols = [
        col for col in result.columns
        if str(col).startswith("Unnamed:")
    ]

    row_pos = np.arange(len(result))

    if date_end.day <= 15:
        # Keep first two rows from every four-row employee block.
        result = result.loc[~(row_pos % 4 >= 2)].reset_index(drop=True)

        rename_map = {
            old: day
            for old, day in zip(unnamed_cols[:15], range(1, 16))
        }
        result = result.rename(columns=rename_map)
        result = result.drop(columns=["Unnamed: 37"], errors="ignore")

    else:
        # Keep second two rows from every four-row employee block.
        result = result.loc[~(row_pos % 4 < 2)].reset_index(drop=True)

        rename_map = {
            old: day
            for old, day in zip(unnamed_cols[:16], range(16, 32))
        }
        result = result.rename(columns=rename_map)

        # Remove impossible dates (e.g. day 31 in a 30-day month).
        extra_days = range(days_in_month + 1, 32)
        result = result.drop(
            columns=[day for day in extra_days if day in result.columns],
            errors="ignore",
        )

    return result


def to_shift_records(
    timesheet: pd.DataFrame,
    date_end: pd.Timestamp,
) -> pd.DataFrame:
    """
    Convert the semi-structured wide day grid to one row per employee/shift.

    Output:
        ФИО
        Должность
        Смена
        Тип смены
        Часы
    """
    day_cols = [col for col in timesheet.columns if isinstance(col, int)]

    # Rows containing letters are shift-code rows; the paired rows contain hours.
    shift_row_mask = timesheet[day_cols].apply(
        lambda row: row.astype(str).str.contains(
            r"[А-ЯA-Z]",
            na=False,
        ).any(),
        axis=1,
    )

    shifts = timesheet.loc[shift_row_mask].copy()
    hours = timesheet.loc[~shift_row_mask].copy()

    hours[day_cols] = hours[day_cols].map(parse_hours)

    shifts = shifts.reset_index(drop=True)
    hours = hours.reset_index(drop=True)

    # Preserve only complete shift/hour pairs.
    pair_count = min(len(shifts), len(hours))
    shifts = shifts.iloc[:pair_count].copy()
    hours = hours.iloc[:pair_count].copy()

    shifts_long = shifts.melt(
        id_vars=["ФИО", "Должность"],
        value_vars=day_cols,
        var_name="day",
        value_name="Тип смены",
    )

    hours_long = hours.melt(
        id_vars=["ФИО", "Должность"],
        value_vars=day_cols,
        var_name="day",
        value_name="Часы",
    )

    records = shifts_long.merge(
        hours_long[["ФИО", "Должность", "day", "Часы"]],
        on=["ФИО", "Должность", "day"],
        how="left",
    )

    records["Смена"] = pd.to_datetime(
        {
            "year": date_end.year,
            "month": date_end.month,
            "day": records["day"],
        }
    )

    records["Часы"] = pd.to_numeric(
        records["Часы"],
        errors="coerce",
    ).fillna(0)

    records = (
        records
        .drop(columns="day")
        .dropna(subset=["Тип смены"])
        .query("`Тип смены` != 'nan'")
        .reset_index(drop=True)
    )

    return records[
        ["ФИО", "Должность", "Смена", "Тип смены", "Часы"]
    ]


def build_absence_summary(
    timesheet: pd.DataFrame,
    shift_records: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build an employee-level summary of hours excluded from the working-time norm.

    Current source codes:
        Б  - sick leave
        НН - absence/no-show
        ДО - leave category used in the source report
    """
    summary = (
        timesheet[["ФИО", "Должность", *ABSENCE_CODES]]
        .drop_duplicates()
        .copy()
    )

    summary[ABSENCE_CODES] = summary[ABSENCE_CODES].fillna(0)

    period_start = shift_records["Смена"].min().date()
    period_end = shift_records["Смена"].max().date()
    output_col = f"{period_start} - {period_end}"

    summary[output_col] = summary[ABSENCE_CODES].sum(axis=1)

    return (
        summary.groupby("ФИО", as_index=False)[output_col]
        .sum()
    )


def process_timesheet(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the complete 1C timesheet preparation pipeline.
    """
    _, date_end = read_report_period(raw)

    cleaned = remove_report_headers(raw)
    structured = extract_employee_structure(cleaned)
    structured = select_half_month(structured, date_end)

    structured["ФИО"] = structured["ФИО"].apply(
        normalize_employee_name
    )

    shift_records = to_shift_records(structured, date_end)
    absence_summary = build_absence_summary(
        structured,
        shift_records,
    )

    return shift_records, absence_summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize a 1C T-13 timesheet for warehouse KPI calculations."
    )
    parser.add_argument("input_xlsx", type=Path)
    parser.add_argument("output_shifts_csv", type=Path)
    parser.add_argument(
        "--absence-output",
        type=Path,
        default=None,
        help="Optional CSV for employee absence-hour summary.",
    )
    args = parser.parse_args()

    raw = pd.read_excel(args.input_xlsx)

    shift_records, absence_summary = process_timesheet(raw)

    args.output_shifts_csv.parent.mkdir(parents=True, exist_ok=True)
    shift_records.to_csv(
        args.output_shifts_csv,
        index=False,
        encoding="utf-8-sig",
    )

    if args.absence_output is not None:
        args.absence_output.parent.mkdir(parents=True, exist_ok=True)
        absence_summary.to_csv(
            args.absence_output,
            index=False,
            encoding="utf-8-sig",
        )


if __name__ == "__main__":
    main()
