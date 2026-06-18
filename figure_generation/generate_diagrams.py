# -*- coding: utf-8 -*-
"""
generate_diagrams.py
基于 Graphviz(dot) 的专利黑白框图生成器（流程图 / 结构框图 / 数据流图等）。

为什么用 Graphviz：
- 自动布局、连线整齐、不重叠，专利框图美观度远胜手工摆放的 matplotlib 方框。

依赖：
- 仅需系统可执行程序 `dot`（Graphviz）。不需要 graphviz 的 python 包。
- 若找不到 dot，render() 会抛出明确错误，由调用方回退到 matplotlib。

通用约束（与专利附图规则一致）：
- 全黑白：节点白底黑框黑字，连线黑色；
- 中文字体显式指定（默认 SimSun）；
- 不使用上标/Unicode 减号等易缺字字符；
- 输出 300dpi PNG。

可作为库导入，也可命令行用 --demo 自测。
"""

import os
import shutil
import subprocess
import tempfile


# 常见 dot 路径（PATH 找不到时兜底搜索）
_DOT_FALLBACKS = [
    r"D:/D/Anaconda/Library/bin/dot.exe",
    r"C:/Program Files/Graphviz/bin/dot.exe",
    r"C:/Program Files (x86)/Graphviz/bin/dot.exe",
]


def find_dot():
    """返回可用的 dot 可执行路径；找不到返回 None。"""
    p = shutil.which("dot")
    if p:
        return p
    for cand in _DOT_FALLBACKS:
        if os.path.exists(cand):
            return cand
    return None


def render(dot_source, out_png, dpi=300):
    """把 dot 源码渲染为 PNG。成功返回 out_png，失败抛异常。"""
    dot = find_dot()
    if dot is None:
        raise RuntimeError(
            "未找到 Graphviz 的 dot 可执行程序。请安装 Graphviz，"
            "或回退到 matplotlib 方式生成框图。"
        )
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".dot", delete=False,
                                     encoding="utf-8") as f:
        f.write(dot_source)
        dot_path = f.name
    try:
        subprocess.run([dot, "-Tpng", "-Gdpi=%d" % dpi, dot_path, "-o", out_png],
                       check=True)
    finally:
        try:
            os.remove(dot_path)
        except OSError:
            pass
    return out_png


def _esc(text):
    """转义并把换行转成 dot 的 \\n。"""
    text = text.replace('"', r'\"')
    text = text.replace("\n", r"\n")
    return text


# 黑白通用头：白底黑框黑字，中文字体
def _header(fontname, rankdir, fontsize=12):
    return (
        "digraph G {\n"
        '  bgcolor="white";\n'
        '  rankdir=%s;\n'
        '  node [shape=box, style="rounded,filled", fillcolor="white", '
        'color="black", fontcolor="black", fontname="%s", fontsize=%d, '
        'margin="0.18,0.10"];\n'
        '  edge [color="black", arrowsize=0.8, fontname="%s", fontcolor="black"];\n'
        % (rankdir, fontname, fontsize, fontname)
    )


def flowchart(steps, out_png, fontname="SimSun", rankdir="TB",
              highlight_last=False, fontsize=12, dpi=300):
    """竖直流程图。
    steps: [str, ...] 每个元素是一个步骤框文本（可含 \n 换行）。
    highlight_last: 末框是否灰底（常用于"输出/回退"框）。
    """
    s = _header(fontname, rankdir, fontsize)
    n = len(steps)
    for i, text in enumerate(steps):
        fill = 'gray90' if (highlight_last and i == n - 1) else '"white"'
        s += '  n%d [label="%s", fillcolor=%s];\n' % (i, _esc(text), fill)
    for i in range(n - 1):
        s += "  n%d -> n%d;\n" % (i, i + 1)
    s += "}\n"
    return render(s, out_png, dpi=dpi)


def _column_label(title, items):
    """把"标题 + 多条目"组装成左对齐的多行节点标签。
    使用 \\l 实现左对齐；条目前加项目符号。
    """
    parts = [title, "————————"]
    parts += ["· " + it for it in items]
    # 每行以 \l 结尾表示左对齐
    return "".join(p + r"\l" for p in parts)


def three_column_diagram(left_title, left_items, center_title, center_lines,
                         right_title, right_items, out_png,
                         fontname="SimSun", fontsize=12, dpi=300,
                         bottom_bar=None):
    """三栏结构框图：左(决策变量/输入) - 中(目标/核心) - 右(约束/输出)。
    每一栏渲染为"单个多行文本框"，左右排列，dot 连线；
    该写法稳定不重叠、整图紧凑、专业，适合专利结构示意图。
    bottom_bar: 可选，底部一条流程说明框（置于中框下方）。
    """
    s = _header(fontname, "LR", fontsize)
    s += '  graph [splines=true, nodesep=0.4, ranksep=0.9];\n'

    s += '  L [label="%s", fillcolor="white"];\n' % _column_label(left_title, left_items)
    center_label = "".join(p + r"\n" for p in [center_title, "————————"]) + r"\n".join(center_lines)
    s += '  C [label="%s", fillcolor=gray95];\n' % _esc(center_label)
    s += '  R [label="%s", fillcolor="white"];\n' % _column_label(right_title, right_items)

    s += '  L -> C;\n  C -> R;\n'

    if bottom_bar:
        s += '  B [label="%s", fillcolor=gray92, shape=box];\n' % _esc(bottom_bar)
        s += '  C -> B;\n'
        # 让 B 位于底部
        s += '  {rank=sink; B;}\n'
    s += "}\n"
    return render(s, out_png, dpi=dpi)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "demo_flow.png"
    flowchart(["步骤一：获取数据", "步骤二：建立模型", "步骤三：求解并输出"],
              out, highlight_last=True)
    print("demo 已生成:", out, "| dot=", find_dot())
