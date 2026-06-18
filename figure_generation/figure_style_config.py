# -*- coding: utf-8 -*-
"""
figure_style_config.py
专利附图统一黑白/灰度样式配置（数据曲线类用）。

要点：
- 全黑/灰阶，禁止彩色；靠线型 + marker + 灰度填充区分曲线；
- 按文件路径稳健注册中文字体，杜绝方块/缺字；
- axes.unicode_minus=False，负号用普通连字符，避免缺字；
- 不要在图里使用上标数字(²/⁶)、Unicode 减号(−)等可能缺字的字符；
- 300dpi，bbox 紧凑。
"""

import os
import matplotlib
import matplotlib.font_manager as fm


# ---- 中文字体稳健注册：优先按 Windows 字体文件路径注册 ----
_CJK_FONT_CANDIDATES = [
    ("SimSun", r"C:/Windows/Fonts/simsun.ttc"),
    ("SimHei", r"C:/Windows/Fonts/simhei.ttf"),
    ("Microsoft YaHei", r"C:/Windows/Fonts/msyh.ttc"),
    ("KaiTi", r"C:/Windows/Fonts/simkai.ttf"),
    ("FangSong", r"C:/Windows/Fonts/simfang.ttf"),
]


def register_cjk_font():
    """注册可用中文字体，返回字体名。找不到则返回 None（由调用方决定回退）。"""
    chosen = None
    for name, path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                fm.fontManager.addfont(path)
                # addfont 后，真实 family 名以文件内部为准；用 FontProperties 取回
                real = fm.FontProperties(fname=path).get_name()
                chosen = real or name
                break
            except Exception:
                continue
    return chosen


CJK_FONT = register_cjk_font() or "DejaVu Sans"

PATENT_RCPARAMS = {
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "font.size": 12,
    "axes.linewidth": 1.0,
    "axes.edgecolor": "black",
    "axes.labelcolor": "black",
    "axes.titlecolor": "black",
    "xtick.color": "black",
    "ytick.color": "black",
    "text.color": "black",
    "lines.linewidth": 1.6,
    "lines.markersize": 6,
    "legend.frameon": True,
    "legend.edgecolor": "black",
    "legend.fontsize": 10,
    "axes.labelsize": 12,
    "font.sans-serif": [CJK_FONT, "DejaVu Sans"],
    "font.family": "sans-serif",
    "axes.unicode_minus": False,
}

# 曲线循环样式：颜色一律黑/灰，靠线型 + marker 区分
LINE_STYLES = [
    {"color": "black", "linestyle": "-",   "marker": "o"},   # 本发明方法
    {"color": "black", "linestyle": "--",  "marker": "s"},   # 对比方法
    {"color": "black", "linestyle": "-.",  "marker": "^"},   # 传统方法
    {"color": "black", "linestyle": ":",   "marker": "D"},   # 备用
    {"color": "dimgray", "linestyle": "-", "marker": "v"},   # 备用(灰)
]

FILL_GRAY = {"facecolor": "0.85", "edgecolor": "black", "alpha": 1.0}

BAR_FILLS = [
    {"facecolor": "white", "edgecolor": "black", "hatch": ""},
    {"facecolor": "0.75",  "edgecolor": "black", "hatch": ""},
    {"facecolor": "white", "edgecolor": "black", "hatch": "///"},
    {"facecolor": "white", "edgecolor": "black", "hatch": "..."},
    {"facecolor": "0.5",   "edgecolor": "black", "hatch": ""},
]


def apply_patent_style():
    matplotlib.rcParams.update(PATENT_RCPARAMS)


def style_for(index):
    return LINE_STYLES[index % len(LINE_STYLES)]


def bar_fill_for(index):
    return BAR_FILLS[index % len(BAR_FILLS)]
