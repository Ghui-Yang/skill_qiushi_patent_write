from __future__ import annotations

"""
main_call_y2m_rolling_optimizer_loop_v9.py

循环调用 y2m_rolling_optimizer_module_v9.py 中的独立算法函数。

新版接口说明：
1) 算法函数接口为 run_y2m_rolling_optimizer(input_map)；
2) input_map 和 output_map 均为单层 dict；
3) input_map 内部不再嵌套 solver_options；
4) 历史实绩 P_h2_actual_MW、P_gen_actual_MW、P_buy_actual_MW、P_sell_actual_MW
   为已完成历史小时功率求和值，不再是年度数组；
5) 算法模块不再返回 DataFrame；
6) 主程序仍然将 output_map 中的 list 结果保存为 Excel；
7) input_map 新增副产氧参数：O2_byproduct_Nm3_per_Nm3_H2、O2_price_yuan_per_Nm3；
8) V9 算法模块内部如开启边界并行，将使用 ProcessPoolExecutor；Windows 下主程序必须保留 if __name__ == "__main__" 入口保护；
9) V9 主程序测试侧也根据 Excel 行数自动适配 8760h 平年 / 8784h 闰年，接口不新增字段。
"""

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from y2m_rolling_optimizer_module_v9 import run_y2m_rolling_optimizer


# =============================================================================
# 工具函数：根据年度小时数构造月份索引，自动适配 8760h 平年 / 8784h 闰年
# =============================================================================
def build_month_of_hour_by_year_hours(year_hours: int) -> np.ndarray:
    if year_hours == 8760:
        days_per_month = np.array([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], dtype=int)
    elif year_hours == 8784:
        days_per_month = np.array([31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], dtype=int)
    else:
        raise ValueError(f"年度小时级数据行数必须为 8760 或 8784，当前为 {year_hours}。")

    hours_per_month = days_per_month * 24
    month_of_hour = np.repeat(np.arange(1, 13, dtype=int), hours_per_month)
    if month_of_hour.size != year_hours:
        raise RuntimeError(
            f"内部月份索引构造错误：month_of_hour长度={month_of_hour.size}，year_hours={year_hours}。"
        )
    return month_of_hour


# =============================================================================
# 工具函数：打印单次调用结果摘要
# =============================================================================
def print_call_summary(opt_month: int, output_map: dict[str, Any]) -> None:
    print("\n================ 单月调用结果 ================")
    print(f"opt_month                       : {opt_month}")
    print(f"success                         : {output_map.get('success')}")
    print(f"plan_available                  : {output_map.get('plan_available')}")
    print(f"dispatch_allowed                : {output_map.get('dispatch_allowed')}")
    print(f"message                         : {output_map.get('message')}")
    print(f"exit_reason                     : {output_map.get('exit_reason')}")
    print(f"final_status_code               : {output_map.get('final_status_code')}")
    print(f"final_status_name               : {output_map.get('final_status_name')}")
    print(f"final_solve_mode                : {output_map.get('final_solve_mode')}")
    print(f"selected_stage_name             : {output_map.get('selected_stage_name')}")
    print(f"log_file                        : {output_map.get('log_file')}")
    print(f"current_month_Dm_MW             : {output_map.get('current_month_Dm_MW')}")
    print(f"current_month_H2_kg             : {output_map.get('current_month_H2_kg')}")
    print(f"current_month_alpha             : {output_map.get('current_month_alpha')}")
    print(f"current_month_CE_kgCO2_per_kgH2 : {output_map.get('current_month_CE_kgCO2_per_kgH2')}")
    print(f"H_done_kg                       : {output_map.get('H_done_kg')}")
    print(f"H_original_target_kg            : {output_map.get('H_original_target_kg')}")
    print(f"min_h2_opt_kg                   : {output_map.get('min_h2_opt_kg')}")
    print(f"max_h2_opt_kg                   : {output_map.get('max_h2_opt_kg')}")
    print(f"h2_target_relation              : {output_map.get('h2_target_relation')}")
    print(f"H_dispatch_target_kg            : {output_map.get('H_dispatch_target_kg')}")
    print(f"H_dispatch_actual_kg            : {output_map.get('H_dispatch_actual_kg')}")
    print(f"H_target_adjusted_flag          : {output_map.get('H_target_adjusted_flag')}")
    print(f"H_target_adjust_reason          : {output_map.get('H_target_adjust_reason')}")
    print(f"fallback_used                   : {output_map.get('fallback_used')}")
    print(f"fallback_from_stage             : {output_map.get('fallback_from_stage')}")
    print(f"fallback_to_stage               : {output_map.get('fallback_to_stage')}")
    print(f"problem_code                    : {output_map.get('problem_code')}")
    print(f"solver_status                   : {output_map.get('solver_status')}")
    print(f"termination_condition           : {output_map.get('termination_condition')}")
    print(f"solve_time_sec                  : {output_map.get('solve_time_sec')}")
    print("==============================================")


# =============================================================================
# 工具函数：保存一次调用返回的结果
# =============================================================================
def save_month_result(output_dir: Path, opt_month: int, output_map: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_excel = output_dir / f"第{opt_month}个月优化结果_接口循环版_pyomo.xlsx"

    hourly_plan = pd.DataFrame(
        {
            "hour_index": output_map.get("hour_index", []),
            "month": output_map.get("month_hourly", []),
            "P_gen_MW": output_map.get("P_gen_MW", []),
            "P_h2_MW": output_map.get("P_h2_MW", []),
            "P_buy_MW": output_map.get("P_buy_MW", []),
            "P_sell_MW": output_map.get("P_sell_MW", []),
        }
    )

    monthly_plan = pd.DataFrame(
        {
            "month": output_map.get("month", []),
            "Gen_MWh": output_map.get("Gen_MWh", []),
            "H2_kg": output_map.get("H2_kg", []),
            "Buy_MWh": output_map.get("Buy_MWh", []),
            "Sell_MWh": output_map.get("Sell_MWh", []),
            "alpha_month": output_map.get("alpha_month", []),
            "Dm_MW": output_map.get("Dm_MW", []),
            "CE_kgCO2_per_kgH2": output_map.get("CE_kgCO2_per_kgH2", []),
        }
    )

    scalar_summary = {}
    for k, v in output_map.items():
        if not isinstance(v, list):
            scalar_summary[k] = v

    call_summary = pd.DataFrame([scalar_summary])

    with pd.ExcelWriter(out_excel, engine="openpyxl") as writer:
        hourly_plan.to_excel(writer, sheet_name="hourly_plan", index=False)
        monthly_plan.to_excel(writer, sheet_name="monthly_plan", index=False)
        call_summary.to_excel(writer, sheet_name="call_summary", index=False)

    return out_excel


# =============================================================================
# 工具函数：从 output_map 中提取当前月小时级结果，并回填为下一轮历史求和值
# =============================================================================
def get_current_month_power_sums_from_output(
    output_map: dict[str, Any],
    opt_month: int,
) -> tuple[float, float, float, float]:
    if not output_map.get("success", False):
        return 0.0, 0.0, 0.0, 0.0

    month_hourly = np.asarray(output_map.get("month_hourly", []), dtype=int)
    if month_hourly.size == 0:
        raise RuntimeError(f"第 {opt_month} 个月 output_map 中没有小时级月份信息，无法回填历史求和值。")

    idx = month_hourly == opt_month
    if not np.any(idx):
        raise RuntimeError(f"第 {opt_month} 个月 output_map 中没有当前月小时级结果，无法回填历史求和值。")

    P_h2 = np.asarray(output_map.get("P_h2_MW", []), dtype=float)
    P_gen = np.asarray(output_map.get("P_gen_MW", []), dtype=float)
    P_buy = np.asarray(output_map.get("P_buy_MW", []), dtype=float)
    P_sell = np.asarray(output_map.get("P_sell_MW", []), dtype=float)

    n = month_hourly.size
    if P_h2.size != n or P_gen.size != n or P_buy.size != n or P_sell.size != n:
        raise RuntimeError("output_map 中小时级功率 list 长度不一致，无法回填历史求和值。")

    if (
        np.any(~np.isfinite(P_h2[idx]))
        or np.any(~np.isfinite(P_gen[idx]))
        or np.any(~np.isfinite(P_buy[idx]))
        or np.any(~np.isfinite(P_sell[idx]))
    ):
        raise RuntimeError(f"第 {opt_month} 个月小时级计划结果存在 NaN 或 Inf，无法回填历史求和值。")

    P_h2_sum = float(np.sum(P_h2[idx]))
    P_gen_sum = float(np.sum(P_gen[idx]))
    P_buy_sum = float(np.sum(P_buy[idx]))
    P_sell_sum = float(np.sum(P_sell[idx]))

    return P_h2_sum, P_gen_sum, P_buy_sum, P_sell_sum


# =============================================================================
# 主程序：外部系统按月循环调用算法函数
# =============================================================================
def main() -> None:
    # -------------------------------------------------------------------------
    # 1. 外部系统读取全年小时级预测数据，自动适配 8760h 平年 / 8784h 闰年
    # -------------------------------------------------------------------------
    input_excel = Path.cwd() / "8784h数据示例.xlsx"
    if not input_excel.exists():
        raise FileNotFoundError(f"找不到输入文件：{input_excel}")

    df_input = pd.read_excel(input_excel)
    if df_input.shape[1] < 3:
        raise ValueError("Excel 至少需要前三列：P_pre_MW、pi_sell_yuan_per_kWh、pi_buy_yuan_per_kWh。")

    data = df_input.iloc[:, :3].to_numpy(dtype=float)
    year_hours = int(data.shape[0])
    if year_hours not in {8760, 8784}:
        raise ValueError(f"Excel 行数必须为 8760 或 8784，当前为 {year_hours}。")
    if not np.isfinite(data).all():
        raise ValueError("Excel 前三列存在 NaN 或 Inf，请检查输入数据。")

    P_pre_MW = data[:, 0]
    pi_sell_yuan_per_kWh = data[:, 1]
    pi_buy_yuan_per_kWh = data[:, 2]

    if np.any(P_pre_MW < 0) or np.any(pi_sell_yuan_per_kWh < 0) or np.any(pi_buy_yuan_per_kWh < 0):
        raise ValueError("P_pre_MW、pi_sell_yuan_per_kWh、pi_buy_yuan_per_kWh 不允许为负。")

    # -------------------------------------------------------------------------
    # 2. 外部系统初始化历史实绩求和值
    # -------------------------------------------------------------------------
    # 注意：
    # 这里不再维护年度长度历史数组。
    # P_h2_actual_MW / P_gen_actual_MW / P_buy_actual_MW / P_sell_actual_MW
    # 表示“已完成历史小时功率求和值”。
    #
    # 若 dt_h = 1，则：
    # E_gen_done_MWh = P_gen_actual_MW * dt_h
    # H_done_kg = P_h2_actual_MW * dt_h * h2_kg_per_mwh
    # -------------------------------------------------------------------------
    P_h2_actual_MW = 0.0
    P_gen_actual_MW = 0.0
    P_buy_actual_MW = 0.0
    P_sell_actual_MW = 0.0

    month_of_hour = build_month_of_hour_by_year_hours(year_hours)
    print(f"年度小时级数据长度 year_hours={year_hours}，已按 {'闰年8784h' if year_hours == 8784 else '平年8760h'} 构造月份索引。")

    # -------------------------------------------------------------------------
    # 3. 外部系统准备固定参数
    # -------------------------------------------------------------------------
    economic_dispatch_mode = "fixed_h2_target"  # 可选："fixed_h2_target" / "free_h2"
    dt_h = 1.0

    H_year_kg = 15000000
    alpha_year = 0.4
    k_H_kWh_per_Nm3 = 5.0
    rho_H2_kg_per_Nm3 = 0.089
    P_h2_min_MW = 0.0
    P_h2_max_MW = 170.0

    P_sell_max_MW = 450 * 0.4
    P_buy_max_MW = 450 * 0.4
    Dm_max_MW = np.array([450 * 0.4] * 12, dtype=float)
    C_cap_month_yuan_per_kW_month = np.array([31.5] * 12, dtype=float)

    EF_tCO2_per_MWh = 0.54
    CE_limit_kgCO2_per_kgH2 = 4.90
    H2_price_yuan_per_kg = 25.0
    carbon_price_yuan_per_tCO2 = 80.0
    O2_byproduct_Nm3_per_Nm3_H2 = 0.5
    O2_price_yuan_per_Nm3 = 1.5

    time_limit_sec = 1200
    mip_gap = 0.0005

    output_dir = Path.cwd() / "月度循环调用结果_接口版_制氧收益_v9_兼容8760_8784"
    log_dir = Path.cwd() / "logs"

    call_summary_rows: list[dict[str, Any]] = []
    actual_sum_rows: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 4. 从 1 月到 12 月，循环调用独立算法函数
    # -------------------------------------------------------------------------
    for opt_month in range(1, 13):
        print("\n\n============================================================")
        print(f"开始第 {opt_month} 个月外部调用")
        print("============================================================")

        # ---------------------------------------------------------------------
        # 4.1 构造 input_map
        # ---------------------------------------------------------------------
        input_map: dict[str, Any] = {
            "opt_month": opt_month,
            "economic_dispatch_mode": economic_dispatch_mode,
            "dt_h": dt_h,

            "P_pre_MW": P_pre_MW.tolist(),
            "pi_sell_yuan_per_kWh": pi_sell_yuan_per_kWh.tolist(),
            "pi_buy_yuan_per_kWh": pi_buy_yuan_per_kWh.tolist(),

            # 历史实绩求和值，不是数组
            "P_h2_actual_MW": P_h2_actual_MW,
            "P_gen_actual_MW": P_gen_actual_MW,
            "P_buy_actual_MW": P_buy_actual_MW,
            "P_sell_actual_MW": P_sell_actual_MW,

            "H_year_kg": H_year_kg,
            "alpha_year": alpha_year,
            "k_H_kWh_per_Nm3": k_H_kWh_per_Nm3,
            "rho_H2_kg_per_Nm3": rho_H2_kg_per_Nm3,
            "P_h2_min_MW": P_h2_min_MW,
            "P_h2_max_MW": P_h2_max_MW,
            "P_sell_max_MW": P_sell_max_MW,
            "P_buy_max_MW": P_buy_max_MW,
            "Dm_max_MW": Dm_max_MW.tolist(),
            "C_cap_month_yuan_per_kW_month": C_cap_month_yuan_per_kW_month.tolist(),
            "EF_tCO2_per_MWh": EF_tCO2_per_MWh,
            "CE_limit_kgCO2_per_kgH2": CE_limit_kgCO2_per_kgH2,
            "H2_price_yuan_per_kg": H2_price_yuan_per_kg,
            "carbon_price_yuan_per_tCO2": carbon_price_yuan_per_tCO2,
            "O2_byproduct_Nm3_per_Nm3_H2": O2_byproduct_Nm3_per_Nm3_H2,
            "O2_price_yuan_per_Nm3": O2_price_yuan_per_Nm3,

            "time_limit_sec": time_limit_sec,
            "mip_gap": mip_gap,

            "log_dir": str(log_dir),
        }

        # ---------------------------------------------------------------------
        # 4.2 调用独立算法函数
        # ---------------------------------------------------------------------
        output_map = run_y2m_rolling_optimizer(input_map)

        # ---------------------------------------------------------------------
        # 4.3 打印摘要
        # ---------------------------------------------------------------------
        print_call_summary(opt_month, output_map)
        if output_map.get("final_status_code") in {11, 12}:
            print("提示：本月为边界 fallback 降级方案，建议人工复核。")

        # ---------------------------------------------------------------------
        # 4.4 保存本月返回结果
        # ---------------------------------------------------------------------
        out_excel = save_month_result(output_dir, opt_month, output_map)
        print(f"第 {opt_month} 个月结果已保存：{out_excel}")

        # ---------------------------------------------------------------------
        # 4.5 汇总本次调用状态
        # ---------------------------------------------------------------------
        call_summary_rows.append(
            {
                "opt_month": opt_month,
                "success": output_map.get("success"),
                "plan_available": output_map.get("plan_available"),
                "dispatch_allowed": output_map.get("dispatch_allowed"),
                "message": output_map.get("message"),
                "exit_reason": output_map.get("exit_reason"),
                "final_status_code": output_map.get("final_status_code"),
                "final_status_name": output_map.get("final_status_name"),
                "final_solve_mode": output_map.get("final_solve_mode"),
                "selected_stage_name": output_map.get("selected_stage_name"),
                "log_file": output_map.get("log_file"),
                "current_month_Dm_MW": output_map.get("current_month_Dm_MW"),
                "current_month_H2_kg": output_map.get("current_month_H2_kg"),
                "current_month_alpha": output_map.get("current_month_alpha"),
                "current_month_CE_kgCO2_per_kgH2": output_map.get("current_month_CE_kgCO2_per_kgH2"),
                "H_done_kg": output_map.get("H_done_kg"),
                "H_original_target_kg": output_map.get("H_original_target_kg"),
                "H_dispatch_target_kg": output_map.get("H_dispatch_target_kg"),
                "H_dispatch_actual_kg": output_map.get("H_dispatch_actual_kg"),
                "min_h2_opt_kg": output_map.get("min_h2_opt_kg"),
                "max_h2_opt_kg": output_map.get("max_h2_opt_kg"),
                "h2_target_relation": output_map.get("h2_target_relation"),
                "H_target_adjusted_flag": output_map.get("H_target_adjusted_flag"),
                "H_target_adjust_reason": output_map.get("H_target_adjust_reason"),
                "fallback_used": output_map.get("fallback_used"),
                "fallback_from_stage": output_map.get("fallback_from_stage"),
                "fallback_to_stage": output_map.get("fallback_to_stage"),
                "fallback_reason": output_map.get("fallback_reason"),
                "problem_code": output_map.get("problem_code"),
                "solver_status": output_map.get("solver_status"),
                "termination_condition": output_map.get("termination_condition"),
                "solve_time_sec": output_map.get("solve_time_sec"),
            }
        )

        # ---------------------------------------------------------------------
        # 4.6 如果本月没有可用结果，停止后续滚动调用
        # ---------------------------------------------------------------------
        if not (output_map.get("success", False) and output_map.get("plan_available", False)):
            print(f"第 {opt_month} 个月没有可下发计划，停止后续月份循环调用。")
            break

        # ---------------------------------------------------------------------
        # 4.7 把当前月计划结果累加为下一轮历史实绩求和值
        # ---------------------------------------------------------------------
        (
            cur_P_h2_sum,
            cur_P_gen_sum,
            cur_P_buy_sum,
            cur_P_sell_sum,
        ) = get_current_month_power_sums_from_output(output_map, opt_month)

        P_h2_actual_MW += cur_P_h2_sum
        P_gen_actual_MW += cur_P_gen_sum
        P_buy_actual_MW += cur_P_buy_sum
        P_sell_actual_MW += cur_P_sell_sum

        actual_sum_rows.append(
            {
                "after_month": opt_month,
                "current_month_P_h2_sum_MW": cur_P_h2_sum,
                "current_month_P_gen_sum_MW": cur_P_gen_sum,
                "current_month_P_buy_sum_MW": cur_P_buy_sum,
                "current_month_P_sell_sum_MW": cur_P_sell_sum,
                "cumulative_P_h2_actual_MW": P_h2_actual_MW,
                "cumulative_P_gen_actual_MW": P_gen_actual_MW,
                "cumulative_P_buy_actual_MW": P_buy_actual_MW,
                "cumulative_P_sell_actual_MW": P_sell_actual_MW,
            }
        )

        idx_cur_month = month_of_hour == opt_month
        expected_hours = int(np.sum(idx_cur_month))
        if expected_hours <= 0:
            raise RuntimeError(f"第 {opt_month} 个月小时数异常。")

        print(f"第 {opt_month} 个月计划已累加为下一轮历史实绩求和值。")
        print(f"当前累计 P_h2_actual_MW 求和值  : {P_h2_actual_MW:.6f}")
        print(f"当前累计 P_gen_actual_MW 求和值 : {P_gen_actual_MW:.6f}")
        print(f"当前累计 P_buy_actual_MW 求和值 : {P_buy_actual_MW:.6f}")
        print(f"当前累计 P_sell_actual_MW 求和值: {P_sell_actual_MW:.6f}")

    # -------------------------------------------------------------------------
    # 5. 保存循环调用总摘要
    # -------------------------------------------------------------------------
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_excel = output_dir / "月度循环调用总摘要_接口版_pyomo.xlsx"

    call_summary = pd.DataFrame(call_summary_rows)
    actual_sum_summary = pd.DataFrame(actual_sum_rows)

    final_actual_sum = pd.DataFrame(
        [
            {
                "final_P_h2_actual_MW": P_h2_actual_MW,
                "final_P_gen_actual_MW": P_gen_actual_MW,
                "final_P_buy_actual_MW": P_buy_actual_MW,
                "final_P_sell_actual_MW": P_sell_actual_MW,
                "note": "以上四个变量均为历史小时功率求和值，不是年度数组。",
            }
        ]
    )

    with pd.ExcelWriter(summary_excel, engine="openpyxl") as writer:
        call_summary.to_excel(writer, sheet_name="call_summary", index=False)
        actual_sum_summary.to_excel(writer, sheet_name="actual_sum_summary", index=False)
        final_actual_sum.to_excel(writer, sheet_name="final_actual_sum", index=False)

    print("\n月度循环调用完成。")
    print(f"循环调用总摘要已保存：{summary_excel}")


if __name__ == "__main__":
    main()
