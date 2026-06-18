# -*- coding: utf-8 -*-
"""
make_patent_figures.py
本案专用：生成本专利的 4 张黑白附图。
- 图1 方法流程图（Graphviz 设计性框图，不依赖数据）
- 图2 优化模型决策变量-目标-约束结构示意图（Graphviz，不依赖数据）
- 图3 逐月累计产氢量与年度目标对比图（matplotlib，基于真实运行结果）
- 图4 逐月剩余下发产氢目标与可达区间对比图（matplotlib，基于真实运行结果）

数据来源：method_code/outputs/fig3_cum_h2.csv、fig4_bounds.csv（由真实优化结果导出）。
严禁修改数值或以随机数替代真实结果。
"""
import os, sys, csv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figure_style_config import apply_patent_style, style_for, FILL_GRAY
import generate_diagrams as gd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # patent_writer/
DATA = os.path.join(BASE, "method_code", "outputs")
OUT = os.path.join(BASE, "outputs", "figures")
os.makedirs(OUT, exist_ok=True)
FONT = "SimSun"


def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    head = rows[0]
    cols = {h: [] for h in head}
    for r in rows[1:]:
        for h, v in zip(head, r):
            cols[h].append(float(v))
    return cols


# ---------------------------------------------------------------- 图1 流程图（Graphviz）
def fig1_flowchart():
    steps = [
        "步骤一\n获取全年小时级新能源预测可用功率、上网电价、购电电价序列；\n按序列长度自动识别平年(8760h)/闰年(8784h)并生成月份索引",
        "步骤二\n获取已完成制氢/发电/购电/售电历史小时功率求和值；\n折算已完成产氢量与发用电量，确定剩余优化时段",
        "步骤三\n针对剩余时段构建优化模型（电力平衡、新能源接纳上限、\n月度下网深度上限/覆盖、购售电互斥、上网电量比例、\n单位制氢碳排放强度等约束）",
        "步骤四\n分别以产氢量最小化、最大化求解，\n得到最小可达产氢量与最大可达产氢量",
        "步骤五\n将剩余年度产氢目标与可达区间比较并修正，\n得到下发产氢目标",
        "步骤六\n以下发产氢目标为产氢量等式约束，\n以净成本最小化求解，得到小时级/月度级调度计划",
        "步骤七\n若经济优化失败则回退对应边界可行解；输出小时级/月度级\n调度计划与计划可下发标志，并将本月计划累加为历史求和值进入下一月",
    ]
    gd.flowchart(steps, os.path.join(OUT, "图1_方法流程图.png"),
                 fontname=FONT, highlight_last=True, fontsize=12)
    print("图1 OK (graphviz)")


# ---------------------------------------------------------------- 图2 结构图（Graphviz）
def fig2_structure():
    left = ["小时级新能源接纳功率 P_gen", "小时级制氢功率 P_h2",
            "小时级购电功率 P_buy", "小时级售电功率 P_sell",
            "月度最大下网深度 Dm", "购售电互斥二进制变量 u"]
    center = ["目标函数：净成本最小化",
              "= 购电成本 + 容量电费 + 碳排放成本",
              "  - 售电收益 - 制氢收益 - 副产氧收益"]
    right = ["电力平衡约束", "新能源接纳上限约束",
             "月度下网深度上限约束", "月度下网深度覆盖约束",
             "购售电互斥约束", "年度上网电量比例约束",
             "年度单位制氢碳排放强度约束"]
    bar = "求解流程：边界诊断(最小/最大可达产氢量)  ->  目标区间修正  ->  经济优化(失败则回退边界可行解)"
    gd.three_column_diagram("决策变量", left, "核心目标", center,
                            "约束条件", right,
                            os.path.join(OUT, "图2_模型结构示意图.png"),
                            fontname=FONT, fontsize=12, bottom_bar=bar)
    print("图2 OK (graphviz)")


# ---------------------------------------------------------------- 图3 累计产氢量（matplotlib）
def fig3_cum():
    apply_patent_style()
    c = read_csv(os.path.join(DATA, "fig3_cum_h2.csv"))
    m = c["月份"]
    cum = [v / 1e6 for v in c["逐月累计产氢量"]]
    tgt = [v / 1e6 for v in c["年度产氢目标"]]
    fig, ax = plt.subplots(figsize=(6.6, 4.3))
    ax.plot(m, cum, label="逐月累计产氢量", markevery=1, **style_for(0))
    ax.plot(m, tgt, label="年度产氢目标", **style_for(1))
    ax.set_xlabel("月份")
    ax.set_ylabel("产氢量 (单位: 百万kg)")
    ax.set_xticks(m)
    ax.legend(loc="best")
    ax.grid(True, color="0.85", linestyle=":", linewidth=0.6)
    fig.savefig(os.path.join(OUT, "图3_累计产氢量对比图.png"))
    plt.close(fig)
    print("图3 OK")


# ---------------------------------------------------------------- 图4 可达区间（matplotlib）
def fig4_bounds():
    apply_patent_style()
    c = read_csv(os.path.join(DATA, "fig4_bounds.csv"))
    m = c["月份"]
    tgt = [v / 1e6 for v in c["剩余下发产氢目标"]]
    lo = [v / 1e6 for v in c["最小可达产氢量"]]
    hi = [v / 1e6 for v in c["最大可达产氢量"]]
    fig, ax = plt.subplots(figsize=(6.6, 4.3))
    ax.fill_between(m, lo, hi, label="可达产氢量区间", **FILL_GRAY)
    ax.plot(m, hi, label="最大可达产氢量", **style_for(2))
    ax.plot(m, lo, label="最小可达产氢量", **style_for(3))
    ax.plot(m, tgt, label="剩余下发产氢目标", markevery=1, **style_for(0))
    ax.set_xlabel("月份")
    ax.set_ylabel("产氢量 (单位: 百万kg)")
    ax.set_xticks(m)
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, color="0.85", linestyle=":", linewidth=0.6)
    fig.savefig(os.path.join(OUT, "图4_可达区间对比图.png"))
    plt.close(fig)
    print("图4 OK")


if __name__ == "__main__":
    fig1_flowchart()
    fig2_structure()
    fig3_cum()
    fig4_bounds()
    print("全部附图已生成至:", OUT)
