# figure_generation —— 专利黑白附图生成

本目录用于生成符合中国专利要求的**黑白/灰度**附图。

## 文件
- `figure_style_config.py`：数据曲线图统一黑白样式（全黑/灰、线型+marker、灰度填充、300dpi；按文件路径稳健注册中文字体，避免缺字）。
- `generate_effect_figures.py`：通用"效果对比图"生成脚本（**只画真实数据**）。
- `generate_diagrams.py`：**基于 Graphviz(dot) 的框图生成器**（流程图/结构框图/数据流图）。自动布局、连线整齐、不重叠，比手摆 matplotlib 方框美观得多。仅需系统 `dot` 可执行程序，无需 graphviz 的 python 包。

## 工具分工（重要）
- 流程图 / 结构框图 / 数据流图 / 控制回路图 → **优先 `generate_diagrams.py`（Graphviz）**；无 `dot` 时回退 matplotlib，但必须保证不重叠、不溢出、不缺字。
- 数据曲线 / 对比图 / 区间图 / 柱状图（效果图）→ `generate_effect_figures.py` + `figure_style_config.py`（matplotlib），且只画真实数据。
- 生成每张图后**务必实际查看图片**，确认无重叠/溢出/缺字、黑白清晰，再写入 `outputs/figures/` 并在 `outputs/patent_draft.md` 中引用。

## 强制规则
- 所有图必须黑白/灰度，禁止彩色；不得用颜色表达关键信息。
- 曲线靠线型（实线/虚线/点划线）、marker（圆/方/三角）、灰度填充区分。
- 效果图/对比图/结果图**必须基于 `method_code/` 的真实运行数据**，严禁造假（详见 SKILL.md 第十一节）。
- 流程图/结构图/示意图属设计性附图，不依赖数据，可直接绘制（可用 matplotlib、graphviz、或在 md 中用文字框图描述后转图）。

## 用法（效果对比图）
1. 确认有真实数据 CSV（来自 method_code 运行输出）。
   - 第一列为横轴；其余每列一条曲线，列名即图例。
2. 运行：
   ```bash
   python generate_effect_figures.py \
     --csv ../../../../outputs/run_results/result.csv \
     --x-label 迭代次数 --y-label 目标函数值 \
     --out ../../../../outputs/figures/图3_效果对比图.png
   ```
3. 如需"约束范围/允许区间"灰色填充带：
   ```bash
   python generate_effect_figures.py --csv ../../../../outputs/run_results/data.csv \
     --band-lo 下界 --band-hi 上界 --out ../../../../outputs/figures/图3.png
   ```

## 适配自定义数据格式
若数据不是上述通用 CSV（如 json、npz、多 sheet xlsx、模型 log），改写 `generate_effect_figures.py` 中的 `load_csv()`，使其返回 `(x, headers, columns)` 即可，其中：
- `x`：横轴数值列表；
- `columns`：`[(曲线名, 数值列表), ...]`。
其余绘图与样式逻辑无需改动。

## 中文字体
脚本已在 `figure_style_config.py` 中配置常见中文字体回退（SimSun/SimHei/微软雅黑/Songti/Noto CJK）。若仍出现方块，请安装上述任一字体，或在脚本中显式指定本机可用字体路径。

## 依赖
- Python 3，matplotlib。安装：`pip install matplotlib`。

> TODO：如需流程图/结构框图的自动生成模板（graphviz / matplotlib 画框线），可在本目录另加脚本扩展。
