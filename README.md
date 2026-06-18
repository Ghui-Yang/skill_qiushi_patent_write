# patent_writer —— 通用中国发明专利撰写 skill

根据用户提供的**任意技术方案**，自动按中国发明专利申请文件的常见八股格式，生成专利名称、说明书摘要、权利要求书、说明书、附图说明、附图建议、黑白仿真效果图与 Markdown 专利草稿。

> ⚠️ 本 skill 是**通用**工具，**不绑定任何行业**。参考专利仅用于学习格式/句式/结构，绝不迁移其技术内容。详见 `SKILL.md`。

## 用途
把技术资料（技术交底书 / 论文 / 方案说明 / 代码）转写为规范的中国发明专利申请文本。

## 安装方式

Codex 使用时，将本目录复制到当前项目或用户技能目录：

```text
.agents/skills/patent_writer/
```

Claude Code 使用时，将本目录复制到：

```text
.claude/skills/patent_writer/
```

两边同时使用时，保持两处 `SKILL.md`、`templates/`、`scripts/`、`figure_generation/` 等长期复用文件一致。

## 输入材料要求
尽量提供：发明类型、技术领域、现有技术与不足、待解决问题、核心技术方案、关键技术特征、输入/处理/输出、技术效果、公式与变量、是否有附图、是否有可运行代码与数据。
信息不足时，skill 会先列"缺失信息清单"；若要求强制出稿，会用 `【待补充：…】` 占位并登记。

## 输出文件（写入当前工作目录的 outputs/）
- `outputs/patent_draft.md`：最终 Markdown 专利草稿
- `outputs/figures/`：必要黑白/灰度附图
- `outputs/run_results/`：必要程序运行结果

最终不要保留审查报告、修订日志、自查表、调试日志或 Word 文件。运行中临时产生的文件应在结束前删除。skill 目录不要放 `outputs/`。

## 使用流程
1. 把技术资料发给 skill（或放入项目）。
2. skill 抽取信息 → 识别发明类型 → 列缺失项。
3. 生成标题、摘要、权利要求书、说明书、附图说明、具体实施方式 → 写入 `outputs/patent_draft.md`。
4. 内部执行格式/公式审查和专利性审查，直接修改 `outputs/patent_draft.md`。
5. 根据审查意见最多迭代 3 轮。
6. 附图：流程图/结构图可直接出到 `outputs/figures/`；效果图须有真实数据（见下）。
7. 结束前清理临时文件，仅保留 `outputs/patent_draft.md` 和必要程序运行结果。

## 如何放置参考专利
放入 `reference_examples/`。**仅用于学习格式/句式/章节/语言风格**，严禁迁移其技术内容、行业、变量、模型、公式、约束。

## 如何放置方法代码
放入 `method_code/`：源码→`src/`，数据→`data/`，依赖→`requirements.txt`。运行结果不要写入 skill 目录，统一写入当前工作目录的 `outputs/run_results/`。

## 如何生成仿真效果图（严禁造假）
1. 用 `method_code/` 中真实代码与数据运行，导出结果 CSV 到当前工作目录的 `outputs/run_results/`。
2. 运行：
   ```bash
   python figure_generation/generate_effect_figures.py \
     --csv outputs/run_results/result.csv \
     --x-label 迭代次数 --y-label 目标函数值 \
     --out outputs/figures/图3_效果对比图.png
   ```
3. 所有图为黑白/灰度，用线型+marker+灰度填充区分。
4. **无代码/无数据则不画效果图**，只给附图设计方案并说明原因。

## 如何检查 Markdown
```bash
python scripts/check_patent_markdown.py --md outputs/patent_draft.md
```

## 注意事项
- 不默认任何行业；技术内容只来自用户本次资料。
- 严禁论文用语（本文/本论文/我们）与商业宣传（极大提升/世界领先/革命性等）。
- 严禁伪造仿真数据。
- 信息不足处用 `【待补充：…】` 显式标注。
- 审查和修订过程可以临时记录，但最终必须清理临时文件。

## 目录
```
patent_writer/
├── SKILL.md                 # 主说明（核心规则）
├── README.md                # 本文件
├── templates/               # 草稿/权要/说明书/附图说明/自查表 模板
├── reference_examples/      # 参考专利（仅学格式）
├── method_code/             # 长期复用示例/适配代码；运行时用户代码优先留在当前工作目录
├── figure_generation/       # 黑白附图脚本
└── scripts/                 # 检查脚本
```
