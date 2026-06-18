# -*- coding: utf-8 -*-
"""
generate_effect_figures.py
通用专利"效果对比图"黑白生成脚本模板。

定位：
- 仅用于将【真实数据】绘制成符合专利要求的黑白/灰度效果图。
- 数据来源应为 method_code 中用户方法的真实运行输出（CSV 等）。
- 严禁用随机数/造数据冒充真实仿真结果（见 SKILL.md 第十一节）。

数据约定（通用 CSV）：
- 第一列为横轴（如时间步/迭代次数/样本编号），列名任意；
- 其余每一列为一条曲线，列名即图例名称（如"本发明方法""对比方法""传统方法"）。
- 若你的数据格式不同，请改写 load_csv() 适配，方法见同目录 README.md。

用法：
    python generate_effect_figures.py --csv ../method_code/outputs/result.csv \
        --x-label 迭代次数 --y-label 目标函数值 \
        --out ../outputs/figures/图3_效果对比图.png

TODO（按需扩展）：
- 多子图、双纵轴、误差带、对数坐标等可在此模板上增补。
"""

import argparse
import os
import sys

import matplotlib
matplotlib.use("Agg")  # 无显示环境也可出图
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figure_style_config import (  # noqa: E402
    apply_patent_style, style_for, FILL_GRAY,
)


def load_csv(csv_path):
    """读取通用 CSV：返回 (x, headers, columns)。
    不依赖 pandas，使用标准库 csv，便于在精简环境运行。
    """
    import csv
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if r and any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("CSV 数据行不足，无法绘图。请检查 method_code 真实输出。")
    headers = rows[0]
    data = rows[1:]

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")

    x = [to_float(r[0]) for r in data]
    columns = []
    for col_idx in range(1, len(headers)):
        col = [to_float(r[col_idx]) if col_idx < len(r) else float("nan") for r in data]
        columns.append((headers[col_idx], col))
    return x, headers, columns


def plot_effect_figure(csv_path, out_path, x_label="", y_label="",
                       title="", band_columns=None):
    """绘制黑白效果对比图。
    band_columns: 可选，(下界列名, 上界列名)，将以灰色填充表示约束范围/允许区间。
    """
    apply_patent_style()
    x, headers, columns = load_csv(csv_path)

    fig, ax = plt.subplots(figsize=(6, 4))

    # 灰色填充带（如有）
    col_map = {name: vals for name, vals in columns}
    drawn_band_names = set()
    if band_columns:
        lo_name, hi_name = band_columns
        if lo_name in col_map and hi_name in col_map:
            ax.fill_between(x, col_map[lo_name], col_map[hi_name],
                            label="约束范围", **FILL_GRAY)
            drawn_band_names.update({lo_name, hi_name})

    # 曲线：黑色 + 不同线型/marker
    idx = 0
    for name, vals in columns:
        if name in drawn_band_names:
            continue
        n = max(len(x), 1)
        markevery = max(1, n // 12)  # 控制标记密度，避免过密
        ax.plot(x, vals, label=name, markevery=markevery, **style_for(idx))
        idx += 1

    if x_label:
        ax.set_xlabel(x_label)
    if y_label:
        ax.set_ylabel(y_label)
    if title:
        ax.set_title(title)
    ax.legend(loc="best")
    ax.grid(True, color="0.85", linestyle=":", linewidth=0.6)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def main():
    p = argparse.ArgumentParser(description="生成专利黑白效果对比图（基于真实CSV数据）")
    p.add_argument("--csv", required=True, help="真实数据 CSV 路径")
    p.add_argument("--out", required=True, help="输出 PNG 路径（建议 outputs/figures/ 下）")
    p.add_argument("--x-label", default="", help="横轴标签")
    p.add_argument("--y-label", default="", help="纵轴标签")
    p.add_argument("--title", default="", help="图标题（可留空，专利附图常不写标题）")
    p.add_argument("--band-lo", default=None, help="约束范围下界列名（可选）")
    p.add_argument("--band-hi", default=None, help="约束范围上界列名（可选）")
    args = p.parse_args()

    if not os.path.exists(args.csv):
        print("[错误] 未找到数据文件：%s" % args.csv)
        print("提示：本脚本只绘制真实数据，严禁伪造仿真结果。")
        print("若暂无 method_code 真实输出，请先生成附图设计方案，待有数据后再出效果图。")
        sys.exit(1)

    band = (args.band_lo, args.band_hi) if (args.band_lo and args.band_hi) else None
    out = plot_effect_figure(args.csv, args.out, args.x_label, args.y_label,
                             args.title, band)
    print("[完成] 已生成黑白效果图：%s" % out)


if __name__ == "__main__":
    main()
