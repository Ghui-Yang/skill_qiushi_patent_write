# -*- coding: utf-8 -*-
"""
Check a patent Markdown draft for core structure, strict LaTeX formula hygiene,
formula numbering, formula references, and basic claim numbering.

This script is intentionally conservative: it reports risks and likely issues.
It does not replace the human/agent format review required by SKILL.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_HEADINGS = [
    "说 明 书 摘 要",
    "权 利 要 求 书",
    "说 明 书",
    "技术领域",
    "背景技术",
    "发明内容",
    "附图说明",
    "具体实施方式",
    "说 明 书 附 图",
]

BAD_REFERENCE_PATTERNS = [
    r"公式\s*\(",
    r"公式\s*[一二三四五六七八九十]+",
    r"式\s*\(\s*\d+\s*\)",
]

UNICODE_SUP_SUB = re.compile(r"[\u2070-\u209f]")
TAG_RE = re.compile(r"\\tag\{(\d+)\}")
CLAIM_RE = re.compile(r"(?m)^(\d+)\.\s+")
FIG_RE = re.compile(r"图(\d+)")


def extract_display_math(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    lines = text.splitlines()
    in_block = False
    start_line = 0
    buf: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if line.strip() == "$$":
            if not in_block:
                in_block = True
                start_line = idx
                buf = []
            else:
                blocks.append((start_line, "\n".join(buf)))
                in_block = False
                buf = []
            continue
        if in_block:
            buf.append(line)
    if in_block:
        blocks.append((start_line, "\n".join(buf) + "\n[UNCLOSED_DISPLAY_MATH]"))
    return blocks


def extract_display_math_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    lines = text.splitlines()
    in_block = False
    start_line = 0
    buf: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if line.strip() == "$$":
            if not in_block:
                in_block = True
                start_line = idx
                buf = []
            else:
                spans.append((start_line, idx, "\n".join(buf)))
                in_block = False
                buf = []
            continue
        if in_block:
            buf.append(line)
    if in_block:
        spans.append((start_line, len(lines), "\n".join(buf) + "\n[UNCLOSED_DISPLAY_MATH]"))
    return spans


def strip_display_math(text: str) -> str:
    return re.sub(r"\$\$.*?\$\$", "", text, flags=re.S)


def check_balanced_inline_dollars(text: str) -> list[str]:
    issues: list[str] = []
    text_without_blocks = strip_display_math(text)
    for idx, line in enumerate(text_without_blocks.splitlines(), start=1):
        if line.strip().startswith("```"):
            continue
        count = line.count("$")
        if count % 2 != 0:
            issues.append(f"L{idx}: 行内公式 `$` 数量为奇数，可能未闭合。")
    return issues


def check_latex_blocks(blocks: list[tuple[int, str]]) -> list[str]:
    issues: list[str] = []
    tags: list[int] = []
    for line_no, block in blocks:
        if "[UNCLOSED_DISPLAY_MATH]" in block:
            issues.append(f"L{line_no}: 独立公式 `$$` 未闭合。")
            continue
        left_count = block.count(r"\left")
        right_count = block.count(r"\right")
        if left_count != right_count:
            issues.append(f"L{line_no}: `\\left` 与 `\\right` 数量不匹配。")
        begins = re.findall(r"\\begin\{([^}]+)\}", block)
        ends = re.findall(r"\\end\{([^}]+)\}", block)
        if begins != ends:
            issues.append(f"L{line_no}: `\\begin{{}}` 与 `\\end{{}}` 环境不匹配。")
        for tag in TAG_RE.findall(block):
            tags.append(int(tag))
    if tags:
        expected = list(range(1, len(tags) + 1))
        if tags != expected:
            issues.append(
                "公式编号不连续或有重复/跳号："
                f"实际 {tags}，期望 {expected}。"
            )
    return issues


def check_formula_references(text: str) -> list[str]:
    issues: list[str] = []
    for pattern in BAD_REFERENCE_PATTERNS:
        for match in re.finditer(pattern, text):
            line_no = text.count("\n", 0, match.start()) + 1
            issues.append(
                f"L{line_no}: 公式引用写法不统一，建议改为“如式（1）所示”。"
            )
    tags = [int(x) for x in TAG_RE.findall(text)]
    refs = [int(x) for x in re.findall(r"式（(\d+)）", text)]
    missing_refs = sorted(set(tags) - set(refs))
    if missing_refs:
        issues.append(f"以下公式编号未发现正文引用：{missing_refs}。")
    unknown_refs = sorted(set(refs) - set(tags))
    if unknown_refs:
        issues.append(f"以下正文引用未发现对应 `\\tag{{}}`：{unknown_refs}。")
    return issues


def check_structure(text: str) -> list[str]:
    issues: list[str] = []
    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            issues.append(f"缺少固定章节：{heading}")
    if "其特征在于" not in text:
        issues.append("权利要求中未发现“其特征在于”。")
    if re.search(r"本文|本论文|我们|我方", text):
        issues.append("发现论文式或非专利用语：本文/本论文/我们/我方。")
    if re.search(r"世界领先|革命性|极大提升|非常先进", text):
        issues.append("发现商业宣传或夸大性用语。")
    return issues


def check_claim_numbers(text: str) -> list[str]:
    issues: list[str] = []
    claims = [int(x) for x in CLAIM_RE.findall(text)]
    if claims:
        expected = list(range(1, len(claims) + 1))
        if claims != expected:
            issues.append(f"权利要求编号不连续：实际 {claims}，期望 {expected}。")
    return issues


def check_figures(text: str) -> list[str]:
    issues: list[str] = []
    figs = [int(x) for x in FIG_RE.findall(text)]
    if figs:
        unique = sorted(set(figs))
        expected = list(range(1, max(unique) + 1))
        if unique != expected:
            issues.append(f"图号可能跳号：实际 {unique}，期望 {expected}。")
    return issues


def check_variable_explanations(text: str) -> list[str]:
    issues: list[str] = []
    blocks = extract_display_math_spans(text)
    lines = text.splitlines()
    for start_line, end_line, block in blocks:
        if TAG_RE.search(block):
            after = "\n".join(lines[end_line : min(len(lines), end_line + 8)])
            if "其中" not in after and "式中" not in after:
                issues.append(f"L{start_line}: 编号公式结束后 8 行内未发现变量解释。")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", default="outputs/patent_draft.md", help="Markdown file to check")
    args = parser.parse_args()

    path = Path(args.md)
    if not path.exists():
        print(f"[ERROR] 未找到 Markdown 文件：{path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    blocks = extract_display_math(text)
    issues: list[str] = []
    issues.extend(check_structure(text))
    issues.extend(check_balanced_inline_dollars(text))
    issues.extend(check_latex_blocks(blocks))
    issues.extend(check_formula_references(text))
    issues.extend(check_variable_explanations(text))
    issues.extend(check_claim_numbers(text))
    issues.extend(check_figures(text))
    if UNICODE_SUP_SUB.search(text):
        issues.append("发现 Unicode 上下标字符，请改为严格 LaTeX，例如 `$x_1$`、`$x^2$`。")

    if issues:
        print("[FAIL] patent Markdown check found issues:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print("[PASS] patent Markdown check passed core structure and LaTeX checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
