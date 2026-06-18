from __future__ import annotations

"""
y2m_rolling_optimizer_module_v9.py

风光制氢年度任务拆解 / 月度滚动优化算法模块。

V9 改造要点：
1) 对外只提供 run_y2m_rolling_optimizer(input_map)；
2) input_map 为单层字典，内部不再嵌套 dict；
3) output_map 为单层字典，内部不再嵌套 dict；
4) 对外输入 / 输出涉及数组的变量全部使用 list；
5) 历史实绩 P_h2_actual_MW、P_gen_actual_MW、P_buy_actual_MW、P_sell_actual_MW
   不再传年度数组，而是传“已完成历史小时功率求和值”；
6) 不再返回 pandas.DataFrame；
7) 返回小时级决策 list 和月度级指标 list；
8) 每次调用生成独立日志文件；
9) 经济优化目标函数中支持计入副产氧收益；min_h2 / max_h2 仅用于真实边界诊断，不计入副产氧收益；
10) fixed_h2_target 模式先求 min_h2 / max_h2 真实边界，再判断目标太低、太高或位于边界内；
11) 目标太低/太高时，优先求边界产氢量固定后的经济优化，只有边界经济优化失败才 fallback 到 raw min/max；
12) 删除旧版强预检 output_map 字段，复杂阶段信息进入日志；
13) 内部增加复杂日志开关 _INTERNAL_VERBOSE_STAGE_LOG，以及边界并行开关 _INTERNAL_ENABLE_PARALLEL_BOUNDARY_SOLVE；
14) V9 保留 ProcessPoolExecutor 边界并行机制；默认仍关闭，开启时 min_h2/max_h2 在独立 Python 进程中求解；
15) V9 改进 fixed_h2_target 边界失败判定：单边界可信且足以安全判断目标太高/太低时，仍继续边界经济优化；缺失边界影响区间判断时才不下发；
16) V9 在不改变 input_map/output_map 接口的前提下，根据 P_pre_MW / pi_sell / pi_buy 三个年度数组长度自动识别 8760h 平年或 8784h 闰年，动态生成月份索引；识别结果只写入复杂日志。
"""

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np
from pyomo.environ import (
    Binary,
    ConcreteModel,
    Constraint,
    ConstraintList,
    NonNegativeReals,
    Objective,
    Param,
    RangeSet,
    Reals,
    SolverFactory,
    Var,
    minimize,
    value,
)
from pyomo.opt import SolverStatus, TerminationCondition


# =============================================================================
# 内部算法参数：不作为对外接口暴露
# =============================================================================
_INTERNAL_SOLVER_NAME = "highs"  # 可选："highs" / "gurobi"
_INTERNAL_TEE = False
_INTERNAL_THREADS: Optional[int] = None
_INTERNAL_PRESOLVE = True
_INTERNAL_LOAD_SOLUTIONS = True
_INTERNAL_SYMBOLIC_SOLVER_LABELS = False
_INTERNAL_COMPARE_TOL_KG = 1e-6
_INTERNAL_CO2_QUOTA_YEAR_T_REF: Optional[float] = None

# 是否允许在经济优化模式下，满足价格安全条件时删除购售电互斥二进制变量，转为 LP。
# False：经济优化阶段完全保持原 MILP 建模方式；
# True ：仅 economic_fixed_h2_target / economic_free_h2 在价格条件满足时转 LP；
#        gurobi 求解器的经济优化阶段始终保持原 MILP，不做 LP 化。
_INTERNAL_ENABLE_ECONOMIC_LP_IF_PRICE_SAFE = True

# max_h2 阶段是否强制不售电并删除购售电互斥二进制变量。
# True：max_h2 阶段固定 P_sell[t] = 0，不创建 grid_mode/u[t]，模型从 MILP 简化为 LP；
#       适用于 max_h2 仅用于“最大可产氢量可达性判断”的场景。
# False：回退为旧版 max_h2 建模，保留 P_sell 与购售电互斥二进制变量。
_INTERNAL_FORCE_ZERO_SELL_IN_MAX_H2 = True

# 是否输出复杂阶段日志。
# True：研发调试模式，输出输入统计、预检分解、阶段问题类型、模型规模、目标函数分解、约束余量等。
# False：交付运行模式，只输出调用状态、最终求解模式、当前月核心结果、求解器状态等简洁日志。
# 该参数为内部开关，不进入 input_map/output_map，避免接口变化。
_INTERNAL_VERBOSE_STAGE_LOG = True

# 是否并行求解 min_h2 / max_h2 边界。默认关闭，避免外层并行与求解器内部线程抢资源。
_INTERNAL_ENABLE_PARALLEL_BOUNDARY_SOLVE = False

# 边界并行采用 ProcessPoolExecutor 时，最多并行 min_h2 / max_h2 两个进程。
_INTERNAL_BOUNDARY_PARALLEL_WORKERS = 2

# 边界并行时，给所有边界求解器合计预留的 CPU 逻辑核心比例。
# 每个求解器 threads = floor(os.cpu_count() * 该比例 / workers)，至少为 1。
_INTERNAL_BOUNDARY_PARALLEL_CPU_FRACTION = 0.80

# 价格安全条件容差，单位：元/kWh。
# 判断条件：pi_sell <= pi_buy + carbon_price * EF / 1000 + tol。
_INTERNAL_PRICE_SAFE_TOL_YUAN_PER_KWH = 1e-10

# 是否在日志中启用求解后购售电同时发生检查。
# True ：求解成功后记录 max overlap、累计 overlap 电量、发生小时数；
# False：不执行该检查，也不向日志写入相关内容。
_INTERNAL_ENABLE_BUY_SELL_OVERLAP_LOG_CHECK = True

# 求解后检查同时购售电的功率重叠阈值，单位：MW。
_INTERNAL_BUY_SELL_OVERLAP_TOL_MW = 1e-6


# =============================================================================
# 内部数据类：求解器配置，不暴露给调用方
# =============================================================================
@dataclass
class _SolverConfig:
    solver_name: str = _INTERNAL_SOLVER_NAME
    candidate_factories: Dict[str, list[str]] = field(
        default_factory=lambda: {
            "highs": ["highs", "appsi_highs"],
            "gurobi": ["gurobi"],
        }
    )
    time_limit_sec: Optional[float] = None
    mip_gap: Optional[float] = None
    threads: Optional[int] = _INTERNAL_THREADS
    presolve: Optional[bool] = _INTERNAL_PRESOLVE
    tee: bool = _INTERNAL_TEE
    load_solutions: bool = _INTERNAL_LOAD_SOLUTIONS
    symbolic_solver_labels: bool = _INTERNAL_SYMBOLIC_SOLVER_LABELS
    extra_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _StageRecord:
    """内部阶段记录：只用于复杂日志，不直接进入 output_map。"""

    stage_name: str
    problem_code: int
    solver_status: str
    termination_condition: str
    solve_time_sec: float
    h2_kg: Optional[float]
    selected: bool = False
    note: str = ""


@dataclass(frozen=True)
class _YearCalendarInfo:
    """内部年度日历信息：只由年度数组长度推导，不进入对外接口。"""

    year_hours: int
    calendar_type: str
    is_leap_year_data: bool
    days_per_month: np.ndarray
    hours_per_month: np.ndarray


# =============================================================================
# 对外主函数
# =============================================================================
def run_y2m_rolling_optimizer(input_map: dict[str, Any]) -> dict[str, Any]:
    """
    风光制氢年度任务拆解 / 月度滚动优化算法函数。

    V9 主流程：
    1) fixed_h2_target：先求 min_h2 / max_h2 真实边界；
       再根据 H_year - H_done 的 raw 剩余目标判断目标太低、太高或位于边界内；
       最终优先下发固定 H_dispatch_target_kg 下的经济优化结果。
    2) free_h2：先求 economic_free_h2；失败后仅求 min/max 边界诊断，不下发边界解。
    3) output_map 保持精简；复杂阶段细节写入日志。
    """

    logger: Optional[logging.Logger] = None
    log_file = ""

    try:
        if not isinstance(input_map, dict):
            raise ValueError("input_map 必须为 dict。")
        for key, val in input_map.items():
            if isinstance(val, dict):
                raise ValueError(f"input_map 内部不允许嵌套 dict，字段 {key} 当前为 dict。")

        logger, log_file = _setup_logger(input_map.get("log_dir", None))
        _log_basic(logger, "============================================================")
        _log_basic(logger, "[Y2M V9] 开始调用 run_y2m_rolling_optimizer")
        _log_basic(logger, "日志模式：%s", "verbose" if _INTERNAL_VERBOSE_STAGE_LOG else "simple")
        _log_basic(logger, "边界并行：%s", _INTERNAL_ENABLE_PARALLEL_BOUNDARY_SOLVE)
        if _INTERNAL_ENABLE_PARALLEL_BOUNDARY_SOLVE:
            _log_basic(logger, "边界并行后端：ProcessPoolExecutor；每个子进程独立创建 Pyomo 模型与求解器。")
        _log_basic(logger, "日志文件：%s", log_file)
        _log_basic(logger, "============================================================")

        # ------------------------------------------------------------------
        # 1. 输入检查与基础整理
        # ------------------------------------------------------------------
        opt_month = int(_get_required_scalar(input_map, "opt_month"))
        if opt_month < 1 or opt_month > 12:
            raise ValueError("opt_month 必须为 1~12 的整数。")

        mode = str(_get_required(input_map, "economic_dispatch_mode")).lower().strip()
        if mode not in {"fixed_h2_target", "free_h2"}:
            raise ValueError("economic_dispatch_mode 只能为 'fixed_h2_target' 或 'free_h2'。")

        dt_h = _get_required_scalar(input_map, "dt_h")
        if dt_h <= 0:
            raise ValueError("dt_h 必须大于 0。")

        P_pre = _to_year_hour_array(_get_required(input_map, "P_pre_MW"), "P_pre_MW")
        pi_sell = _to_year_hour_array(_get_required(input_map, "pi_sell_yuan_per_kWh"), "pi_sell_yuan_per_kWh")
        pi_buy = _to_year_hour_array(_get_required(input_map, "pi_buy_yuan_per_kWh"), "pi_buy_yuan_per_kWh")
        year_calendar = _detect_year_calendar_from_arrays(P_pre, pi_sell, pi_buy)
        if np.any(P_pre < 0):
            raise ValueError("P_pre_MW 不允许为负。")
        if np.any(pi_sell < 0):
            raise ValueError("pi_sell_yuan_per_kWh 不允许为负。")
        if np.any(pi_buy < 0):
            raise ValueError("pi_buy_yuan_per_kWh 不允许为负。")

        P_h2_actual_sum_MW = _get_required_scalar(input_map, "P_h2_actual_MW")
        P_gen_actual_sum_MW = _get_required_scalar(input_map, "P_gen_actual_MW")
        P_buy_actual_sum_MW = _get_required_scalar(input_map, "P_buy_actual_MW")
        P_sell_actual_sum_MW = _get_required_scalar(input_map, "P_sell_actual_MW")
        for name, val in {
            "P_h2_actual_MW": P_h2_actual_sum_MW,
            "P_gen_actual_MW": P_gen_actual_sum_MW,
            "P_buy_actual_MW": P_buy_actual_sum_MW,
            "P_sell_actual_MW": P_sell_actual_sum_MW,
        }.items():
            if val < 0:
                raise ValueError(f"{name} 为历史小时功率求和值，不允许为负。")

        H_year_kg = _get_required_scalar(input_map, "H_year_kg")
        alpha_year = _get_required_scalar(input_map, "alpha_year")
        k_H_kWh_per_Nm3 = _get_required_scalar(input_map, "k_H_kWh_per_Nm3")
        rho_H2_kg_per_Nm3 = _get_required_scalar(input_map, "rho_H2_kg_per_Nm3")
        P_h2_min_MW = _get_required_scalar(input_map, "P_h2_min_MW")
        P_h2_max_MW = _get_required_scalar(input_map, "P_h2_max_MW")
        P_sell_max_MW = _get_required_scalar(input_map, "P_sell_max_MW")
        P_buy_max_MW = _get_required_scalar(input_map, "P_buy_max_MW")
        Dm_max_MW = _to_12_array(_get_required(input_map, "Dm_max_MW"), "Dm_max_MW")
        C_cap_month_yuan_per_kW_month = _to_12_array(
            _get_required(input_map, "C_cap_month_yuan_per_kW_month"),
            "C_cap_month_yuan_per_kW_month",
        )
        EF_tCO2_per_MWh = _get_required_scalar(input_map, "EF_tCO2_per_MWh")
        CE_limit_kgCO2_per_kgH2 = _get_required_scalar(input_map, "CE_limit_kgCO2_per_kgH2")
        H2_price_yuan_per_kg = _get_required_scalar(input_map, "H2_price_yuan_per_kg")
        carbon_price_yuan_per_tCO2 = _get_required_scalar(input_map, "carbon_price_yuan_per_tCO2")
        O2_byproduct_Nm3_per_Nm3_H2 = _get_required_scalar(input_map, "O2_byproduct_Nm3_per_Nm3_H2")
        O2_price_yuan_per_Nm3 = _get_required_scalar(input_map, "O2_price_yuan_per_Nm3")

        time_limit_sec = _get_optional_scalar(input_map, "time_limit_sec", None)
        mip_gap = _get_optional_scalar(input_map, "mip_gap", None)
        solver_cfg = _build_internal_solver_config(time_limit_sec=time_limit_sec, mip_gap=mip_gap)

        if H_year_kg < 0:
            raise ValueError("H_year_kg 不允许为负。")
        if not (0 <= alpha_year <= 1):
            raise ValueError("alpha_year 必须位于 0~1。")
        if k_H_kWh_per_Nm3 <= 0:
            raise ValueError("k_H_kWh_per_Nm3 必须大于 0。")
        if rho_H2_kg_per_Nm3 <= 0:
            raise ValueError("rho_H2_kg_per_Nm3 必须大于 0。")
        if P_h2_min_MW < 0:
            raise ValueError("P_h2_min_MW 不允许为负。")
        if P_h2_max_MW < P_h2_min_MW:
            raise ValueError("P_h2_max_MW 必须大于等于 P_h2_min_MW。")
        if P_sell_max_MW < 0 or P_buy_max_MW < 0:
            raise ValueError("P_sell_max_MW / P_buy_max_MW 不允许为负。")
        if np.any(Dm_max_MW < 0):
            raise ValueError("Dm_max_MW 不允许为负。")
        if np.any(C_cap_month_yuan_per_kW_month < 0):
            raise ValueError("C_cap_month_yuan_per_kW_month 不允许为负。")
        if EF_tCO2_per_MWh <= 0:
            raise ValueError("EF_tCO2_per_MWh 必须大于 0。")
        if CE_limit_kgCO2_per_kgH2 < 0:
            raise ValueError("CE_limit_kgCO2_per_kgH2 不允许为负。")
        if O2_byproduct_Nm3_per_Nm3_H2 < 0 or O2_price_yuan_per_Nm3 < 0:
            raise ValueError("O2_byproduct_Nm3_per_Nm3_H2 / O2_price_yuan_per_Nm3 不允许为负。")

        days_per_month = year_calendar.days_per_month.copy()
        hours_per_month = year_calendar.hours_per_month.copy()
        month_of_t = np.repeat(np.arange(1, 13, dtype=int), hours_per_month)
        if month_of_t.size != year_calendar.year_hours:
            raise RuntimeError(
                f"内部月份索引构造错误：month_of_t长度={month_of_t.size}，"
                f"year_hours={year_calendar.year_hours}。"
            )

        h2_kg_per_mwh = 1000.0 / k_H_kWh_per_Nm3 * rho_H2_kg_per_Nm3
        CO2_quota_year_t_ref = (
            CE_limit_kgCO2_per_kgH2 * H_year_kg / 1000.0
            if _INTERNAL_CO2_QUOTA_YEAR_T_REF is None
            else float(_INTERNAL_CO2_QUOTA_YEAR_T_REF)
        )

        M_done = opt_month - 1
        idx_rem = month_of_t >= opt_month
        T_rem = int(np.sum(idx_rem))
        rem_month_ids = np.arange(opt_month, 13, dtype=int)
        n_rem_months = int(rem_month_ids.size)
        month_of_t_rem_abs = month_of_t[idx_rem]
        month_of_t_rem_local = month_of_t_rem_abs - M_done
        P_pre_rem = P_pre[idx_rem]
        pi_sell_rem = pi_sell[idx_rem]
        pi_buy_rem = pi_buy[idx_rem]
        C_cap_month_rem = C_cap_month_yuan_per_kW_month[rem_month_ids - 1]
        Dm_max_rem = Dm_max_MW[rem_month_ids - 1]

        H_done_kg = float(P_h2_actual_sum_MW * dt_h * h2_kg_per_mwh)
        E_gen_done_mwh = float(P_gen_actual_sum_MW * dt_h)
        E_sell_done_mwh = float(P_sell_actual_sum_MW * dt_h)
        E_buy_done_mwh = float(P_buy_actual_sum_MW * dt_h)
        CO2_done_kg = E_buy_done_mwh * EF_tCO2_per_MWh * 1000.0

        H_original_target_kg = float(H_year_kg - H_done_kg) if mode == "fixed_h2_target" else None
        H_target_constraint_active = 1 if mode == "fixed_h2_target" else 0

        _log_basic(
            logger,
            "[Y2M V9] 参数：opt_month=%s, mode=%s, T_rem=%s, solver=%s, time_limit_sec=%s, mip_gap=%s",
            opt_month, mode, T_rem, solver_cfg.solver_name, time_limit_sec, mip_gap,
        )
        _log_year_calendar_verbose(logger, year_calendar, opt_month, T_rem, rem_month_ids)
        _log_verbose(logger, "H_done_kg = %.9f", H_done_kg)
        _log_verbose(logger, "H_original_target_kg(raw) = %s", _fmt_value(H_original_target_kg, 9))
        _log_input_and_conversion_verbose(
            logger, P_pre, pi_sell, pi_buy, H_year_kg, alpha_year, k_H_kWh_per_Nm3,
            rho_H2_kg_per_Nm3, P_h2_min_MW, P_h2_max_MW, P_buy_max_MW, P_sell_max_MW,
            Dm_max_MW, C_cap_month_yuan_per_kW_month, EF_tCO2_per_MWh,
            CE_limit_kgCO2_per_kgH2, H2_price_yuan_per_kg, carbon_price_yuan_per_tCO2,
            O2_byproduct_Nm3_per_Nm3_H2, O2_price_yuan_per_Nm3, h2_kg_per_mwh,
            O2_price_yuan_per_Nm3 * O2_byproduct_Nm3_per_Nm3_H2 / rho_H2_kg_per_Nm3,
            O2_price_yuan_per_Nm3 * O2_byproduct_Nm3_per_Nm3_H2 * 1000.0 / k_H_kWh_per_Nm3,
            CE_limit_kgCO2_per_kgH2 / (EF_tCO2_per_MWh * 1000.0),
            1.0 - h2_kg_per_mwh * CE_limit_kgCO2_per_kgH2 / (EF_tCO2_per_MWh * 1000.0),
            CO2_quota_year_t_ref,
        )

        # ------------------------------------------------------------------
        # 2. 通用求解上下文
        # ------------------------------------------------------------------
        common_args = dict(
            P_pre_rem=P_pre_rem,
            pi_sell_rem=pi_sell_rem,
            pi_buy_rem=pi_buy_rem,
            month_of_t_rem_local=month_of_t_rem_local,
            dt_h=dt_h,
            C_cap_month_rem=C_cap_month_rem,
            k_H_kWh_per_Nm3=k_H_kWh_per_Nm3,
            alpha_year=alpha_year,
            CE_limit_kgCO2_per_kgH2=CE_limit_kgCO2_per_kgH2,
            P_h2_min_MW=P_h2_min_MW,
            P_h2_max_MW=P_h2_max_MW,
            EF_tCO2_per_MWh=EF_tCO2_per_MWh,
            rho_H2_kg_per_Nm3=rho_H2_kg_per_Nm3,
            P_sell_max_MW=P_sell_max_MW,
            P_buy_max_MW=P_buy_max_MW,
            Dm_max_rem=Dm_max_rem,
            H_done_kg=H_done_kg,
            E_gen_done_mwh=E_gen_done_mwh,
            E_sell_done_mwh=E_sell_done_mwh,
            CO2_done_kg=CO2_done_kg,
            H2_price_yuan_per_kg=H2_price_yuan_per_kg,
            carbon_price_yuan_per_tCO2=carbon_price_yuan_per_tCO2,
            O2_byproduct_Nm3_per_Nm3_H2=O2_byproduct_Nm3_per_Nm3_H2,
            O2_price_yuan_per_Nm3=O2_price_yuan_per_Nm3,
            solver_cfg=solver_cfg,
            logger=logger,
        )

        stage_records: list[_StageRecord] = []
        solve_time_total = 0.0

        def solve_stage(stage_name: str, H_target: Optional[float] = None, init_res: Optional[dict[str, Any]] = None, logger_for_stage: Optional[logging.Logger] = logger) -> dict[str, Any]:
            if init_res is not None and _stage_ok(init_res):
                P_h2_0 = np.asarray(init_res["P_h2"], dtype=float).copy()
                P_gen_0 = np.asarray(init_res["P_gen"], dtype=float).copy()
                P_sell_0 = np.asarray(init_res["P_sell"], dtype=float).copy()
                P_buy_0 = np.asarray(init_res["P_buy"], dtype=float).copy()
                Dm_0 = np.asarray(init_res["Dm"], dtype=float).copy()
            else:
                P_h2_0, P_gen_0, P_sell_0, P_buy_0, Dm_0 = _make_initial_guess(
                    P_pre_rem, month_of_t_rem_local, n_rem_months, dt_h, h2_kg_per_mwh,
                    P_h2_min_MW, P_h2_max_MW, P_buy_max_MW, H_target,
                )

            return _solve_pyomo_model_roll_profit(
                common_args["P_pre_rem"], common_args["pi_sell_rem"], common_args["pi_buy_rem"],
                common_args["month_of_t_rem_local"], common_args["dt_h"], common_args["C_cap_month_rem"],
                H_target,
                common_args["k_H_kWh_per_Nm3"], common_args["alpha_year"], common_args["CE_limit_kgCO2_per_kgH2"],
                common_args["P_h2_min_MW"], common_args["P_h2_max_MW"], common_args["EF_tCO2_per_MWh"],
                common_args["rho_H2_kg_per_Nm3"], common_args["P_sell_max_MW"], common_args["P_buy_max_MW"],
                common_args["Dm_max_rem"], common_args["H_done_kg"], common_args["E_gen_done_mwh"],
                common_args["E_sell_done_mwh"], common_args["CO2_done_kg"], common_args["H2_price_yuan_per_kg"],
                common_args["carbon_price_yuan_per_tCO2"], common_args["O2_byproduct_Nm3_per_Nm3_H2"],
                common_args["O2_price_yuan_per_Nm3"],
                P_h2_0, P_gen_0, P_sell_0, P_buy_0, Dm_0,
                common_args["solver_cfg"], stage_name, logger=logger_for_stage,
            )

        def add_record(stage_name: str, res: Optional[dict[str, Any]], selected: bool = False, note: str = "") -> None:
            if res is None:
                rec = _StageRecord(stage_name, 9, "not_solved", "not_solved", 0.0, None, selected, note)
            else:
                diag = res.get("diagnostics", {})
                rec = _StageRecord(
                    stage_name=stage_name,
                    problem_code=int(diag.get("problem", 9)),
                    solver_status=str(diag.get("solver_status", "")),
                    termination_condition=str(diag.get("termination_condition", "")),
                    solve_time_sec=float(diag.get("stage_total_time_sec", res.get("solve_time", 0.0))),
                    h2_kg=_clean_float(_stage_h2_kg(res, dt_h, h2_kg_per_mwh)),
                    selected=selected,
                    note=note,
                )
            stage_records.append(rec)

        # ------------------------------------------------------------------
        # 3. 初始化输出状态
        # ------------------------------------------------------------------
        selected_res: Optional[dict[str, Any]] = None
        selected_stage_name = ""
        final_status_code = -1
        final_status_name = "all_solver_failed_no_output"
        final_solve_mode = "no_valid_solution"
        exit_reason = "not_solved"
        message = "未得到可下发计划。"
        success = False
        plan_available = False
        fallback_used = False
        fallback_from_stage = ""
        fallback_to_stage = ""
        fallback_reason = ""
        h2_target_relation = "not_used_in_free_h2" if mode == "free_h2" else "not_evaluated"
        H_dispatch_target_kg: Optional[float] = None
        H_dispatch_actual_kg: Optional[float] = None
        H_target_adjusted_flag = False
        H_target_adjust_reason = "not_adjusted"
        min_h2_opt_kg: Optional[float] = None
        max_h2_opt_kg: Optional[float] = None
        h2_compare_tol_kg: Optional[float] = None

        # ------------------------------------------------------------------
        # 4. 分模式求解
        # ------------------------------------------------------------------
        if mode == "fixed_h2_target":
            _log_basic(logger, "[Y2M V9] fixed_h2_target：开始求真实 min_h2 / max_h2 边界。")
            if _INTERNAL_ENABLE_PARALLEL_BOUNDARY_SOLVE:
                boundary_workers = _INTERNAL_BOUNDARY_PARALLEL_WORKERS
                boundary_cpu_count = os.cpu_count() or 1
                boundary_threads = _compute_boundary_threads_per_solver(boundary_workers)
                _log_basic(
                    logger,
                    "[Y2M V9] 边界并行后端：ProcessPoolExecutor, workers=%s, cpu_count=%s, threads_per_solver=%s",
                    boundary_workers, boundary_cpu_count, boundary_threads,
                )
                worker_solver_cfg = _copy_solver_config_with_threads(solver_cfg, boundary_threads, force_tee_false=True)
                min_payload = _make_boundary_worker_payload(common_args, worker_solver_cfg, "min_h2", n_rem_months, h2_kg_per_mwh)
                max_payload = _make_boundary_worker_payload(common_args, worker_solver_cfg, "max_h2", n_rem_months, h2_kg_per_mwh)
                try:
                    with ProcessPoolExecutor(max_workers=boundary_workers) as executor:
                        fut_min = executor.submit(_boundary_solve_worker, min_payload)
                        fut_max = executor.submit(_boundary_solve_worker, max_payload)
                        min_res = fut_min.result()
                        max_res = fut_max.result()
                    add_record("min_h2", min_res, note=f"process boundary; threads_per_solver={boundary_threads}")
                    add_record("max_h2", max_res, note=f"process boundary; threads_per_solver={boundary_threads}")
                except Exception as exc:
                    _log_basic(
                        logger,
                        "[Y2M V9] 边界进程并行失败，自动回退为串行边界求解。原因：%s",
                        exc,
                    )
                    min_res = solve_stage("min_h2")
                    add_record("min_h2", min_res, note=f"process boundary failed; serial fallback; reason={exc}")
                    max_res = solve_stage("max_h2")
                    add_record("max_h2", max_res, note=f"process boundary failed; serial fallback; reason={exc}")
            else:
                min_res = solve_stage("min_h2")
                add_record("min_h2", min_res)
                max_res = solve_stage("max_h2")
                add_record("max_h2", max_res)

            solve_time_total += _stage_total_time(min_res) + _stage_total_time(max_res)

            min_ok = _stage_ok(min_res)
            max_ok = _stage_ok(max_res)
            if min_ok:
                min_h2_opt_kg = _stage_h2_kg(min_res, dt_h, h2_kg_per_mwh)
            if max_ok:
                max_h2_opt_kg = _stage_h2_kg(max_res, dt_h, h2_kg_per_mwh)

            assert H_original_target_kg is not None
            h2_compare_tol_kg = _h2_compare_tol(
                H_original_target_kg,
                min_h2_opt_kg if min_ok else None,
                max_h2_opt_kg if max_ok else None,
            )

            econ_stage: Optional[str] = None
            init_res: Optional[dict[str, Any]] = None
            boundary_decision_mode = "not_evaluated"

            if min_ok and max_ok:
                boundary_decision_mode = "full_bounds_available"
                if min_h2_opt_kg is None or max_h2_opt_kg is None or min_h2_opt_kg > max_h2_opt_kg + h2_compare_tol_kg:
                    boundary_decision_mode = "bounds_inconsistent"
                    final_status_code = -3
                    final_status_name = "base_model_infeasible_or_inconsistent_no_output"
                    exit_reason = "boundary_inconsistent"
                    h2_target_relation = "unknown_due_to_bounds_inconsistent"
                    message = "fixed_h2_target 模式下真实产氢边界不一致或基础模型不可行，未输出计划。"
                elif H_original_target_kg < min_h2_opt_kg - h2_compare_tol_kg:
                    h2_target_relation = "target_lt_true_min_h2"
                    H_dispatch_target_kg = min_h2_opt_kg
                    H_target_adjusted_flag = True
                    if H_original_target_kg < 0:
                        H_target_adjust_reason = "history_h2_already_exceeds_annual_target"
                    else:
                        H_target_adjust_reason = "target_below_true_min_h2"
                    econ_stage = "economic_fixed_min_boundary"
                    init_res = min_res
                elif H_original_target_kg > max_h2_opt_kg + h2_compare_tol_kg:
                    h2_target_relation = "target_gt_true_max_h2"
                    H_dispatch_target_kg = max_h2_opt_kg
                    H_target_adjusted_flag = True
                    H_target_adjust_reason = "target_above_true_max_h2"
                    econ_stage = "economic_fixed_max_boundary"
                    init_res = max_res
                else:
                    h2_target_relation = "target_inside_true_h2_bounds"
                    H_dispatch_target_kg = H_original_target_kg
                    H_target_adjusted_flag = False
                    H_target_adjust_reason = "not_adjusted"
                    econ_stage = "economic_fixed_h2_target"
                    init_res = _mix_boundary_solution(min_res, max_res, H_dispatch_target_kg, min_h2_opt_kg, max_h2_opt_kg)

            elif (not min_ok) and max_ok:
                if max_h2_opt_kg is not None and H_original_target_kg > max_h2_opt_kg + h2_compare_tol_kg:
                    boundary_decision_mode = "only_max_available_target_above_max"
                    h2_target_relation = "target_gt_true_max_h2"
                    H_dispatch_target_kg = max_h2_opt_kg
                    H_target_adjusted_flag = True
                    H_target_adjust_reason = "target_above_true_max_h2_min_h2_failed"
                    econ_stage = "economic_fixed_max_boundary"
                    init_res = max_res
                else:
                    boundary_decision_mode = "only_max_available_no_safe_decision"
                    final_status_code = -2
                    final_status_name = "boundary_solve_incomplete_no_safe_decision"
                    exit_reason = "min_h2_failed_target_not_above_max"
                    h2_target_relation = "unknown_due_to_min_h2_failed"
                    message = "min_h2 边界失败，max_h2 边界成功，但目标未明显高于 max_h2；缺失 min_h2 会影响目标区间判断，未输出计划。"

            elif min_ok and (not max_ok):
                if min_h2_opt_kg is not None and H_original_target_kg < min_h2_opt_kg - h2_compare_tol_kg:
                    boundary_decision_mode = "only_min_available_target_below_min"
                    h2_target_relation = "target_lt_true_min_h2"
                    H_dispatch_target_kg = min_h2_opt_kg
                    H_target_adjusted_flag = True
                    if H_original_target_kg < 0:
                        H_target_adjust_reason = "history_h2_already_exceeds_annual_target_max_h2_failed"
                    else:
                        H_target_adjust_reason = "target_below_true_min_h2_max_h2_failed"
                    econ_stage = "economic_fixed_min_boundary"
                    init_res = min_res
                else:
                    boundary_decision_mode = "only_min_available_no_safe_decision"
                    final_status_code = -2
                    final_status_name = "boundary_solve_incomplete_no_safe_decision"
                    exit_reason = "max_h2_failed_target_not_below_min"
                    h2_target_relation = "unknown_due_to_max_h2_failed"
                    message = "max_h2 边界失败，min_h2 边界成功，但目标未明显低于 min_h2；缺失 max_h2 会影响目标区间判断，未输出计划。"

            else:
                boundary_decision_mode = "all_bounds_failed"
                final_status_code = -3
                final_status_name = "base_model_boundary_all_failed_no_output"
                exit_reason = "all_boundary_solve_failed"
                h2_target_relation = "unknown_due_to_all_boundary_failed"
                message = "fixed_h2_target 模式下 min_h2 和 max_h2 边界均未得到可靠解，未输出计划。"

            _log_basic(
                logger,
                "[Y2M V9] 边界判定：min_ok=%s, max_ok=%s, decision_mode=%s, H_original=%s, H_min=%s, H_max=%s, relation=%s, H_dispatch=%s, tol=%s",
                min_ok, max_ok, boundary_decision_mode,
                _fmt_value(H_original_target_kg, 6), _fmt_value(min_h2_opt_kg, 6),
                _fmt_value(max_h2_opt_kg, 6), h2_target_relation,
                _fmt_value(H_dispatch_target_kg, 6), _fmt_value(h2_compare_tol_kg, 6),
            )
            _log_verbose(logger, "H_target_adjusted_flag = %s", H_target_adjusted_flag)
            _log_verbose(logger, "H_target_adjust_reason = %s", H_target_adjust_reason)

            if econ_stage is not None and H_dispatch_target_kg is not None and init_res is not None:
                eco_res = solve_stage(econ_stage, H_dispatch_target_kg, init_res)
                add_record(econ_stage, eco_res)
                solve_time_total += _stage_total_time(eco_res)

                if _stage_ok(eco_res):
                    selected_res = eco_res
                    selected_stage_name = econ_stage
                    success = True
                    plan_available = True
                    final_solve_mode = econ_stage
                    exit_reason = "solved"
                    H_dispatch_actual_kg = _stage_h2_kg(eco_res, dt_h, h2_kg_per_mwh)
                    if econ_stage == "economic_fixed_min_boundary":
                        final_status_code = 2
                        final_status_name = "economic_fixed_min_boundary_success"
                        if boundary_decision_mode == "only_min_available_target_below_min":
                            message = "max_h2 边界失败但 min_h2 可信，目标明显低于 min_h2；已修正到 min 边界并完成边界经济优化。"
                        else:
                            message = "目标低于真实最小可行产氢量，已修正到 min 边界并完成边界经济优化。"
                    elif econ_stage == "economic_fixed_max_boundary":
                        final_status_code = 3
                        final_status_name = "economic_fixed_max_boundary_success"
                        if boundary_decision_mode == "only_max_available_target_above_max":
                            message = "min_h2 边界失败但 max_h2 可信，目标明显高于 max_h2；已修正到 max 边界并完成边界经济优化。"
                        else:
                            message = "目标高于真实最大可行产氢量，已修正到 max 边界并完成边界经济优化。"
                    else:
                        final_status_code = 1
                        final_status_name = "economic_fixed_target_success"
                        message = "目标位于真实可行边界内，固定原目标经济优化成功。"

                elif econ_stage == "economic_fixed_min_boundary":
                    selected_res = min_res
                    selected_stage_name = "min_h2"
                    success = True
                    plan_available = True
                    fallback_used = True
                    fallback_from_stage = econ_stage
                    fallback_to_stage = "min_h2"
                    fallback_reason = "min_boundary_economic_failed"
                    final_solve_mode = "raw_min_h2_fallback"
                    final_status_code = 11
                    final_status_name = "fallback_to_min_h2_after_min_boundary_economic_fail"
                    exit_reason = "fallback_to_min_h2"
                    if boundary_decision_mode == "only_min_available_target_below_min":
                        message = "max_h2 边界失败但 min_h2 可信，目标明显低于 min_h2；min 边界经济优化失败，已 fallback 到原始 min_h2 可行边界方案。"
                    else:
                        message = "目标低于真实下界；min 边界经济优化失败，已 fallback 到原始 min_h2 可行边界方案。"
                    H_dispatch_actual_kg = min_h2_opt_kg

                elif econ_stage == "economic_fixed_max_boundary":
                    selected_res = max_res
                    selected_stage_name = "max_h2"
                    success = True
                    plan_available = True
                    fallback_used = True
                    fallback_from_stage = econ_stage
                    fallback_to_stage = "max_h2"
                    fallback_reason = "max_boundary_economic_failed"
                    final_solve_mode = "raw_max_h2_fallback"
                    final_status_code = 12
                    final_status_name = "fallback_to_max_h2_after_max_boundary_economic_fail"
                    exit_reason = "fallback_to_max_h2"
                    if boundary_decision_mode == "only_max_available_target_above_max":
                        message = "min_h2 边界失败但 max_h2 可信，目标明显高于 max_h2；max 边界经济优化失败，已 fallback 到原始 max_h2 可行边界方案。"
                    else:
                        message = "目标高于真实上界；max 边界经济优化失败，已 fallback 到原始 max_h2 可行边界方案。"
                    H_dispatch_actual_kg = max_h2_opt_kg

                else:
                    repair_res = solve_stage("feasible_fixed_h2_target", H_dispatch_target_kg, init_res)
                    add_record("feasible_fixed_h2_target", repair_res)
                    solve_time_total += _stage_total_time(repair_res)
                    if _stage_ok(repair_res):
                        selected_res = repair_res
                        selected_stage_name = "feasible_fixed_h2_target"
                        success = True
                        plan_available = True
                        final_solve_mode = "feasible_fixed_h2_target"
                        final_status_code = 5
                        final_status_name = "feasible_fixed_target_success"
                        exit_reason = "economic_failed_feasible_repair_solved"
                        message = "固定原目标经济优化失败，但固定目标可行性修复成功。"
                        H_dispatch_actual_kg = _stage_h2_kg(repair_res, dt_h, h2_kg_per_mwh)
                    else:
                        final_status_code = -4
                        final_status_name = "economic_fixed_failed_no_output"
                        final_solve_mode = "no_valid_solution"
                        exit_reason = "economic_and_repair_failed"
                        message = "目标位于真实边界内，但固定目标经济优化和可行性修复均失败，未输出计划。"
        else:
            _log_basic(logger, "[Y2M V9] free_h2：先求自由经济优化。")
            eco_res = solve_stage("economic_free_h2")
            add_record("economic_free_h2", eco_res)
            solve_time_total += _stage_total_time(eco_res)

            if _stage_ok(eco_res):
                selected_res = eco_res
                selected_stage_name = "economic_free_h2"
                success = True
                plan_available = True
                final_solve_mode = "economic_free_h2"
                final_status_code = 4
                final_status_name = "economic_free_success"
                exit_reason = "solved"
                message = "自由型经济优化求解成功。"
                H_dispatch_actual_kg = _stage_h2_kg(eco_res, dt_h, h2_kg_per_mwh)
            else:
                _log_basic(logger, "[Y2M V9] free_h2 经济优化失败，开始 min/max 边界诊断；诊断结果不下发。")
                min_res = solve_stage("min_h2")
                add_record("min_h2", min_res, note="diagnostic_only")
                max_res = solve_stage("max_h2")
                add_record("max_h2", max_res, note="diagnostic_only")
                solve_time_total += _stage_total_time(min_res) + _stage_total_time(max_res)
                if _stage_ok(min_res):
                    min_h2_opt_kg = _stage_h2_kg(min_res, dt_h, h2_kg_per_mwh)
                if _stage_ok(max_res):
                    max_h2_opt_kg = _stage_h2_kg(max_res, dt_h, h2_kg_per_mwh)

                if _stage_ok(min_res) and _stage_ok(max_res):
                    final_status_code = -5
                    final_status_name = "economic_free_failed_boundary_diagnostic_only"
                    exit_reason = "economic_free_failed_boundary_diagnostic_only"
                    message = "自由经济优化失败；min/max 边界诊断成功，但边界解不作为 free_h2 下发方案。"
                else:
                    final_status_code = -6
                    final_status_name = "economic_free_failed_and_boundary_failed"
                    exit_reason = "economic_free_and_boundary_failed"
                    message = "自由经济优化失败，且 min/max 边界诊断也失败，未输出计划。"

        # 标记选中阶段
        for rec in stage_records:
            if selected_stage_name and rec.stage_name == selected_stage_name:
                rec.selected = True

        # 提取最终计划数组与诊断
        if selected_res is not None and plan_available:
            P_h2_plan_rem = selected_res["P_h2"]
            P_gen_plan_rem = selected_res["P_gen"]
            P_buy_plan_rem = selected_res["P_buy"]
            P_sell_plan_rem = selected_res["P_sell"]
            Dm_plan_rem = selected_res["Dm"]
            selected_diag = _diagnostics_to_public(selected_res.get("diagnostics", {}), solve_time_total)
        else:
            P_h2_plan_rem = np.full(T_rem, np.nan, dtype=float)
            P_gen_plan_rem = np.full(T_rem, np.nan, dtype=float)
            P_buy_plan_rem = np.full(T_rem, np.nan, dtype=float)
            P_sell_plan_rem = np.full(T_rem, np.nan, dtype=float)
            Dm_plan_rem = np.full(n_rem_months, np.nan, dtype=float)
            selected_diag = {
                "problem_code": 1 if final_status_code != -9 else 2,
                "solver_status": "no_selected_solution",
                "termination_condition": exit_reason,
                "solve_time_sec": float(solve_time_total),
            }

        # 若下发目标实际值未填，用最终方案计算一次
        if plan_available and H_dispatch_actual_kg is None:
            H_dispatch_actual_kg = _stage_h2_kg({"P_h2": P_h2_plan_rem}, dt_h, h2_kg_per_mwh)

        output_map = _build_output_map(
            success=success,
            message=message,
            exit_reason=exit_reason,
            log_file=log_file,
            plan_available=plan_available,
            final_status_code=final_status_code,
            final_status_name=final_status_name,
            final_solve_mode=final_solve_mode,
            selected_stage_name=selected_stage_name,
            idx_rem=idx_rem,
            month_of_t=month_of_t,
            month_of_t_rem_abs=month_of_t_rem_abs,
            rem_month_ids=rem_month_ids,
            dt_h=dt_h,
            h2_kg_per_mwh=h2_kg_per_mwh,
            EF_tCO2_per_MWh=EF_tCO2_per_MWh,
            P_gen_plan_rem=P_gen_plan_rem,
            P_h2_plan_rem=P_h2_plan_rem,
            P_buy_plan_rem=P_buy_plan_rem,
            P_sell_plan_rem=P_sell_plan_rem,
            Dm_plan_rem=Dm_plan_rem,
            H_done_kg=H_done_kg,
            H_rem_target_kg=H_original_target_kg if mode == "fixed_h2_target" else None,
            H_original_target_kg=H_original_target_kg,
            H_dispatch_target_kg=H_dispatch_target_kg,
            H_dispatch_actual_kg=H_dispatch_actual_kg,
            min_h2_opt_kg=min_h2_opt_kg,
            max_h2_opt_kg=max_h2_opt_kg,
            h2_target_relation=h2_target_relation,
            H_target_adjusted_flag=H_target_adjusted_flag,
            H_target_adjust_reason=H_target_adjust_reason,
            h2_compare_tol_kg=h2_compare_tol_kg,
            fallback_used=fallback_used,
            fallback_from_stage=fallback_from_stage,
            fallback_to_stage=fallback_to_stage,
            fallback_reason=fallback_reason,
            H_target_constraint_active=H_target_constraint_active,
            diagnostics=selected_diag,
        )

        _log_stage_records(logger, stage_records)
        _log_basic(
            logger,
            "[Y2M V9] status: success=%s, final_status_code=%s, final_status_name=%s, final_solve_mode=%s, exit_reason=%s",
            output_map["success"], output_map["final_status_code"], output_map["final_status_name"],
            output_map["final_solve_mode"], output_map["exit_reason"],
        )
        _log_basic(logger, "[Y2M V9] message: %s", output_map["message"])
        _log_basic(
            logger,
            "[Y2M V9] target: H_original=%s, H_min=%s, H_max=%s, H_dispatch_target=%s, H_dispatch_actual=%s, relation=%s",
            output_map["H_original_target_kg"], output_map["min_h2_opt_kg"], output_map["max_h2_opt_kg"],
            output_map["H_dispatch_target_kg"], output_map["H_dispatch_actual_kg"], output_map["h2_target_relation"],
        )
        _log_basic(
            logger,
            "[Y2M V9] current_month_result: H2_kg=%s, Dm_MW=%s, alpha=%s, CE=%s",
            output_map["current_month_H2_kg"], output_map["current_month_Dm_MW"],
            output_map["current_month_alpha"], output_map["current_month_CE_kgCO2_per_kgH2"],
        )
        _log_basic(
            logger,
            "[Y2M V9] solver_summary: problem_code=%s, solver_status=%s, termination_condition=%s, solve_time_sec=%s",
            output_map["problem_code"], output_map["solver_status"], output_map["termination_condition"], output_map["solve_time_sec"],
        )
        _log_basic(logger, "[Y2M V9] 本次调用结束。")
        return output_map

    except Exception as exc:
        if logger is not None:
            logger.exception("run_y2m_rolling_optimizer 调用异常。")
        return _build_error_output_map(log_file=log_file, message=f"run_y2m_rolling_optimizer 调用异常：{exc}")

    finally:
        if logger is not None:
            _close_logger(logger)


# =============================================================================
# 输入解析与日志工具函数
# =============================================================================
def _setup_logger(log_dir: Optional[Any]) -> tuple[logging.Logger, str]:
    if log_dir is None or str(log_dir).strip() == "":
        try:
            base_dir = Path(__file__).resolve().parent
        except NameError:
            base_dir = Path.cwd()
        log_path = base_dir / "logs"
    else:
        log_path = Path(str(log_dir))

    log_path.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    log_file = log_path / f"Y2M_{now:%Y_%m_%d_%H_%M_%S_%f}_{time.time_ns()}.log"

    logger_name = f"Y2MRollingOptimizer_{now:%Y%m%d%H%M%S}_{time.time_ns()}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger, str(log_file)


def _close_logger(logger: logging.Logger) -> None:
    handlers = list(logger.handlers)
    for handler in handlers:
        try:
            handler.flush()
            handler.close()
        finally:
            logger.removeHandler(handler)


def _log_basic(logger: Optional[logging.Logger], msg: str, *args: Any) -> None:
    """简洁日志：无论 verbose 开关如何都输出，用于交付运行状态。"""
    if logger is not None:
        logger.info(msg, *args)


def _log_verbose(logger: Optional[logging.Logger], msg: str, *args: Any) -> None:
    """复杂日志：仅 _INTERNAL_VERBOSE_STAGE_LOG=True 时输出，用于研发调试。"""
    if logger is not None and _INTERNAL_VERBOSE_STAGE_LOG:
        logger.info(msg, *args)


def _fmt_value(x: Any, digits: int = 6) -> str:
    if x is None:
        return "None"
    try:
        val = float(x)
    except Exception:
        return str(x)
    if not np.isfinite(val):
        return str(val)
    return f"{val:.{digits}f}"


def _array_summary(arr: np.ndarray, name: str, digits: int = 6) -> str:
    a = np.asarray(arr, dtype=float).reshape(-1)
    if a.size == 0:
        return f"{name}: length=0"
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return f"{name}: length={a.size}, finite=0"
    return (
        f"{name}: length={a.size}, min={np.min(finite):.{digits}f}, "
        f"max={np.max(finite):.{digits}f}, mean={np.mean(finite):.{digits}f}, "
        f"sum={np.sum(finite):.{digits}f}, nonzero_count={int(np.sum(np.abs(finite) > 1e-12))}"
    )


def _log_section(logger: Optional[logging.Logger], title: str) -> None:
    _log_verbose(logger, "============================================================")
    _log_verbose(logger, "%s", title)
    _log_verbose(logger, "============================================================")


def _log_input_and_conversion_verbose(logger: Optional[logging.Logger], P_pre: np.ndarray, pi_sell: np.ndarray, pi_buy: np.ndarray, H_year_kg: float, alpha_year: float, k_H_kWh_per_Nm3: float, rho_H2_kg_per_Nm3: float, P_h2_min_MW: float, P_h2_max_MW: float, P_buy_max_MW: float, P_sell_max_MW: float, Dm_max_MW: np.ndarray, C_cap_month_yuan_per_kW_month: np.ndarray, EF_tCO2_per_MWh: float, CE_limit_kgCO2_per_kgH2: float, H2_price_yuan_per_kg: float, carbon_price_yuan_per_tCO2: float, O2_byproduct_Nm3_per_Nm3_H2: float, O2_price_yuan_per_Nm3: float, h2_kg_per_mwh: float, O2_revenue_yuan_per_kgH2: float, O2_revenue_yuan_per_MWh: float, gamma_buy_mwh_per_kgH2: float, carbon_coupling_coef: float, CO2_quota_year_t_ref: float) -> None:
    _log_section(logger, "[INPUT] 输入数据与关键参数摘要")
    _log_verbose(logger, _array_summary(P_pre, "P_pre_MW"))
    _log_verbose(logger, _array_summary(pi_sell, "pi_sell_yuan_per_kWh"))
    _log_verbose(logger, _array_summary(pi_buy, "pi_buy_yuan_per_kWh"))
    _log_verbose(logger, _array_summary(Dm_max_MW, "Dm_max_MW"))
    _log_verbose(logger, _array_summary(C_cap_month_yuan_per_kW_month, "C_cap_month_yuan_per_kW_month"))
    _log_verbose(logger, "H_year_kg = %.6f", H_year_kg)
    _log_verbose(logger, "alpha_year = %.9f", alpha_year)
    _log_verbose(logger, "k_H_kWh_per_Nm3 = %.9f", k_H_kWh_per_Nm3)
    _log_verbose(logger, "rho_H2_kg_per_Nm3 = %.9f", rho_H2_kg_per_Nm3)
    _log_verbose(logger, "P_h2_min_MW / P_h2_max_MW = %.9f / %.9f", P_h2_min_MW, P_h2_max_MW)
    _log_verbose(logger, "P_buy_max_MW / P_sell_max_MW = %.9f / %.9f", P_buy_max_MW, P_sell_max_MW)
    _log_verbose(logger, "EF_tCO2_per_MWh = %.9f", EF_tCO2_per_MWh)
    _log_verbose(logger, "CE_limit_kgCO2_per_kgH2 = %.9f", CE_limit_kgCO2_per_kgH2)
    _log_verbose(logger, "H2_price_yuan_per_kg = %.9f", H2_price_yuan_per_kg)
    _log_verbose(logger, "carbon_price_yuan_per_tCO2 = %.9f", carbon_price_yuan_per_tCO2)
    _log_verbose(logger, "O2_byproduct_Nm3_per_Nm3_H2 = %.9f", O2_byproduct_Nm3_per_Nm3_H2)
    _log_verbose(logger, "O2_price_yuan_per_Nm3 = %.9f", O2_price_yuan_per_Nm3)
    _log_section(logger, "[CONVERT] 物理换算")
    _log_verbose(logger, "h2_kg_per_mwh = %.12f kg/MWh", h2_kg_per_mwh)
    _log_verbose(logger, "O2_revenue_yuan_per_kgH2 = %.12f 元/kgH2", O2_revenue_yuan_per_kgH2)
    _log_verbose(logger, "O2_revenue_yuan_per_MWh = %.12f 元/MWh", O2_revenue_yuan_per_MWh)
    _log_verbose(logger, "gamma_buy_mwh_per_kgH2 = %.12f MWh/kgH2", gamma_buy_mwh_per_kgH2)
    _log_verbose(logger, "carbon_coupling_coef = %.12f", carbon_coupling_coef)
    _log_verbose(logger, "CO2_quota_year_t_ref = %.12f tCO2", CO2_quota_year_t_ref)



def _estimate_model_size(T: int, n_month_var: int, use_buy_sell_binary: bool, mode_lc: str) -> dict[str, int]:
    n_continuous_vars = 4 * int(T) + int(n_month_var)
    n_binary_vars = int(T) if use_buy_sell_binary else 0
    n_constraints = 3 * int(T) + int(n_month_var) + 2
    fixed_modes = {"economic_fixed_h2_target", "economic_fixed_min_boundary", "economic_fixed_max_boundary", "feasible_fixed_h2_target"}
    if mode_lc in fixed_modes:
        n_constraints += 1
    if use_buy_sell_binary:
        n_constraints += 2 * int(T)
    return {"n_continuous_vars_est": n_continuous_vars, "n_binary_vars_est": n_binary_vars, "n_constraints_est": n_constraints}


def _safe_sum(arr: np.ndarray) -> float:
    a = np.asarray(arr, dtype=float).reshape(-1)
    finite = a[np.isfinite(a)]
    return float(np.sum(finite)) if finite.size > 0 else float("nan")


def _solution_metrics(P_h2: np.ndarray, P_gen: np.ndarray, P_buy: np.ndarray, P_sell: np.ndarray, Dm: np.ndarray, pi_buy: np.ndarray, pi_sell: np.ndarray, C_cap_month: np.ndarray, Dm_max: np.ndarray, month_of_t: np.ndarray, dt: float, h2_kg_per_mwh: float, k_H: float, alpha_year: float, CE_limit: float, EF: float, H_done_kg: float, E_gen_done_mwh: float, E_sell_done_mwh: float, CO2_done_kg: float, H_rem_target: Optional[float], H2_price: float, carbon_price: float, O2_byproduct_Nm3_per_Nm3_H2: float, O2_price_yuan_per_Nm3: float, P_pre: np.ndarray, P_h2_min: float, P_h2_max: float, use_buy_sell_binary: bool, force_zero_sell: bool, mode_lc: str) -> dict[str, Any]:
    gen_mwh = _safe_sum(P_gen) * dt
    buy_mwh = _safe_sum(P_buy) * dt
    sell_mwh = _safe_sum(P_sell) * dt
    h2_kg = _safe_sum(P_h2) * dt * h2_kg_per_mwh
    h2_volume_Nm3 = _safe_sum(P_h2) * dt * 1000.0 / k_H
    buy_cost_yuan = float(np.sum(np.asarray(pi_buy) * np.asarray(P_buy)) * dt * 1000.0)
    sell_revenue_yuan = float(np.sum(np.asarray(pi_sell) * np.asarray(P_sell)) * dt * 1000.0)
    capacity_cost_yuan = float(np.sum(np.asarray(C_cap_month) * np.asarray(Dm)) * 1000.0)
    h2_revenue_yuan = float(H2_price * h2_kg)
    o2_revenue_yuan = float(O2_price_yuan_per_Nm3 * O2_byproduct_Nm3_per_Nm3_H2 * h2_volume_Nm3)
    carbon_cost_yuan = float(buy_mwh * EF * carbon_price)
    net_objective_yuan = buy_cost_yuan - sell_revenue_yuan + capacity_cost_yuan - h2_revenue_yuan - o2_revenue_yuan + carbon_cost_yuan
    if mode_lc == "max_h2":
        objective_value_raw = -h2_kg
        objective_value_interpreted = h2_kg
    elif mode_lc == "min_h2":
        objective_value_raw = h2_kg
        objective_value_interpreted = h2_kg
    else:
        objective_value_raw = net_objective_yuan
        objective_value_interpreted = net_objective_yuan
    alpha_lhs = E_sell_done_mwh + sell_mwh
    alpha_rhs = alpha_year * (E_gen_done_mwh + gen_mwh)
    alpha_slack = alpha_rhs - alpha_lhs
    ce_lhs = CO2_done_kg + buy_mwh * EF * 1000.0
    ce_rhs = CE_limit * (H_done_kg + h2_kg)
    ce_slack = ce_rhs - ce_lhs
    total_alpha = alpha_lhs / (E_gen_done_mwh + gen_mwh) if (E_gen_done_mwh + gen_mwh) > 1e-9 else None
    total_CE = ce_lhs / (H_done_kg + h2_kg) if (H_done_kg + h2_kg) > 1e-9 else None
    h2_target_gap_kg = None if H_rem_target is None else h2_kg - float(H_rem_target)
    power_balance_residual = np.asarray(P_gen) + np.asarray(P_buy) - np.asarray(P_h2) - np.asarray(P_sell)
    power_balance_max_abs_residual_MW = float(np.nanmax(np.abs(power_balance_residual))) if power_balance_residual.size > 0 else 0.0
    P_gen_upper_max_violation_MW = float(max(0.0, np.nanmax(np.asarray(P_gen) - np.asarray(P_pre)))) if P_gen.size > 0 else 0.0
    P_h2_min_violation_MW = float(max(0.0, P_h2_min - np.nanmin(P_h2))) if P_h2.size > 0 else 0.0
    P_h2_max_violation_MW = float(max(0.0, np.nanmax(P_h2) - P_h2_max)) if P_h2.size > 0 else 0.0
    Dm_max_violation_MW = float(max(0.0, np.nanmax(np.asarray(Dm) - np.asarray(Dm_max)))) if Dm.size > 0 else 0.0
    dm_cover_violation = 0.0
    for m in (range(1, int(np.max(month_of_t)) + 1) if month_of_t.size > 0 else []):
        idx = month_of_t == m
        if np.any(idx) and m - 1 < len(Dm):
            dm_cover_violation = max(dm_cover_violation, float(np.nanmax(P_buy[idx] - Dm[m - 1])))
    dm_cover_violation = max(0.0, dm_cover_violation)
    finite_mask = np.isfinite(P_buy) & np.isfinite(P_sell)
    if np.any(finite_mask):
        overlap = np.minimum(np.maximum(P_buy[finite_mask], 0.0), np.maximum(P_sell[finite_mask], 0.0))
        max_overlap_mw = float(np.max(overlap)) if overlap.size > 0 else 0.0
        sum_overlap_mwh = float(np.sum(overlap) * dt) if overlap.size > 0 else 0.0
        num_overlap_hours = int(np.sum(overlap > _INTERNAL_BUY_SELL_OVERLAP_TOL_MW))
    else:
        max_overlap_mw, sum_overlap_mwh, num_overlap_hours = float("nan"), float("nan"), 0
    return {
        "Gen_MWh_total": gen_mwh, "H2_kg_total": h2_kg, "Buy_MWh_total": buy_mwh, "Sell_MWh_total": sell_mwh,
        "max_Dm_MW": float(np.nanmax(Dm)) if Dm.size > 0 else None,
        "buy_cost_yuan": buy_cost_yuan, "sell_revenue_yuan": sell_revenue_yuan, "capacity_cost_yuan": capacity_cost_yuan,
        "h2_revenue_yuan": h2_revenue_yuan, "o2_revenue_yuan": o2_revenue_yuan, "carbon_cost_yuan": carbon_cost_yuan,
        "net_objective_yuan": net_objective_yuan, "objective_value_raw": objective_value_raw, "objective_value_interpreted": objective_value_interpreted,
        "alpha_constraint_lhs": alpha_lhs, "alpha_constraint_rhs": alpha_rhs, "alpha_constraint_slack": alpha_slack,
        "CE_constraint_lhs_kgCO2": ce_lhs, "CE_constraint_rhs_kgCO2": ce_rhs, "CE_constraint_slack_kgCO2": ce_slack,
        "alpha_total_with_history": total_alpha, "CE_total_with_history": total_CE, "H2_target_gap_kg": h2_target_gap_kg,
        "power_balance_max_abs_residual_MW": power_balance_max_abs_residual_MW, "P_gen_upper_max_violation_MW": P_gen_upper_max_violation_MW,
        "P_h2_min_violation_MW": P_h2_min_violation_MW, "P_h2_max_violation_MW": P_h2_max_violation_MW,
        "Dm_max_violation_MW": Dm_max_violation_MW, "Dm_cover_max_violation_MW": dm_cover_violation,
        "buy_sell_overlap_max_MW": max_overlap_mw, "buy_sell_overlap_energy_MWh": sum_overlap_mwh, "buy_sell_overlap_hours_count": num_overlap_hours,
        "use_buy_sell_binary": bool(use_buy_sell_binary), "force_zero_sell": bool(force_zero_sell),
    }


def _log_stage_verbose_report(logger: Optional[logging.Logger], solve_mode: str, diagnostics: dict[str, Any]) -> None:
    if not _INTERNAL_VERBOSE_STAGE_LOG:
        return
    _log_section(logger, f"[STAGE] {solve_mode}")
    keys_order = [
        "stage_role", "problem_type", "objective_type", "solver_name", "factory_name", "time_limit_sec", "mip_gap", "presolve", "tee",
        "n_time_steps", "n_months", "n_continuous_vars_est", "n_binary_vars_est", "n_constraints_est", "force_zero_sell", "use_buy_sell_binary",
        "mutex_model_reason", "price_condition_checked", "price_condition_pass", "carbon_adder_yuan_per_kwh", "max_price_violation_yuan_per_kwh",
        "problem", "info", "solver_status", "termination_condition", "model_build_time_sec", "solver_solve_time_sec", "solution_extract_time_sec", "stage_total_time_sec",
        "objective_value_raw", "objective_value_interpreted", "Gen_MWh_total", "H2_kg_total", "Buy_MWh_total", "Sell_MWh_total", "max_Dm_MW",
        "buy_cost_yuan", "sell_revenue_yuan", "capacity_cost_yuan", "h2_revenue_yuan", "o2_revenue_yuan", "carbon_cost_yuan", "net_objective_yuan",
        "H2_target_gap_kg", "alpha_constraint_slack", "CE_constraint_slack_kgCO2", "power_balance_max_abs_residual_MW", "P_gen_upper_max_violation_MW",
        "P_h2_min_violation_MW", "P_h2_max_violation_MW", "Dm_max_violation_MW", "Dm_cover_max_violation_MW", "buy_sell_overlap_max_MW",
        "buy_sell_overlap_energy_MWh", "buy_sell_overlap_hours_count",
    ]
    for key in keys_order:
        if key in diagnostics:
            val = diagnostics[key]
            if isinstance(val, float):
                _log_verbose(logger, "%s = %.12f", key, val)
            else:
                _log_verbose(logger, "%s = %s", key, val)


def _get_required(input_map: dict[str, Any], key: str) -> Any:
    if key not in input_map:
        raise ValueError(f"input_map 缺少必需字段：{key}")
    return input_map[key]


def _get_required_scalar(input_map: dict[str, Any], key: str) -> float:
    return _to_scalar(_get_required(input_map, key), key)


def _get_optional_scalar(input_map: dict[str, Any], key: str, default: Optional[float]) -> Optional[float]:
    if key not in input_map or input_map[key] is None:
        return default
    return _to_scalar(input_map[key], key)


def _to_scalar(x: Any, name: str) -> float:
    if isinstance(x, (list, tuple, dict, np.ndarray)):
        raise ValueError(f"{name} 必须为标量，不能为 list / tuple / dict / ndarray。")
    try:
        val = float(x)
    except Exception as exc:
        raise ValueError(f"{name} 必须为可转换为 float 的标量。") from exc
    if not np.isfinite(val):
        raise ValueError(f"{name} 必须为有限数值。")
    return val


def _to_year_hour_array(x: Iterable[float], name: str) -> np.ndarray:
    """将年度小时级输入转为一维有限数值数组；长度校验在日历识别函数中统一完成。"""
    if isinstance(x, dict):
        raise ValueError(f"{name} 必须为年度小时级 list，不能为 dict。")
    try:
        arr = np.asarray(list(x), dtype=float).reshape(-1)
    except TypeError as exc:
        raise ValueError(f"{name} 必须为可迭代的年度小时级 list。") from exc
    if arr.size == 0:
        raise ValueError(f"{name} 不能为空。")
    if np.any(~np.isfinite(arr)):
        raise ValueError(f"{name} 不允许包含 NaN 或 Inf。")
    return arr


def _detect_year_calendar_from_arrays(
    P_pre: np.ndarray,
    pi_sell: np.ndarray,
    pi_buy: np.ndarray,
) -> _YearCalendarInfo:
    """根据三个年度小时级数组长度自动识别平年/闰年，接口不新增字段。"""
    lengths = {
        "P_pre_MW": int(P_pre.size),
        "pi_sell_yuan_per_kWh": int(pi_sell.size),
        "pi_buy_yuan_per_kWh": int(pi_buy.size),
    }
    unique_lengths = set(lengths.values())
    if len(unique_lengths) != 1:
        raise ValueError(
            "P_pre_MW、pi_sell_yuan_per_kWh、pi_buy_yuan_per_kWh 长度必须一致；"
            f"当前长度为 {lengths}。"
        )

    year_hours = unique_lengths.pop()
    if year_hours == 8760:
        days_per_month = np.array([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], dtype=int)
        calendar_type = "common_year_8760h"
        is_leap_year_data = False
    elif year_hours == 8784:
        days_per_month = np.array([31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31], dtype=int)
        calendar_type = "leap_year_8784h"
        is_leap_year_data = True
    else:
        raise ValueError(
            "年度小时级数据长度必须为 8760 或 8784；"
            f"当前长度为 {year_hours}。"
        )

    hours_per_month = days_per_month * 24
    if int(np.sum(hours_per_month)) != year_hours:
        raise RuntimeError(
            f"内部年度日历构造错误：calendar_type={calendar_type}, "
            f"sum(hours_per_month)={int(np.sum(hours_per_month))}, year_hours={year_hours}。"
        )

    return _YearCalendarInfo(
        year_hours=int(year_hours),
        calendar_type=calendar_type,
        is_leap_year_data=bool(is_leap_year_data),
        days_per_month=days_per_month,
        hours_per_month=hours_per_month,
    )


def _log_year_calendar_verbose(
    logger: Optional[logging.Logger],
    year_calendar: _YearCalendarInfo,
    opt_month: int,
    T_rem: int,
    rem_month_ids: np.ndarray,
) -> None:
    """复杂日志：记录年度数据平年/闰年自动识别结果，不进入 output_map。"""
    _log_verbose(logger, "[CALENDAR] year_hours = %s", year_calendar.year_hours)
    _log_verbose(logger, "[CALENDAR] calendar_type = %s", year_calendar.calendar_type)
    _log_verbose(logger, "[CALENDAR] is_leap_year_data = %s", year_calendar.is_leap_year_data)
    _log_verbose(logger, "[CALENDAR] days_per_month = %s", year_calendar.days_per_month.tolist())
    _log_verbose(logger, "[CALENDAR] hours_per_month = %s", year_calendar.hours_per_month.tolist())
    _log_verbose(logger, "[CALENDAR] opt_month = %s", opt_month)
    _log_verbose(logger, "[CALENDAR] remaining_months = %s", np.asarray(rem_month_ids, dtype=int).tolist())
    _log_verbose(logger, "[CALENDAR] T_rem = %s", T_rem)


def _to_12_array(x: Iterable[float], name: str) -> np.ndarray:
    arr = np.asarray(list(x), dtype=float).reshape(-1)
    if arr.size != 12:
        raise ValueError(f"{name} 必须为长度 12 的 list。")
    if np.any(~np.isfinite(arr)):
        raise ValueError(f"{name} 不允许包含 NaN 或 Inf。")
    return arr


def _build_internal_solver_config(
    time_limit_sec: Optional[float],
    mip_gap: Optional[float],
) -> _SolverConfig:
    if time_limit_sec is not None and float(time_limit_sec) <= 0:
        raise ValueError("time_limit_sec 必须大于 0 或为 None。")
    if mip_gap is not None and float(mip_gap) < 0:
        raise ValueError("mip_gap 必须大于等于 0 或为 None。")

    return _SolverConfig(
        solver_name=_INTERNAL_SOLVER_NAME,
        time_limit_sec=None if time_limit_sec is None else float(time_limit_sec),
        mip_gap=None if mip_gap is None else float(mip_gap),
    )



def _copy_solver_config_with_threads(
    solver_cfg: _SolverConfig,
    threads: Optional[int],
    force_tee_false: bool = False,
) -> _SolverConfig:
    """复制求解器配置，并可覆盖 threads。用于边界进程并行，避免共享 solver 对象。"""
    return _SolverConfig(
        solver_name=solver_cfg.solver_name,
        candidate_factories={k: list(v) for k, v in solver_cfg.candidate_factories.items()},
        time_limit_sec=solver_cfg.time_limit_sec,
        mip_gap=solver_cfg.mip_gap,
        threads=None if threads is None else int(threads),
        presolve=solver_cfg.presolve,
        tee=False if force_tee_false else solver_cfg.tee,
        load_solutions=solver_cfg.load_solutions,
        symbolic_solver_labels=solver_cfg.symbolic_solver_labels,
        extra_options=dict(solver_cfg.extra_options or {}),
    )


def _compute_boundary_threads_per_solver(workers: int = _INTERNAL_BOUNDARY_PARALLEL_WORKERS) -> int:
    """边界进程并行时，每个求解器分配的线程数。"""
    cpu_count = os.cpu_count() or 1
    workers_eff = max(1, int(workers))
    threads = int((cpu_count * _INTERNAL_BOUNDARY_PARALLEL_CPU_FRACTION) // workers_eff)
    return max(1, threads)


def _make_boundary_worker_payload(
    common_args: dict[str, Any],
    solver_cfg: _SolverConfig,
    stage_name: str,
    n_rem_months: int,
    h2_kg_per_mwh: float,
) -> dict[str, Any]:
    """构造 ProcessPoolExecutor worker 的纯数据 payload。不要传 logger / Pyomo model / solver。"""
    payload = {k: v for k, v in common_args.items() if k != "logger"}
    payload["solver_cfg"] = solver_cfg
    payload["stage_name"] = stage_name
    payload["n_rem_months"] = int(n_rem_months)
    payload["h2_kg_per_mwh"] = float(h2_kg_per_mwh)
    return payload


def _boundary_solve_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """
    边界求解子进程入口。必须是模块顶层函数，便于 Windows spawn 模式 pickle。

    子进程内重新构造 Pyomo 模型、重新创建求解器，不共享主进程中的 logger、模型或 solver。
    """
    stage_name = str(payload["stage_name"])
    P_pre_rem = np.asarray(payload["P_pre_rem"], dtype=float)
    month_of_t_rem_local = np.asarray(payload["month_of_t_rem_local"], dtype=int)
    n_rem_months = int(payload["n_rem_months"])
    dt_h = float(payload["dt_h"])
    h2_kg_per_mwh = float(payload["h2_kg_per_mwh"])
    P_h2_min_MW = float(payload["P_h2_min_MW"])
    P_h2_max_MW = float(payload["P_h2_max_MW"])
    P_buy_max_MW = float(payload["P_buy_max_MW"])

    P_h2_0, P_gen_0, P_sell_0, P_buy_0, Dm_0 = _make_initial_guess(
        P_pre_rem,
        month_of_t_rem_local,
        n_rem_months,
        dt_h,
        h2_kg_per_mwh,
        P_h2_min_MW,
        P_h2_max_MW,
        P_buy_max_MW,
        None,
    )

    res = _solve_pyomo_model_roll_profit(
        P_pre_rem,
        np.asarray(payload["pi_sell_rem"], dtype=float),
        np.asarray(payload["pi_buy_rem"], dtype=float),
        month_of_t_rem_local,
        dt_h,
        np.asarray(payload["C_cap_month_rem"], dtype=float),
        None,
        float(payload["k_H_kWh_per_Nm3"]),
        float(payload["alpha_year"]),
        float(payload["CE_limit_kgCO2_per_kgH2"]),
        P_h2_min_MW,
        P_h2_max_MW,
        float(payload["EF_tCO2_per_MWh"]),
        float(payload["rho_H2_kg_per_Nm3"]),
        float(payload["P_sell_max_MW"]),
        P_buy_max_MW,
        np.asarray(payload["Dm_max_rem"], dtype=float),
        float(payload["H_done_kg"]),
        float(payload["E_gen_done_mwh"]),
        float(payload["E_sell_done_mwh"]),
        float(payload["CO2_done_kg"]),
        float(payload["H2_price_yuan_per_kg"]),
        float(payload["carbon_price_yuan_per_tCO2"]),
        float(payload["O2_byproduct_Nm3_per_Nm3_H2"]),
        float(payload["O2_price_yuan_per_Nm3"]),
        P_h2_0,
        P_gen_0,
        P_sell_0,
        P_buy_0,
        Dm_0,
        payload["solver_cfg"],
        stage_name,
        logger=None,
    )

    diag = res.setdefault("diagnostics", {})
    diag["boundary_parallel_backend"] = "ProcessPoolExecutor"
    diag["boundary_worker_pid"] = os.getpid()
    diag["boundary_worker_threads"] = getattr(payload["solver_cfg"], "threads", None)
    return res




def _make_initial_guess(
    P_pre_rem: np.ndarray,
    month_of_t_rem_local: np.ndarray,
    n_rem_months: int,
    dt_h: float,
    h2_kg_per_mwh: float,
    P_h2_min_MW: float,
    P_h2_max_MW: float,
    P_buy_max_MW: float,
    H_target_kg: Optional[float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    T = int(P_pre_rem.size)
    if H_target_kg is None or T <= 0 or dt_h <= 0 or h2_kg_per_mwh <= 0:
        p_level = P_h2_min_MW
    else:
        p_level = float(H_target_kg) / max(1e-12, T * dt_h * h2_kg_per_mwh)
        p_level = min(max(p_level, P_h2_min_MW), P_h2_max_MW)
    P_h2_0 = np.full(T, p_level, dtype=float)
    P_gen_0 = np.minimum(P_pre_rem, P_h2_0)
    P_sell_0 = np.zeros(T, dtype=float)
    P_buy_0 = np.minimum(P_buy_max_MW, np.maximum(0.0, P_h2_0 - P_gen_0))
    Dm_0 = np.array(
        [
            np.max(P_buy_0[month_of_t_rem_local == mm]) if np.any(month_of_t_rem_local == mm) else 0.0
            for mm in range(1, n_rem_months + 1)
        ],
        dtype=float,
    )
    return P_h2_0, P_gen_0, P_sell_0, P_buy_0, Dm_0


def _stage_ok(res: Optional[dict[str, Any]]) -> bool:
    if res is None:
        return False
    diag = res.get("diagnostics", {})
    if int(diag.get("problem", 9)) != 0:
        return False
    for key in ("P_h2", "P_gen", "P_buy", "P_sell", "Dm"):
        arr = np.asarray(res.get(key, []), dtype=float)
        if arr.size == 0 or np.any(~np.isfinite(arr)):
            return False
    return True


def _stage_h2_kg(res: dict[str, Any], dt_h: float, h2_kg_per_mwh: float) -> Optional[float]:
    try:
        P_h2 = np.asarray(res.get("P_h2", []), dtype=float).reshape(-1)
        if P_h2.size == 0 or np.any(~np.isfinite(P_h2)):
            return None
        return float(np.sum(P_h2) * dt_h * h2_kg_per_mwh)
    except Exception:
        return None


def _stage_total_time(res: Optional[dict[str, Any]]) -> float:
    if res is None:
        return 0.0
    diag = res.get("diagnostics", {})
    try:
        return float(diag.get("stage_total_time_sec", res.get("solve_time", 0.0)))
    except Exception:
        return 0.0


def _h2_compare_tol(*values: Optional[float]) -> float:
    finite_vals = []
    for v in values:
        try:
            fv = float(v)
        except Exception:
            continue
        if np.isfinite(fv):
            finite_vals.append(abs(fv))
    scale = max([1.0] + finite_vals)
    return max(_INTERNAL_COMPARE_TOL_KG, 1e-8 * scale)


def _mix_boundary_solution(
    min_res: dict[str, Any],
    max_res: dict[str, Any],
    H_target_kg: float,
    H_min_kg: float,
    H_max_kg: float,
) -> Optional[dict[str, Any]]:
    if not (_stage_ok(min_res) and _stage_ok(max_res)):
        return min_res if _stage_ok(min_res) else None
    if abs(H_max_kg - H_min_kg) <= _h2_compare_tol(H_target_kg, H_min_kg, H_max_kg):
        return min_res
    lam = min(1.0, max(0.0, (float(H_target_kg) - float(H_min_kg)) / (float(H_max_kg) - float(H_min_kg))))
    mixed: dict[str, Any] = {"diagnostics": min_res.get("diagnostics", {})}
    for key in ("P_h2", "P_gen", "P_buy", "P_sell", "Dm"):
        a = np.asarray(min_res[key], dtype=float)
        b = np.asarray(max_res[key], dtype=float)
        mixed[key] = (1.0 - lam) * a + lam * b
    return mixed


def _log_stage_records(logger: Optional[logging.Logger], stage_records: list[_StageRecord]) -> None:
    if logger is None:
        return
    _log_verbose(logger, "============================================================")
    _log_verbose(logger, "[STAGE SUMMARY] 阶段求解汇总")
    _log_verbose(logger, "============================================================")
    for rec in stage_records:
        _log_verbose(
            logger,
            "stage=%s, selected=%s, problem_code=%s, solver_status=%s, termination=%s, time=%.6f, H2_kg=%s, note=%s",
            rec.stage_name, rec.selected, rec.problem_code, rec.solver_status,
            rec.termination_condition, rec.solve_time_sec, _fmt_value(rec.h2_kg, 6), rec.note,
        )


# =============================================================================
# 输出封装
# =============================================================================
def _build_output_map(
    success: bool,
    message: str,
    exit_reason: str,
    log_file: str,
    plan_available: bool,
    final_status_code: int,
    final_status_name: str,
    final_solve_mode: str,
    selected_stage_name: str,
    idx_rem: np.ndarray,
    month_of_t: np.ndarray,
    month_of_t_rem_abs: np.ndarray,
    rem_month_ids: np.ndarray,
    dt_h: float,
    h2_kg_per_mwh: float,
    EF_tCO2_per_MWh: float,
    P_gen_plan_rem: np.ndarray,
    P_h2_plan_rem: np.ndarray,
    P_buy_plan_rem: np.ndarray,
    P_sell_plan_rem: np.ndarray,
    Dm_plan_rem: np.ndarray,
    H_done_kg: float,
    H_rem_target_kg: Optional[float],
    H_original_target_kg: Optional[float],
    H_dispatch_target_kg: Optional[float],
    H_dispatch_actual_kg: Optional[float],
    min_h2_opt_kg: Optional[float],
    max_h2_opt_kg: Optional[float],
    h2_target_relation: str,
    H_target_adjusted_flag: bool,
    H_target_adjust_reason: str,
    h2_compare_tol_kg: Optional[float],
    fallback_used: bool,
    fallback_from_stage: str,
    fallback_to_stage: str,
    fallback_reason: str,
    H_target_constraint_active: int,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:

    if plan_available:
        hour_index = np.where(idx_rem)[0] + 1
        month_hourly = month_of_t[idx_rem]

        month_list: list[int] = []
        Gen_MWh: list[Optional[float]] = []
        H2_kg: list[Optional[float]] = []
        Buy_MWh: list[Optional[float]] = []
        Sell_MWh: list[Optional[float]] = []
        alpha_month: list[Optional[float]] = []
        Dm_MW: list[Optional[float]] = []
        CE_kgCO2_per_kgH2: list[Optional[float]] = []

        for i, abs_month in enumerate(rem_month_ids):
            idx_m = month_of_t_rem_abs == abs_month
            gen_mwh = float(np.sum(P_gen_plan_rem[idx_m]) * dt_h)
            h2_kg = float(np.sum(P_h2_plan_rem[idx_m]) * dt_h * h2_kg_per_mwh)
            buy_mwh = float(np.sum(P_buy_plan_rem[idx_m]) * dt_h)
            sell_mwh = float(np.sum(P_sell_plan_rem[idx_m]) * dt_h)
            co2_kg = buy_mwh * EF_tCO2_per_MWh * 1000.0

            alpha = sell_mwh / gen_mwh if gen_mwh > 1e-9 else None
            ce = co2_kg / h2_kg if h2_kg > 1e-9 else None
            dm = float(Dm_plan_rem[i]) if i < len(Dm_plan_rem) else None

            month_list.append(int(abs_month))
            Gen_MWh.append(_clean_float(gen_mwh))
            H2_kg.append(_clean_float(h2_kg))
            Buy_MWh.append(_clean_float(buy_mwh))
            Sell_MWh.append(_clean_float(sell_mwh))
            alpha_month.append(_clean_float(alpha))
            Dm_MW.append(_clean_float(dm))
            CE_kgCO2_per_kgH2.append(_clean_float(ce))

        current_month_Dm_MW = Dm_MW[0] if Dm_MW else None
        current_month_H2_kg = H2_kg[0] if H2_kg else None
        current_month_alpha = alpha_month[0] if alpha_month else None
        current_month_CE = CE_kgCO2_per_kgH2[0] if CE_kgCO2_per_kgH2 else None

        hour_index_list = [int(v) for v in hour_index.tolist()]
        month_hourly_list = [int(v) for v in month_hourly.tolist()]
        P_gen_list = _clean_float_list(P_gen_plan_rem)
        P_h2_list = _clean_float_list(P_h2_plan_rem)
        P_buy_list = _clean_float_list(P_buy_plan_rem)
        P_sell_list = _clean_float_list(P_sell_plan_rem)

    else:
        hour_index_list = []
        month_hourly_list = []
        P_gen_list = []
        P_h2_list = []
        P_buy_list = []
        P_sell_list = []
        month_list = []
        Gen_MWh = []
        H2_kg = []
        Buy_MWh = []
        Sell_MWh = []
        alpha_month = []
        Dm_MW = []
        CE_kgCO2_per_kgH2 = []
        current_month_Dm_MW = None
        current_month_H2_kg = None
        current_month_alpha = None
        current_month_CE = None

    return {
        "success": bool(success),
        "message": str(message),
        "exit_reason": str(exit_reason),
        "log_file": str(log_file),

        "final_status_code": int(final_status_code),
        "final_status_name": str(final_status_name),
        "final_solve_mode": str(final_solve_mode),
        "selected_stage_name": str(selected_stage_name),
        "plan_available": bool(plan_available),
        "dispatch_allowed": bool(success and plan_available),

        "current_month_Dm_MW": _clean_float(current_month_Dm_MW),
        "current_month_H2_kg": _clean_float(current_month_H2_kg),
        "current_month_alpha": _clean_float(current_month_alpha),
        "current_month_CE_kgCO2_per_kgH2": _clean_float(current_month_CE),

        "hour_index": hour_index_list,
        "month_hourly": month_hourly_list,
        "P_gen_MW": P_gen_list,
        "P_h2_MW": P_h2_list,
        "P_buy_MW": P_buy_list,
        "P_sell_MW": P_sell_list,

        "month": month_list,
        "Gen_MWh": Gen_MWh,
        "H2_kg": H2_kg,
        "Buy_MWh": Buy_MWh,
        "Sell_MWh": Sell_MWh,
        "alpha_month": alpha_month,
        "Dm_MW": Dm_MW,
        "CE_kgCO2_per_kgH2": CE_kgCO2_per_kgH2,

        "H_done_kg": _clean_float(H_done_kg),
        "H_rem_target_kg": _clean_float(H_rem_target_kg),
        "H_original_target_kg": _clean_float(H_original_target_kg),
        "H_dispatch_target_kg": _clean_float(H_dispatch_target_kg),
        "H_dispatch_actual_kg": _clean_float(H_dispatch_actual_kg),
        "min_h2_opt_kg": _clean_float(min_h2_opt_kg),
        "max_h2_opt_kg": _clean_float(max_h2_opt_kg),
        "h2_target_relation": str(h2_target_relation),
        "H_target_adjusted_flag": bool(H_target_adjusted_flag),
        "H_target_adjust_reason": str(H_target_adjust_reason),
        "h2_compare_tol_kg": _clean_float(h2_compare_tol_kg),
        "H_target_constraint_active": int(H_target_constraint_active),

        "fallback_used": bool(fallback_used),
        "fallback_from_stage": str(fallback_from_stage),
        "fallback_to_stage": str(fallback_to_stage),
        "fallback_reason": str(fallback_reason),

        "problem_code": int(diagnostics.get("problem_code", 9)),
        "solver_status": str(diagnostics.get("solver_status", "")),
        "termination_condition": str(diagnostics.get("termination_condition", "")),
        "solve_time_sec": _clean_float(diagnostics.get("solve_time_sec", None)),
    }


def _build_error_output_map(log_file: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "message": str(message),
        "exit_reason": "exception",
        "log_file": str(log_file),
        "final_status_code": -9,
        "final_status_name": "exception_no_output",
        "final_solve_mode": "no_valid_solution",
        "selected_stage_name": "",
        "plan_available": False,
        "dispatch_allowed": False,
        "current_month_Dm_MW": None,
        "current_month_H2_kg": None,
        "current_month_alpha": None,
        "current_month_CE_kgCO2_per_kgH2": None,
        "hour_index": [],
        "month_hourly": [],
        "P_gen_MW": [],
        "P_h2_MW": [],
        "P_buy_MW": [],
        "P_sell_MW": [],
        "month": [],
        "Gen_MWh": [],
        "H2_kg": [],
        "Buy_MWh": [],
        "Sell_MWh": [],
        "alpha_month": [],
        "Dm_MW": [],
        "CE_kgCO2_per_kgH2": [],
        "H_done_kg": None,
        "H_rem_target_kg": None,
        "H_original_target_kg": None,
        "H_dispatch_target_kg": None,
        "H_dispatch_actual_kg": None,
        "min_h2_opt_kg": None,
        "max_h2_opt_kg": None,
        "h2_target_relation": "not_evaluated",
        "H_target_adjusted_flag": False,
        "H_target_adjust_reason": "not_evaluated",
        "h2_compare_tol_kg": None,
        "H_target_constraint_active": 0,
        "fallback_used": False,
        "fallback_from_stage": "",
        "fallback_to_stage": "",
        "fallback_reason": "",
        "problem_code": 2,
        "solver_status": "exception",
        "termination_condition": "exception",
        "solve_time_sec": 0.0,
    }


def _clean_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        val = float(x)
    except Exception:
        return None
    if not np.isfinite(val):
        return None
    return val


def _clean_float_list(arr: np.ndarray) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    for v in np.asarray(arr).reshape(-1):
        out.append(_clean_float(v))
    return out


# =============================================================================
# 求解器工具函数
# =============================================================================
def _create_solver(solver_cfg: _SolverConfig):
    requested = solver_cfg.solver_name.lower().strip()
    candidates = solver_cfg.candidate_factories.get(requested, [requested])

    last_error: Optional[str] = None
    for candidate in candidates:
        try:
            solver = SolverFactory(candidate)
            if solver is None:
                last_error = f"SolverFactory('{candidate}') 返回 None"
                continue
            if not solver.available(False):
                last_error = f"求解器 {candidate} 不可用"
                continue
            _apply_solver_options(solver, candidate, solver_cfg)
            return solver, candidate
        except Exception as exc:
            last_error = f"尝试求解器 {candidate} 失败：{exc}"
            continue

    raise RuntimeError(f"无法创建可用求解器，内部 solver={requested}。最后错误：{last_error}")


def _apply_solver_options(solver, solver_name: str, solver_cfg: _SolverConfig) -> None:
    name = solver_name.lower()

    if name == "gurobi":
        if solver_cfg.time_limit_sec is not None:
            solver.options["TimeLimit"] = float(solver_cfg.time_limit_sec)
        if solver_cfg.mip_gap is not None:
            solver.options["MIPGap"] = float(solver_cfg.mip_gap)
        if solver_cfg.threads is not None:
            solver.options["Threads"] = int(solver_cfg.threads)
        if solver_cfg.presolve is not None:
            solver.options["Presolve"] = 2 if solver_cfg.presolve else 0

    elif name in {"highs", "appsi_highs"}:
        if solver_cfg.time_limit_sec is not None:
            solver.options["time_limit"] = float(solver_cfg.time_limit_sec)
        if solver_cfg.mip_gap is not None:
            solver.options["mip_rel_gap"] = float(solver_cfg.mip_gap)
        if solver_cfg.threads is not None:
            solver.options["threads"] = int(solver_cfg.threads)
        if solver_cfg.presolve is not None:
            solver.options["presolve"] = "on" if solver_cfg.presolve else "off"

    for k, v in (solver_cfg.extra_options or {}).items():
        solver.options[k] = v


def _empty_solver_diagnostics() -> Dict[str, Any]:
    return {
        "problem_code": 9,
        "solver_status": "not_solved",
        "termination_condition": "not_solved",
        "solve_time_sec": 0.0,
    }


def _diagnostics_to_public(diagnostics: Dict[str, Any], solve_time_sec: float) -> Dict[str, Any]:
    return {
        "problem_code": int(diagnostics.get("problem", 9)),
        "solver_status": str(diagnostics.get("solver_status", "")),
        "termination_condition": str(diagnostics.get("termination_condition", "")),
        "solve_time_sec": float(solve_time_sec),
    }


# =============================================================================
# 内部函数：建立并求解剩余阶段 Pyomo 模型
# =============================================================================
def _solve_pyomo_model_roll_profit(
    P_pre: np.ndarray,
    pi_sell: np.ndarray,
    pi_buy: np.ndarray,
    month_of_t: np.ndarray,
    dt: float,
    C_cap_month: np.ndarray,
    H_rem_target: Optional[float],
    k_H: float,
    alpha_year: float,
    CE_limit: float,
    P_h2_min: float,
    P_h2_max: float,
    EF: float,
    rho_H2: float,
    P_sell_max: float,
    P_buy_max: float,
    Dm_max: np.ndarray,
    H_done_kg: float,
    E_gen_done_mwh: float,
    E_sell_done_mwh: float,
    CO2_done_kg: float,
    H2_price: float,
    carbon_price: float,
    O2_byproduct_Nm3_per_Nm3_H2: float,
    O2_price_yuan_per_Nm3: float,
    P_h2_0: np.ndarray,
    P_gen_0: np.ndarray,
    P_sell_0: np.ndarray,
    P_buy_0: np.ndarray,
    Dm_0: np.ndarray,
    solver_cfg: _SolverConfig,
    solve_mode: str,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    stage_start_time = time.perf_counter()
    T = int(P_pre.size)
    n_month_var = int(C_cap_month.size)
    h2_kg_per_mwh = 1000.0 / k_H * rho_H2
    mode_lc = solve_mode.lower().strip()

    # -------------------------------------------------------------------------
    # 内部建模策略：是否保留购售电互斥二进制变量 / 是否强制 max_h2 不售电
    # -------------------------------------------------------------------------
    # 1) max_h2 仅用于“完整约束下最大可产氢量”判断，不计售电收益；
    #    当 _INTERNAL_FORCE_ZERO_SELL_IN_MAX_H2=True 时，固定 P_sell[t]=0，
    #    并删除购售电互斥二进制变量，使 max_h2 从 MILP 简化为 LP。
    # 2) economic_fixed_h2_target / economic_free_h2 仍保持旧逻辑：
    #    非 gurobi 且价格安全时可删除互斥变量转 LP，否则保留 MILP。
    carbon_adder_yuan_per_kwh = float(carbon_price) * float(EF) / 1000.0
    price_margin = np.asarray(pi_sell, dtype=float) - np.asarray(pi_buy, dtype=float) - carbon_adder_yuan_per_kwh
    max_price_violation_yuan_per_kwh = float(np.max(price_margin)) if price_margin.size > 0 else 0.0

    price_condition_checked = False
    price_condition_pass: Optional[bool] = None
    use_buy_sell_binary = True
    force_zero_sell = False
    mutex_model_reason = "default_keep_binary"

    solver_name_lc = str(solver_cfg.solver_name).lower().strip()
    is_gurobi_solver = solver_name_lc == "gurobi"
    fixed_target_modes = {"economic_fixed_h2_target", "economic_fixed_min_boundary", "economic_fixed_max_boundary", "feasible_fixed_h2_target"}
    is_economic_mode = mode_lc in fixed_target_modes or mode_lc == "economic_free_h2"

    if mode_lc == "min_h2":
        use_buy_sell_binary = True
        force_zero_sell = False
        mutex_model_reason = "min_h2_keep_full_domain_binary"
    elif mode_lc == "max_h2":
        if _INTERNAL_FORCE_ZERO_SELL_IN_MAX_H2:
            force_zero_sell = True
            use_buy_sell_binary = False
            mutex_model_reason = "max_h2_force_zero_sell_use_lp"
        else:
            force_zero_sell = False
            use_buy_sell_binary = True
            mutex_model_reason = "max_h2_keep_binary_legacy"
    elif is_economic_mode:
        if not _INTERNAL_ENABLE_ECONOMIC_LP_IF_PRICE_SAFE:
            use_buy_sell_binary = True
            mutex_model_reason = "economic_lp_switch_disabled"
        elif is_gurobi_solver:
            use_buy_sell_binary = True
            mutex_model_reason = "gurobi_keep_original_milp"
        else:
            price_condition_checked = True
            price_condition_pass = bool(max_price_violation_yuan_per_kwh <= _INTERNAL_PRICE_SAFE_TOL_YUAN_PER_KWH)
            if price_condition_pass:
                use_buy_sell_binary = False
                mutex_model_reason = "economic_price_safe_use_lp"
            else:
                use_buy_sell_binary = True
                mutex_model_reason = "economic_price_not_safe_keep_milp"
    else:
        use_buy_sell_binary = True
        mutex_model_reason = "unknown_mode_keep_binary"

    if logger is not None:
        _log_verbose(logger, "购售电互斥建模策略 solve_mode = %s", solve_mode)
        _log_verbose(logger, "购售电互斥建模策略 solver_name = %s", solver_cfg.solver_name)
        _log_verbose(logger, 
            "购售电互斥建模策略 _INTERNAL_ENABLE_ECONOMIC_LP_IF_PRICE_SAFE = %s",
            _INTERNAL_ENABLE_ECONOMIC_LP_IF_PRICE_SAFE,
        )
        _log_verbose(logger, "购售电互斥建模策略 is_gurobi_solver = %s", is_gurobi_solver)
        _log_verbose(logger, "购售电互斥建模策略 force_zero_sell = %s", force_zero_sell)
        _log_verbose(logger, "购售电互斥建模策略 price_condition_checked = %s", price_condition_checked)
        _log_verbose(logger, "购售电互斥建模策略 price_condition_pass = %s", price_condition_pass)
        _log_verbose(logger, 
            "购售电互斥建模策略 carbon_adder_yuan_per_kWh = %.12f",
            carbon_adder_yuan_per_kwh,
        )
        _log_verbose(logger, 
            "购售电互斥建模策略 max_price_violation_yuan_per_kWh = %.12f",
            max_price_violation_yuan_per_kwh,
        )
        _log_verbose(logger, "购售电互斥建模策略 use_buy_sell_binary = %s", use_buy_sell_binary)
        _log_verbose(logger, "购售电互斥建模策略 model_type = %s", "MILP" if use_buy_sell_binary else "LP")
        _log_verbose(logger, "购售电互斥建模策略 reason = %s", mutex_model_reason)

    model = ConcreteModel(name=f"roll_profit_{solve_mode}")
    model.T = RangeSet(1, T)
    model.M = RangeSet(1, n_month_var)

    model.P_pre = Param(model.T, initialize={t + 1: float(P_pre[t]) for t in range(T)})
    model.pi_sell = Param(model.T, initialize={t + 1: float(pi_sell[t]) for t in range(T)})
    model.pi_buy = Param(model.T, initialize={t + 1: float(pi_buy[t]) for t in range(T)})
    model.month_of_t = Param(model.T, initialize={t + 1: int(month_of_t[t]) for t in range(T)})
    model.C_cap_month = Param(model.M, initialize={m + 1: float(C_cap_month[m]) for m in range(n_month_var)})
    model.Dm_max = Param(model.M, initialize={m + 1: float(Dm_max[m]) for m in range(n_month_var)})

    model.P_h2 = Var(model.T, domain=Reals, bounds=(P_h2_min, P_h2_max))
    # max_h2 阶段固定 P_sell[t] = 0：用变量上下界 (0, 0) 表达，保持 output_map 字段不变。
    # economic_* 阶段保持原有 P_sell 变量与上限。
    P_sell_bounds = (0.0, 0.0) if force_zero_sell else (0.0, P_sell_max)
    model.P_sell = Var(model.T, domain=NonNegativeReals, bounds=P_sell_bounds)
    model.P_buy = Var(model.T, domain=NonNegativeReals, bounds=(0.0, P_buy_max))
    model.P_gen = Var(model.T, domain=NonNegativeReals)
    model.Dm = Var(model.M, domain=NonNegativeReals)
    if use_buy_sell_binary:
        model.u = Var(model.T, domain=Binary)

    for t in model.T:
        model.P_h2[t].value = float(P_h2_0[t - 1])
        model.P_gen[t].value = float(P_gen_0[t - 1])
        model.P_sell[t].value = 0.0 if force_zero_sell else float(P_sell_0[t - 1])
        model.P_buy[t].value = float(P_buy_0[t - 1])
        if use_buy_sell_binary:
            model.u[t].value = 1.0 if P_buy_0[t - 1] > 1e-8 else 0.0

    for m in model.M:
        model.Dm[m].value = float(Dm_0[m - 1])

    # -------------------------------------------------------------------------
    # 约束 1：电力平衡
    # P_gen + P_buy = P_h2 + P_sell
    # max_h2 且 force_zero_sell=True 时，由 P_sell[t]=0 自动退化为：
    # P_gen + P_buy = P_h2
    # -------------------------------------------------------------------------
    model.power_balance = Constraint(
        model.T,
        rule=lambda md, t: md.P_gen[t] + md.P_buy[t] == md.P_h2[t] + md.P_sell[t],
    )

    # -------------------------------------------------------------------------
    # 约束 2：新能源接纳功率不超过预测可用功率
    # -------------------------------------------------------------------------
    model.gen_upper = Constraint(
        model.T,
        rule=lambda md, t: md.P_gen[t] <= md.P_pre[t],
    )

    # -------------------------------------------------------------------------
    # 约束 3：月度最大下网深度上限
    # -------------------------------------------------------------------------
    model.dm_upper = Constraint(
        model.M,
        rule=lambda md, m: md.Dm[m] <= md.Dm_max[m],
    )

    # -------------------------------------------------------------------------
    # 约束 4：买电 / 卖电互斥
    # max_h2 且 force_zero_sell=True 时不创建二进制变量 u[t]，因此这里不加 Big-M 互斥约束。
    # -------------------------------------------------------------------------
    if use_buy_sell_binary:
        model.buy_mutex = Constraint(
            model.T,
            rule=lambda md, t: md.P_buy[t] <= P_buy_max * md.u[t],
        )
        model.sell_mutex = Constraint(
            model.T,
            rule=lambda md, t: md.P_sell[t] <= P_sell_max * (1 - md.u[t]),
        )

    # -------------------------------------------------------------------------
    # 约束 5：月度 Dm 覆盖该月所有小时购电功率
    # -------------------------------------------------------------------------
    model.dm_cover = ConstraintList()
    for m in range(1, n_month_var + 1):
        idxs = np.where(month_of_t == m)[0] + 1
        for t in idxs:
            model.dm_cover.add(model.Dm[m] >= model.P_buy[t])

    def _sum_expr_gen(md):
        return sum(md.P_gen[t] for t in md.T) * dt

    def _sum_expr_buy(md):
        return sum(md.P_buy[t] for t in md.T) * dt

    def _sum_expr_sell(md):
        return sum(md.P_sell[t] for t in md.T) * dt

    def _sum_expr_h2(md):
        return sum(md.P_h2[t] for t in md.T) * dt * h2_kg_per_mwh

    # -------------------------------------------------------------------------
    # 约束 6：年度并网比例约束
    # E_sell_done + E_sell_rem <= alpha_year * (E_gen_done + E_gen_rem)
    # -------------------------------------------------------------------------
    model.alpha_constraint = Constraint(
        expr=_sum_expr_sell(model)
        <= alpha_year * _sum_expr_gen(model)
        + alpha_year * E_gen_done_mwh
        - E_sell_done_mwh
    )

    # -------------------------------------------------------------------------
    # 约束 7：年度单位制氢碳排约束
    # CO2_done + E_buy_rem * EF * 1000 <= CE_limit * (H_done + H_rem)
    # -------------------------------------------------------------------------
    model.ce_constraint = Constraint(
        expr=CO2_done_kg + _sum_expr_buy(model) * EF * 1000.0
        <= CE_limit * (H_done_kg + _sum_expr_h2(model))
    )

    # -------------------------------------------------------------------------
    # 约束 8：固定制氢目标等式
    # -------------------------------------------------------------------------
    if mode_lc in {"economic_fixed_h2_target", "economic_fixed_min_boundary", "economic_fixed_max_boundary", "feasible_fixed_h2_target"}:
        if H_rem_target is None:
            raise ValueError(f"{solve_mode} 模式下 H_rem_target 不允许为 None。")
        model.h_target = Constraint(expr=_sum_expr_h2(model) == float(H_rem_target))

    # -------------------------------------------------------------------------
    # 目标函数
    # -------------------------------------------------------------------------
    buy_cost = sum(model.pi_buy[t] * model.P_buy[t] for t in model.T) * dt * 1000.0
    sell_rev = sum(model.pi_sell[t] * model.P_sell[t] for t in model.T) * dt * 1000.0
    cap_cost = sum(model.C_cap_month[m] * model.Dm[m] for m in model.M) * 1000.0
    h2_rev = H2_price * _sum_expr_h2(model)
    h2_volume_Nm3 = sum(model.P_h2[t] for t in model.T) * dt * 1000.0 / k_H
    o2_rev = O2_price_yuan_per_Nm3 * O2_byproduct_Nm3_per_Nm3_H2 * h2_volume_Nm3
    carbon_cost_eq = (_sum_expr_buy(model) * EF) * carbon_price

    if logger is not None:
        _log_verbose(logger, "目标函数经济项：H2_price_yuan_per_kg = %.9f", H2_price)
        _log_verbose(logger, "目标函数经济项：O2_byproduct_Nm3_per_Nm3_H2 = %.9f", O2_byproduct_Nm3_per_Nm3_H2)
        _log_verbose(logger, "目标函数经济项：O2_price_yuan_per_Nm3 = %.9f", O2_price_yuan_per_Nm3)
        _log_verbose(logger, "目标函数经济项：O2 revenue 已计入 economic_* 目标函数；max_h2 目标函数不计入 O2 revenue。")

    if mode_lc in {"economic_fixed_h2_target", "economic_fixed_min_boundary", "economic_fixed_max_boundary", "economic_free_h2"}:
        model.obj = Objective(
            expr=buy_cost - sell_rev + cap_cost - h2_rev - o2_rev + carbon_cost_eq,
            sense=minimize,
        )
    elif mode_lc == "feasible_fixed_h2_target":
        # 修复阶段：仍固定 H_target，但目标改为较温和的可行性/运行成本目标，不追求收益最优。
        model.obj = Objective(expr=buy_cost + cap_cost + carbon_cost_eq, sense=minimize)
    elif mode_lc == "max_h2":
        model.obj = Objective(expr=-_sum_expr_h2(model), sense=minimize)
    elif mode_lc == "min_h2":
        model.obj = Objective(expr=_sum_expr_h2(model), sense=minimize)
    else:
        raise ValueError(f"未知 solve_mode：{solve_mode}")

    model_build_time_sec = time.perf_counter() - stage_start_time
    size_info = _estimate_model_size(T, n_month_var, use_buy_sell_binary, mode_lc)
    stage_role = {
        "min_h2": "full_constraints_true_min_h2",
        "max_h2": "full_constraints_true_max_h2",
        "economic_fixed_h2_target": "profit_optimization_with_fixed_h2_target",
        "economic_fixed_min_boundary": "profit_optimization_with_fixed_min_boundary_h2",
        "economic_fixed_max_boundary": "profit_optimization_with_fixed_max_boundary_h2",
        "feasible_fixed_h2_target": "feasibility_repair_with_fixed_h2_target",
        "economic_free_h2": "profit_optimization_free_h2",
    }.get(mode_lc, "unknown")
    if mode_lc == "max_h2":
        objective_type = "maximize_h2"
    elif mode_lc == "min_h2":
        objective_type = "minimize_h2"
    elif mode_lc == "feasible_fixed_h2_target":
        objective_type = "feasibility_repair_minimize_operation_cost"
    else:
        objective_type = "minimize_net_cost"

    diagnostics = {
        "problem": 9,
        "info": "not solved",
        "solver_status": "",
        "termination_condition": "",
        "solver_name": solver_cfg.solver_name,
        "factory_name": "",
        "use_buy_sell_binary": bool(use_buy_sell_binary),
        "force_zero_sell": bool(force_zero_sell),
        "mutex_model_reason": mutex_model_reason,
        "price_condition_checked": bool(price_condition_checked),
        "price_condition_pass": price_condition_pass,
        "max_price_violation_yuan_per_kwh": max_price_violation_yuan_per_kwh,
        "carbon_adder_yuan_per_kwh": carbon_adder_yuan_per_kwh,
        "stage_role": stage_role,
        "problem_type": "MILP" if use_buy_sell_binary else "LP",
        "objective_type": objective_type,
        "n_time_steps": T,
        "n_months": n_month_var,
        "model_build_time_sec": model_build_time_sec,
        "time_limit_sec": solver_cfg.time_limit_sec,
        "mip_gap": solver_cfg.mip_gap,
        "presolve": solver_cfg.presolve,
        "tee": solver_cfg.tee,
        **size_info,
    }

    P_h2_val = np.full(T, np.nan, dtype=float)
    P_sell_val = np.full(T, np.nan, dtype=float)
    P_buy_val = np.full(T, np.nan, dtype=float)
    P_gen_val = np.full(T, np.nan, dtype=float)
    Dm_val = np.full(n_month_var, np.nan, dtype=float)
    u_val = np.full(T, np.nan, dtype=float)
    solve_time = 0.0
    solution_extract_time_sec = 0.0

    try:
        solver, factory_name = _create_solver(solver_cfg)
        diagnostics["factory_name"] = factory_name

        t0 = time.perf_counter()
        try:
            result = solver.solve(
                model,
                tee=solver_cfg.tee,
                load_solutions=solver_cfg.load_solutions,
                symbolic_solver_labels=solver_cfg.symbolic_solver_labels,
            )
        except TypeError:
            result = solver.solve(
                model,
                tee=solver_cfg.tee,
                load_solutions=solver_cfg.load_solutions,
            )

        solve_time = time.perf_counter() - t0

        status = str(result.solver.status)
        termination = str(result.solver.termination_condition)
        diagnostics["solver_status"] = status
        diagnostics["termination_condition"] = termination

        ok_terms = {
            TerminationCondition.optimal,
            TerminationCondition.feasible,
            TerminationCondition.locallyOptimal,
            TerminationCondition.globallyOptimal,
        }

        if result.solver.status in {SolverStatus.ok, SolverStatus.warning} and result.solver.termination_condition in ok_terms:
            diagnostics["problem"] = 0
            diagnostics["info"] = f"{status} / {termination}"

            extract_start_time = time.perf_counter()
            for t in model.T:
                P_h2_val[t - 1] = float(value(model.P_h2[t]))
                P_sell_val[t - 1] = float(value(model.P_sell[t]))
                P_buy_val[t - 1] = float(value(model.P_buy[t]))
                P_gen_val[t - 1] = float(value(model.P_gen[t]))
                if use_buy_sell_binary:
                    u_val[t - 1] = float(value(model.u[t]))

            for m in model.M:
                idx = month_of_t == m
                if np.any(idx):
                    Dm_val[m - 1] = max(0.0, float(np.nanmax(P_buy_val[idx])))

            solution_extract_time_sec = time.perf_counter() - extract_start_time
            diagnostics.update(_solution_metrics(
                P_h2_val, P_gen_val, P_buy_val, P_sell_val, Dm_val, pi_buy, pi_sell, C_cap_month, Dm_max, month_of_t, dt,
                h2_kg_per_mwh, k_H, alpha_year, CE_limit, EF, H_done_kg, E_gen_done_mwh, E_sell_done_mwh,
                CO2_done_kg, H_rem_target, H2_price, carbon_price, O2_byproduct_Nm3_per_Nm3_H2,
                O2_price_yuan_per_Nm3, P_pre, P_h2_min, P_h2_max, use_buy_sell_binary, force_zero_sell, mode_lc,
            ))

            if _INTERNAL_ENABLE_BUY_SELL_OVERLAP_LOG_CHECK and logger is not None:
                finite_mask = np.isfinite(P_buy_val) & np.isfinite(P_sell_val)
                if np.any(finite_mask):
                    overlap = np.minimum(
                        np.maximum(P_buy_val[finite_mask], 0.0),
                        np.maximum(P_sell_val[finite_mask], 0.0),
                    )
                    max_overlap_mw = float(np.max(overlap)) if overlap.size > 0 else 0.0
                    sum_overlap_mwh = float(np.sum(overlap) * dt) if overlap.size > 0 else 0.0
                    num_overlap_hours = int(np.sum(overlap > _INTERNAL_BUY_SELL_OVERLAP_TOL_MW))
                else:
                    max_overlap_mw = float("nan")
                    sum_overlap_mwh = float("nan")
                    num_overlap_hours = 0

                _log_verbose(logger, "购售电同时发生检查 enabled = %s", _INTERNAL_ENABLE_BUY_SELL_OVERLAP_LOG_CHECK)
                _log_verbose(logger, "购售电同时发生检查 solve_mode = %s", solve_mode)
                _log_verbose(logger, "购售电同时发生检查 use_buy_sell_binary = %s", use_buy_sell_binary)
                _log_verbose(logger, "购售电同时发生检查 force_zero_sell = %s", force_zero_sell)
                _log_verbose(logger, "购售电同时发生检查 max_buy_sell_overlap_MW = %.12f", max_overlap_mw)
                _log_verbose(logger, "购售电同时发生检查 sum_buy_sell_overlap_MWh = %.12f", sum_overlap_mwh)
                _log_verbose(logger, "购售电同时发生检查 num_overlap_hours = %s", num_overlap_hours)
                diagnostics["buy_sell_overlap_max_MW"] = max_overlap_mw
                diagnostics["buy_sell_overlap_energy_MWh"] = sum_overlap_mwh
                diagnostics["buy_sell_overlap_hours_count"] = num_overlap_hours

                if np.isfinite(max_overlap_mw) and max_overlap_mw > _INTERNAL_BUY_SELL_OVERLAP_TOL_MW:
                    logger.warning(
                        "购售电同时发生检查发现明显 overlap：max_overlap=%.12f MW, num_overlap_hours=%s",
                        max_overlap_mw,
                        num_overlap_hours,
                    )

        else:
            diagnostics["problem"] = 1
            diagnostics["info"] = f"{status} / {termination}"

    except Exception as exc:
        solve_time = 0.0
        diagnostics["problem"] = 2
        diagnostics["info"] = f"exception: {exc}"
        diagnostics["solver_status"] = "exception"
        diagnostics["termination_condition"] = str(exc)

    diagnostics["solver_solve_time_sec"] = float(solve_time)
    diagnostics["solution_extract_time_sec"] = float(solution_extract_time_sec)
    diagnostics["stage_total_time_sec"] = float(time.perf_counter() - stage_start_time)
    _log_stage_verbose_report(logger, solve_mode, diagnostics)

    return {
        "P_h2": P_h2_val,
        "P_sell": P_sell_val,
        "P_buy": P_buy_val,
        "P_gen": P_gen_val,
        "Dm": Dm_val,
        "u": u_val,
        "diagnostics": diagnostics,
        "solve_time": solve_time,
    }
