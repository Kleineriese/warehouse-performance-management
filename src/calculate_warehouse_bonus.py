"""
Portfolio version of a warehouse KPI and bonus calculation pipeline.

Pipeline:
    operation complexity
    -> product complexity
    -> KPI points
    -> hourly benchmark
    -> achievement %
    -> bonus multiplier
    -> shift bonus

The script intentionally contains only the core business logic and excludes
company-specific file paths, employee names, warehouse mappings and reporting code.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


STANDARD_SHIFT_HOURS = 11
MAX_BONUS_ACHIEVEMENT = 3.0  # Cap paid KPI points at 300% of the shift plan.

# Bonus multiplier is deliberately moderate around the 100% benchmark.
BONUS_BINS = [-np.inf, 90, 100, 110, 120, np.inf]
BONUS_MULTIPLIERS = [0.8, 0.9, 1.0, 1.1, 1.2]


def add_product_complexity(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add product complexity coefficient used in KPI calculation.

    Expected columns:
        Объем                  - product volume in source units
        Вес                    - physical weight in kg
        Вид операции реальный  - normalized warehouse operation

    Business rules from the production model:
      * volumetric weight = volume / 5;
      * for packing:
            <= 3 kg  -> 1
            <= 5 kg  -> 3
            >  5 kg  -> 6
      * for picking:
            <= 0.3 kg -> 1.5
            >  0.3 kg -> 1
      * all other operations -> 1
    """
    result = df.copy()

    result["Объемный вес"] = result["Объем"] / 5
    result["Расчетный вес"] = np.maximum(
        result["Объемный вес"],
        result["Вес"],
    )

    max_weight = result["Расчетный вес"]

    is_pack = result["Вид операции реальный"].isin(
        ["Упаковка FBS", "Упаковка ФБО"]
    )
    is_pick = result["Вид операции реальный"].eq("Отбор")

    result["Сложность товара (КТ)"] = np.select(
        condlist=[
            is_pack & (max_weight <= 3),
            is_pack & (max_weight <= 5),
            is_pack & (max_weight > 5),
            is_pick & (max_weight <= 0.3),
            is_pick & (max_weight > 0.3),
        ],
        choicelist=[1.0, 3.0, 6.0, 1.5, 1.0],
        default=1.0,
    )

    return result


def add_kpi_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate KPI points for each warehouse operation line.

    Expected columns:
        Сложность операции (КО)
        Сложность товара (КТ)
        Кол-во товара
        Кратность
        Расчетный вес

    Multiplicity corrects bulk-pack records where the system quantity is larger
    than the number of real physical picks. For light items the base score is
    divided by pack multiplicity; heavy items keep the full quantity-based score.
    """
    result = df.copy()

    multiplicity = (
        pd.to_numeric(result["Кратность"], errors="coerce")
        .replace(0, np.nan)
        .fillna(1)
    )

    base_kpi = (
        result["Сложность операции (КО)"]
        * result["Сложность товара (КТ)"]
        * result["Кол-во товара"]
    )

    result["Балл KPI"] = np.where(
        result["Расчетный вес"] <= 3,
        base_kpi / multiplicity,
        base_kpi,
    )

    return result


def aggregate_employee_shifts(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate line-level KPI points to employee-shift level.

    Expected columns:
        Исполнитель, Смена, Склад, Часы, Балл KPI

    Optional:
        Должность, Тип смены
    """
    group_cols = ["Исполнитель", "Смена", "Склад"]
    optional_group_cols = [
        col for col in ["Должность", "Тип смены"] if col in df.columns
    ]
    group_cols += optional_group_cols

    shifts = (
        df.groupby(group_cols, as_index=False)
        .agg(
            Часы=("Часы", "first"),
            **{"Балл KPI": ("Балл KPI", "sum")},
        )
    )

    shifts["Балл KPI за час"] = np.divide(
        shifts["Балл KPI"],
        shifts["Часы"],
        out=np.zeros(len(shifts), dtype=float),
        where=shifts["Часы"].fillna(0).to_numpy() > 0,
    )

    return shifts


def calculate_hourly_benchmark(
    shifts: pd.DataFrame,
    quantile: float = 0.50,
) -> pd.DataFrame:
    """
    Calculate the warehouse KPI benchmark per productive hour.

    The production approach uses the median (P50) of historical employee-shifts
    within each warehouse. Using KPI/hour makes the benchmark compatible with
    regular, shortened and overtime shifts.
    """
    valid = shifts.loc[
        (shifts["Часы"] > 0) & shifts["Балл KPI за час"].notna()
    ].copy()

    benchmark = (
        valid.groupby("Склад", as_index=False)["Балл KPI за час"]
        .quantile(quantile)
        .rename(columns={"Балл KPI за час": "План KPI за час"})
    )

    benchmark["План KPI стандартной смены"] = (
        benchmark["План KPI за час"] * STANDARD_SHIFT_HOURS
    )

    return benchmark


def add_bonus_calculation(
    shifts: pd.DataFrame,
    benchmark: pd.DataFrame,
    bonus_at_100: pd.DataFrame,
) -> pd.DataFrame:
    """
    Calculate plan, achievement, multiplier and bonus for each employee-shift.

    bonus_at_100 must contain:
        Склад
        Премия за 100%

    The price of one KPI point is anchored to the bonus paid for a standard
    11-hour shift at 100% of the warehouse benchmark.

    Bonus formula:
        paid_points * price_per_point * multiplier

    where paid_points are capped at MAX_BONUS_ACHIEVEMENT * shift_plan.
    """
    result = (
        shifts.merge(benchmark, on="Склад", how="left")
        .merge(bonus_at_100[["Склад", "Премия за 100%"]], on="Склад", how="left")
    )

    result["План KPI"] = result["План KPI за час"] * result["Часы"]

    result["% выполнения"] = np.divide(
        result["Балл KPI"],
        result["План KPI"],
        out=np.zeros(len(result), dtype=float),
        where=result["План KPI"].fillna(0).to_numpy() > 0,
    ) * 100

    result["КоэфПремии"] = pd.cut(
        result["% выполнения"],
        bins=BONUS_BINS,
        labels=BONUS_MULTIPLIERS,
        right=False,
    ).astype(float)

    result["Цена 1 балла KPI"] = np.divide(
        result["Премия за 100%"],
        result["План KPI стандартной смены"],
        out=np.zeros(len(result), dtype=float),
        where=result["План KPI стандартной смены"].fillna(0).to_numpy() > 0,
    )

    paid_points = np.minimum(
        result["Балл KPI"],
        result["План KPI"] * MAX_BONUS_ACHIEVEMENT,
    )

    result["ПремияСмена"] = (
        paid_points
        * result["Цена 1 балла KPI"]
        * result["КоэфПремии"]
    )

    result["% выполнения"] = result["% выполнения"].round(1)
    result["План KPI"] = result["План KPI"].round(0)
    result["Балл KPI"] = result["Балл KPI"].round(0)
    result["ПремияСмена"] = result["ПремияСмена"].round(0)

    return result


def build_bonus_model(
    operations: pd.DataFrame,
    bonus_at_100: pd.DataFrame,
    benchmark_quantile: float = 0.50,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the complete portfolio pipeline.

    The input operations are assumed to be already enriched with:
        Сложность операции (КО), Кол-во товара, Кратность,
        Объем, Вес, Часы, Склад and normalized operation name.
    """
    scored = add_product_complexity(operations)
    scored = add_kpi_points(scored)

    shifts = aggregate_employee_shifts(scored)
    benchmark = calculate_hourly_benchmark(
        shifts,
        quantile=benchmark_quantile,
    )
    shifts = add_bonus_calculation(shifts, benchmark, bonus_at_100)

    return shifts, benchmark


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calculate warehouse KPI benchmarks and employee shift bonuses."
    )
    parser.add_argument("operations_csv", type=Path)
    parser.add_argument(
        "bonus_config_csv",
        type=Path,
        help="CSV with columns: Склад, Премия за 100%",
    )
    parser.add_argument("output_csv", type=Path)
    parser.add_argument(
        "--benchmark-quantile",
        type=float,
        default=0.50,
        help="Historical KPI/hour quantile used as the 100%% benchmark.",
    )
    args = parser.parse_args()

    operations = pd.read_csv(args.operations_csv)
    operations["Смена"] = pd.to_datetime(operations["Смена"])

    bonus_config = pd.read_csv(args.bonus_config_csv)

    shift_bonus, benchmark = build_bonus_model(
        operations=operations,
        bonus_at_100=bonus_config,
        benchmark_quantile=args.benchmark_quantile,
    )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    shift_bonus.to_csv(args.output_csv, index=False, encoding="utf-8-sig")

    benchmark_path = args.output_csv.with_name(
        f"{args.output_csv.stem}_benchmarks.csv"
    )
    benchmark.to_csv(benchmark_path, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
