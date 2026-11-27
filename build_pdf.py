#!/usr/bin/env python3
"""一键按 outline.yaml 顺序将拆分后的 Markdown（支持手写裁剪图与 LaTeX 公式/表格混排）合并排版为完整 PDF。

特性：
- 兼容手写裁剪图笔记与标准 LaTeX ($...$ / $$...$$) 富文本笔记混排
- 内置离线数学排版引擎：上下式分数线、带完整横线的全包根号、大括号联立方程组（\begin{cases}）
- 支持全部合并或按模块/小节筛选导出

用法示例:
    python3 build_pdf.py                              # 合并全部模块 -> output/解析几何小题合集.pdf
    python3 build_pdf.py --modules 01                 # 仅合并模块 01（直线与方程）
    python3 build_pdf.py --subsection "对称问题"       # 仅合并标题包含“对称问题”的小节
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import base64
import html
import mimetypes
import re
import sys
from pathlib import Path

import markdown
import yaml

REPO_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Built-in LaTeX -> Publication HTML/CSS/SVG Math Renderer (zero TeXLive needed)
# ---------------------------------------------------------------------------

_LEFT_BRACE_SVG = (
    '<svg class="m-brace-svg" viewBox="0 0 14 100" preserveAspectRatio="none" aria-hidden="true">'
    '<path d="M12,2 C6,2 6,8 6,20 L6,42 C6,47 3,50 1,50 C3,50 6,53 6,58 L6,80 C6,92 6,98 12,98" '
    'fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" '
    'vector-effect="non-scaling-stroke"/>'
    '</svg>'
)

_SQRT_HOOK_SVG = (
    '<svg class="m-sqrt-svg" viewBox="0 0 12 24" preserveAspectRatio="none" aria-hidden="true">'
    '<polyline points="0.5,14 3.5,12.5 6.2,22.5 11.5,1.2" '
    'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
    'vector-effect="non-scaling-stroke"/>'
    '</svg>'
)


def _extract_brace_arg(s: str, pos: int) -> tuple[str, int]:
    """Extracts {...} starting at pos (ignoring leading spaces), returns (inner, next_pos)."""
    n = len(s)
    while pos < n and s[pos] == " ":
        pos += 1
    if pos >= n or s[pos] != "{":
        if pos < n and s[pos] == "\\":
            m = re.match(r"\\[a-zA-Z]+", s[pos:])
            if m:
                return m.group(0), pos + len(m.group(0))
        return (s[pos] if pos < n else ""), min(pos + 1, n)
    depth = 0
    start = pos + 1
    i = pos
    while i < n:
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    return s[start:], n


def _render_latex_expr(tex: str) -> str:
    """Recursively converts a LaTeX math expression into HTML with stacked fractions,
    full-overbar square roots, and SVG curly-brace cases."""
    tex = tex.strip()
    out = []
    i = 0
    n = len(tex)

    while i < n:
        # 1. \begin{cases} ... \end{cases}
        if tex.startswith(r"\begin{cases}", i):
            end_idx = tex.find(r"\end{cases}", i)
            if end_idx != -1:
                inner = tex[i + len(r"\begin{cases}") : end_idx].strip()
                rows = [r.strip() for r in inner.split(r"\\") if r.strip()]
                row_html = "".join(
                    f'<span class="m-case-row">{_render_latex_expr(r)}</span>' for r in rows
                )
                out.append(
                    f'<span class="m-cases">'
                    f'<span class="m-brace-cell">{_LEFT_BRACE_SVG}</span>'
                    f'<span class="m-cases-rows">{row_html}</span>'
                    f'</span>'
                )
                i = end_idx + len(r"\end{cases}")
                continue

        # 2. \frac{num}{den}
        if tex.startswith(r"\frac", i) or tex.startswith(r"\dfrac", i):
            skip = 6 if tex.startswith(r"\dfrac", i) else 5
            num, next_i = _extract_brace_arg(tex, i + skip)
            den, next_i = _extract_brace_arg(tex, next_i)
            out.append(
                f'<span class="m-frac">'
                f'<span class="m-num">{_render_latex_expr(num)}</span>'
                f'<span class="m-den">{_render_latex_expr(den)}</span>'
                f'</span>'
            )
            i = next_i
            continue

        # 3. \sqrt{body}
        if tex.startswith(r"\sqrt", i):
            body, next_i = _extract_brace_arg(tex, i + 5)
            out.append(
                f'<span class="m-sqrt">'
                f'<span class="m-sqrt-hook">{_SQRT_HOOK_SVG}</span>'
                f'<span class="m-radicand">{_render_latex_expr(body)}</span>'
                f'</span>'
            )
            i = next_i
            continue

        # 3b. \text{...}
        if tex.startswith(r"\text", i):
            body, next_i = _extract_brace_arg(tex, i + 5)
            out.append(f'<span class="m-text">{html.escape(body)}</span>')
            i = next_i
            continue

        # 3c. \overrightarrow{...}
        if tex.startswith(r"\overrightarrow", i):
            body, next_i = _extract_brace_arg(tex, i + 15)
            out.append(f'<span class="m-vec">{_render_latex_expr(body)}</span>')
            i = next_i
            continue

        # 3d. \vec{...}
        if tex.startswith(r"\vec", i) and not tex.startswith(r"\overrightarrow", i):
            body, next_i = _extract_brace_arg(tex, i + 4)
            out.append(f'<span class="m-vec">{_render_latex_expr(body)}</span>')
            i = next_i
            continue

        # 4. \left( ... \right) or \left| ... \right|
        if tex.startswith(r"\left", i):
            j = i + 5
            if j < n:
                ch = tex[j]
                if ch in "([|":
                    out.append(f'<span class="m-big-delim">{html.escape(ch)}</span>')
                    i = j + 1
                    continue
        if tex.startswith(r"\right", i):
            j = i + 6
            if j < n:
                ch = tex[j]
                if ch in ")]|":
                    out.append(f'<span class="m-big-delim">{html.escape(ch)}</span>')
                    i = j + 1
                    continue

        # 5. Commands
        if tex[i] == "\\":
            # Spacing commands \; \, \quad \
            if tex.startswith(r"\qquad", i):
                out.append('<span class="m-quad"></span><span class="m-quad"></span>')
                i += 6
                continue
            if tex.startswith(r"\quad", i):
                out.append('<span class="m-quad"></span>')
                i += 5
                continue
            if i + 1 < n and tex[i + 1] in ";, ":
                out.append("&#8201;")
                i += 2
                continue
            m = re.match(r"\\([a-zA-Z]+)", tex[i:])
            if m:
                cmd = m.group(1)
                i += len(m.group(0))
                sym_map = {
                    "cdot": "&#183;",
                    "times": "&#215;",
                    "neq": "&#8800;",
                    "parallel": "&#8741;",
                    "nparallel": "&#8742;",
                    "perp": "&#8869;",
                    "Rightarrow": "&#8658;",
                    "therefore": "&#8756;",
                    "because": "&#8757;",
                    "alpha": "&#945;",
                    "beta": "&#946;",
                    "theta": "&#952;",
                    "pi": "&#960;",
                    "pm": "&#177;",
                    "le": "&#8804;",
                    "ge": "&#8805;",
                    "tan": '<span class="m-op">tan</span>',
                    "sin": '<span class="m-op">sin</span>',
                    "cos": '<span class="m-op">cos</span>',
                    "lambda": "&#955;",
                    "mu": "&#956;",
                    "triangle": "&#9651;",
                    "angle": "&#8736;",
                    "sim": "&#8765;",
                    "min": '<span class="m-op">min</span>',
                    "max": '<span class="m-op">max</span>',
                    "to": "&#8594;",
                    "circ": "&#176;",
                    "Delta": "&#916;",
                    "odot": "&#8857;",
                    "langle": "&#10216;",
                    "rangle": "&#10217;",
                    "mathbb": "",
                    "infty": "&#8734;",
                    "Leftrightarrow": "&#8660;",
                    "cong": "&#8773;",
                    "in": "&#8712;",
                    "gamma": "&#947;",
                    "varphi": "&#966;",
                    "ne": "&#8800;",
                    "leq": "&#8804;",
                    "geq": "&#8805;",
                    "cdots": "&#8943;",
                    "ldots": "&#8230;",
                    "iff": "&#10234;",
                    "Longrightarrow": "&#10233;",
                    "Leftarrow": "&#8656;",
                    "cot": '<span class="m-op">cot</span>',
                    "arctan": '<span class="m-op">arctan</span>',
                    "ln": '<span class="m-op">ln</span>',
                    "approx": "&#8776;",
                    "cup": "&#8746;",
                    "cap": "&#8745;",
                    "varnothing": "&#8709;",
                    "emptyset": "&#8709;",
                    "subset": "&#8834;",
                    "sigma": "&#963;",
                    "varphi": "&#966;",
                    "phi": "&#966;",
                    "omega": "&#969;",
                    "rho": "&#961;",
                    "delta": "&#948;",
                    "geqslant": "&#8805;",
                    "leqslant": "&#8804;",
                    "notin": "&#8713;",
                    "forall": "&#8704;",
                    "mp": "&#8723;",
                    "sum": "&#8721;",
                    "div": "&#247;",
                }
                if cmd in sym_map:
                    out.append(sym_map[cmd])
                else:
                    out.append(html.escape(cmd))
                continue

        # 6. Subscript _ and Superscript ^
        if tex[i] in "_^":
            tag = "sub" if tex[i] == "_" else "sup"
            arg, next_i = _extract_brace_arg(tex, i + 1)
            out.append(f"<{tag}>{_render_latex_expr(arg)}</{tag}>")
            i = next_i
            continue

        # 7. Plain characters (italicize Latin variables, keep digits/operators roman)
        ch = tex[i]
        if ch.isalpha() and ord(ch) < 128:
            out.append(f"<i>{ch}</i>")
        elif ch == "-":
            out.append("&#8722;")
        elif ch in "=+":
            out.append(f" {ch} ")
        else:
            out.append(html.escape(ch))
        i += 1

    return "".join(out)


def convert_markdown_with_math(md_text: str) -> str:
    """Protects $$...$$ and $...$ math blocks, runs Markdown->HTML, then restores rendered math."""
    placeholders = {}

    def _sub_block(m: re.Match) -> str:
        key = f"@@MATHBLOCK{len(placeholders)}@@"
        placeholders[key] = f'<div class="math-display">{_render_latex_expr(m.group(1))}</div>'
        return f"\n\n{key}\n\n"

    def _sub_inline(m: re.Match) -> str:
        key = f"@@MATHINLINE{len(placeholders)}@@"
        placeholders[key] = f'<span class="math-inline">{_render_latex_expr(m.group(1))}</span>'
        return key

    text = re.sub(r"\$\$(.+?)\$\$", _sub_block, md_text, flags=re.DOTALL)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _sub_inline, text)

    html_out = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    # ==重点== 语法：PDF 中渲染为红色加粗（可包含 $公式$）
    html_out = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r'<mark class="key-point">\1</mark>', html_out)

    for key, val in placeholders.items():
        html_out = html_out.replace(f"<p>{key}</p>", val)
        html_out = html_out.replace(key, val)
    return html_out


def encode_image_data_uri(img_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(img_path))
    if img_path.suffix.lower() == ".svg":
        mime = "image/svg+xml"
    elif not mime:
        mime = "image/jpeg"
    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


# ---------------------------------------------------------------------------
# 图片排版：手写长图按人工核对过的切点分段；自绘 SVG 按统一字号缩放
# ---------------------------------------------------------------------------

TEXT_WIDTH_MM = 180.0      # A4 宽 210mm − 左右页边距 15mm × 2
HW_MAX_H_MM = 245.0        # 单段手写图最大高度（一页版心约 263mm）
SVG_MM_PER_PX = 0.2625     # 自绘图里 12px 的标注 ≈ 正文字号的 0.85 倍
SVG_MAX_H_MM = 232.0
CUTS_FILE = REPO_ROOT / "handwritten_cuts.yaml"
_CUTS: dict | None = None


def _load_cuts() -> dict:
    global _CUTS
    if _CUTS is None:
        _CUTS = {}
        if CUTS_FILE.is_file():
            data = yaml.safe_load(CUTS_FILE.read_text(encoding="utf-8")) or {}
            for k, v in data.items():
                _CUTS[str(k)] = sorted(int(y) for y in (v or {}).get("cuts", []))
    return _CUTS


def _svg_size_px(svg_path: Path) -> tuple[float, float] | None:
    head = svg_path.read_text(encoding="utf-8", errors="ignore")[:2000]
    m = re.search(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)"', head)
    if m:
        return float(m.group(1)), float(m.group(2))
    mw = re.search(r'<svg[^>]*\swidth="([\d.]+)', head)
    mh = re.search(r'<svg[^>]*\sheight="([\d.]+)', head)
    if mw and mh:
        return float(mw.group(1)), float(mh.group(1))
    return None


def _raster_chunks(img_path: Path):
    """返回 [(data_uri, 宽px, 高px), ...]。按 handwritten_cuts.yaml 的切点切成几段，并压缩为 JPEG。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    import hashlib
    import io
    key = hashlib.sha1(img_path.read_bytes()).hexdigest()[:12]
    cuts = _load_cuts().get(key, [])
    im = Image.open(img_path).convert("RGB")
    w, h = im.size
    bounds = [0] + [y for y in cuts if 0 < y < h] + [h]
    out = []
    for top, bottom in zip(bounds, bounds[1:]):
        piece = im.crop((0, top, w, bottom))
        if w > 1400:
            piece = piece.resize((1400, round(piece.height * 1400 / w)), Image.LANCZOS)
        buf = io.BytesIO()
        piece.save(buf, "JPEG", quality=86, optimize=True)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        out.append((uri, piece.width, piece.height))
    return out


def render_image_html(img_abs: Path, alt: str) -> str:
    alt_attr = html.escape(alt)
    if img_abs.suffix.lower() == ".svg":
        size = _svg_size_px(img_abs)
        style = ""
        if size:
            w_mm, h_mm = size[0] * SVG_MM_PER_PX, size[1] * SVG_MM_PER_PX
            k = min(1.0, TEXT_WIDTH_MM / w_mm, SVG_MAX_H_MM / h_mm)
            style = f' style="width:{w_mm * k:.1f}mm"'
        return (f'<figure class="note-figure svg-fig"><img class="diagram-img"{style} '
                f'src="{encode_image_data_uri(img_abs)}" alt="{alt_attr}" /></figure>')
    chunks = _raster_chunks(img_abs)
    if not chunks:
        return (f'<figure class="note-figure handwritten-img-fig"><img class="handwritten-img" '
                f'src="{encode_image_data_uri(img_abs)}" alt="{alt_attr}" /></figure>')
    parts = []
    for uri, w, h in chunks:
        width = TEXT_WIDTH_MM
        if width * h / w > HW_MAX_H_MM:
            width = HW_MAX_H_MM * w / h
        parts.append(f'<figure class="hw-chunk"><img src="{uri}" alt="{alt_attr}" style="width:{width:.1f}mm" /></figure>')
    return '<div class="hw-group">' + "".join(parts) + "</div>"


def render_item_markdown(md_path: Path) -> str:
    """将单个知识点的 Markdown 渲染为 HTML（兼容纯裁剪图笔记与含 LaTeX 公式/表格的补充笔记）。"""
    raw_text = md_path.read_text(encoding="utf-8")
    lines = raw_text.splitlines()

    idx = 0
    while idx < len(lines) and (
        not lines[idx].strip()
        or lines[idx].strip().startswith("# ")
        or lines[idx].strip().startswith("> ")
    ):
        idx += 1
    body_lines = lines[idx:]

    non_empty = [l.strip() for l in body_lines if l.strip()]
    has_rich_md = any(
        l.startswith("##")
        or l.startswith("|")
        or l.startswith("- ")
        or l.startswith("$$")
        or re.match(r"^\d+\.\s", l)
        for l in non_empty
    )

    if not has_rich_md:
        parts = []
        for line in non_empty:
            img_match = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line)
            if img_match:
                alt, rel = img_match.group(1), img_match.group(2)
                img_abs = (md_path.parent / rel).resolve()
                if img_abs.is_file():
                    parts.append(render_image_html(img_abs, alt))
            else:
                rendered_note = convert_markdown_with_math(line)
                rendered_note = re.sub(r"^<p>|</p>$", "", rendered_note.strip())
                parts.append(f'<div class="topic-note">{rendered_note}</div>')
        return "\n".join(parts)

    body_md = "\n".join(body_lines)

    def _replace_img(m: re.Match) -> str:
        alt, rel = m.group(1), m.group(2)
        img_abs = (md_path.parent / rel).resolve()
        if img_abs.is_file():
            return render_image_html(img_abs, alt)
        return m.group(0)

    body_md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace_img, body_md)
    rendered = convert_markdown_with_math(body_md)
    return f'<div class="rich-content">{rendered}</div>'


BASE_CSS = r"""
    :root {
      --primary: #1e3a5f;
      --accent: #0f766e;
      --surface: #f8fafc;
      --surface-warm: #fffbeb;
      --border: #cbd5e1;
      --text: #1e293b;
      --muted: #475569;
      --bg: #ffffff;
    }

    @page {
      size: A4;
      margin: 16mm 15mm 18mm 15mm;
      background: var(--bg);
      @top-left {
        content: "__DOC_TITLE__";
        font-family: 'Noto Sans CJK SC', 'Inter', sans-serif;
        font-size: 9pt;
        color: var(--muted);
      }
      @bottom-right {
        content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
        font-family: 'Noto Sans CJK SC', 'Inter', sans-serif;
        font-size: 9pt;
        color: var(--muted);
        white-space: nowrap;
      }
    }

    @page :first {
      @top-left { content: none; }
    }

    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: transparent; }
    body {
      font-family: 'Noto Sans CJK SC', 'Inter', -apple-system, sans-serif;
      font-size: 10.5pt;
      line-height: 1.65;
      color: var(--text);
      letter-spacing: normal;
    }

    main.document-flow { width: 100%; }

    .hero-header {
      border-bottom: 3px solid var(--primary);
      padding-bottom: 12px;
      margin-bottom: 16px;
    }
    .hero-kicker {
      font-size: 9.5pt;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 4px;
    }
    h1 {
      font-size: 22pt;
      font-weight: 800;
      color: var(--primary);
      margin: 0 0 6px 0;
      line-height: 1.25;
    }
    .hero-subtitle { font-size: 10.5pt; color: var(--muted); margin: 0 0 8px 0; }
    .hero-stats {
      font-size: 10pt;
      color: var(--text);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 12px;
      display: inline-block;
    }

    .toc-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 18px;
    }
    .toc-heading {
      font-size: 12pt;
      font-weight: 700;
      color: var(--primary);
      margin: 0 0 8px 0;
      padding-bottom: 5px;
      border-bottom: 1px solid var(--border);
    }
    .toc-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
    .toc-module-title { font-size: 10.5pt; font-weight: 700; color: var(--primary); margin-bottom: 4px; }
    .toc-module-title a { color: var(--primary); text-decoration: none; }
    .toc-sublist { list-style: none; margin: 0; padding: 0; }
    .toc-sublist li {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      font-size: 10pt;
      padding: 3px 0;
      border-bottom: 1px dashed #e2e8f0;
    }
    .toc-sublist a { color: var(--text); text-decoration: none; }
    .toc-meta { font-size: 9.5pt; color: var(--muted); flex-shrink: 0; margin-left: 8px; }

    .module-section { margin-top: 16px; break-inside: auto; }
    .module-banner {
      background: var(--primary);
      color: #ffffff;
      border-radius: 6px;
      padding: 10px 16px;
      margin: 14px 0 12px 0;
      break-inside: avoid;
      break-after: avoid;
    }
    .module-tag { display: inline-block; font-size: 9pt; font-weight: 700; color: #bae6fd; margin-bottom: 2px; }
    .module-banner h2 { font-size: 15.5pt; font-weight: 700; color: #ffffff; margin: 0; line-height: 1.3; }

    h3.sub-heading {
      font-size: 13pt;
      font-weight: 700;
      color: var(--accent);
      margin: 14px 0 8px 0;
      padding-bottom: 4px;
      border-bottom: 1.5px solid #ccfbf1;
      break-after: avoid;
    }

    .topic-block { margin: 0 0 16px 0; break-inside: auto; }
    .topic-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 10px;
      background: var(--surface);
      border-left: 4px solid var(--primary);
      padding: 6px 10px;
      margin-bottom: 8px;
      border-radius: 0 4px 4px 0;
      break-after: avoid;
    }
    .topic-title { font-size: 11.5pt; font-weight: 700; color: var(--primary); }
    .topic-badge { font-size: 9.5pt; color: var(--muted); white-space: nowrap; flex-shrink: 0; }
    .topic-note {
      font-size: 10pt;
      color: #92400e;
      background: var(--surface-warm);
      border-left: 3px solid #d97706;
      padding: 6px 10px;
      margin: 0 0 6px 0;
      border-radius: 0 4px 4px 0;
      break-inside: avoid;
      break-after: avoid;
    }

    figure.note-figure {
      margin: 0 0 8px 0;
      padding: 4px;
      border: 1px solid #e2e8f0;
      border-radius: 4px;
      background: #ffffff;
      break-inside: avoid;
    }
    figure.note-figure img {
      display: block;
      width: 100%;
      max-height: 192mm;
      height: auto;
      object-fit: contain;
      margin: 0 auto;
    }

    /* ==重点== 标记 */
    mark.key-point { background: none; color: #c81e1e; font-weight: 700; padding: 0; }

    /* Rich Markdown */
    .rich-content { padding: 2px 4px 4px 4px; }
    .rich-content h2 {
      font-size: 11.5pt;
      font-weight: 700;
      color: var(--primary);
      margin: 12px 0 6px 0;
      padding-bottom: 2px;
      border-bottom: 1px solid #e2e8f0;
      break-after: avoid;
    }
    .rich-content h3 {
      font-size: 10.5pt;
      font-weight: 700;
      color: var(--accent);
      margin: 10px 0 4px 0;
      break-after: avoid;
    }
    .rich-content p { margin: 0 0 7px 0; }
    .rich-content ul, .rich-content ol { margin: 0 0 8px 0; padding-left: 22px; }
    .rich-content li { margin-bottom: 5px; }
    .rich-content li > ul { margin-top: 4px; margin-bottom: 4px; }
    .rich-content table {
      width: 100%;
      border-collapse: collapse;
      margin: 8px 0 12px 0;
      font-size: 10pt;
      break-inside: auto;
    }
    .rich-content th {
      background: var(--primary);
      color: #ffffff;
      text-align: left;
      padding: 6px 10px;
      font-weight: 700;
    }
    .rich-content td {
      padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }
    .rich-content tbody tr:nth-child(even) { background: var(--surface); }
    .rich-content tr { break-inside: avoid; }

    /* Publication Math Typography: stacked fractions, full-bar roots, tall SVG curly braces */
    .math-inline, .math-display {
      font-family: 'Noto Serif CJK SC', 'DejaVu Serif', 'Times New Roman', serif;
      color: inherit;
    }
    .math-inline {
      display: inline;
      white-space: nowrap;
    }
    .math-display {
      display: block;
      text-align: center;
      margin: 10px 0 12px 0;
      padding: 8px 14px;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      font-size: 11pt;
      line-height: 1.4;
      break-inside: avoid;
    }
    .math-inline i, .math-display i { font-style: italic; }
    .math-inline sup, .math-display sup { font-size: 0.72em; vertical-align: super; line-height: 0; }
    .math-inline sub, .math-display sub { font-size: 0.72em; vertical-align: sub; line-height: 0; }
    .m-op { font-style: normal; margin-right: 2px; }
    .m-text { font-family: 'Noto Sans CJK SC', sans-serif; font-style: normal; font-size: 0.92em; }
    .m-vec { display: inline-block; position: relative; padding-top: 0.45em; }
    .m-vec::before { content: '→'; position: absolute; top: -0.25em; left: 0; right: 0; text-align: center; font-size: 0.72em; font-style: normal; line-height: 1; }
    .rich-content img.diagram-img { display: block; max-width: 96%; max-height: 86mm; width: auto; height: auto; margin: 8px auto; border: 1px solid #e2e8f0; border-radius: 6px; padding: 4px; background: #ffffff; break-inside: avoid; }
    .rich-content img.handwritten-img { display: block; width: 100%; max-width: 100%; max-height: 192mm; height: auto; object-fit: contain; margin: 6px auto; }
    .diagram-img-fig { border: none !important; padding: 0 !important; background: transparent !important; }
    .m-quad { display: inline-block; width: 1.2em; }

    /* Stacked fraction: numerator over horizontal bar over denominator */
    .m-frac {
      display: inline-table;
      vertical-align: middle;
      text-align: center;
      margin: 0 2px;
      line-height: 1.15;
      font-size: 0.92em;
    }
    .m-frac > .m-num {
      display: table-row;
      border-bottom: 1.1px solid #0f172a;
    }
    .m-frac > .m-num, .m-frac > .m-den {
      display: block;
      padding: 0 4px;
    }
    .m-frac > .m-num {
      border-bottom: 1.1px solid #0f172a;
      padding-bottom: 1.5px;
      margin-bottom: 1.5px;
    }

    /* Full-overbar square root */
    .m-sqrt {
      display: inline-table;
      vertical-align: middle;
      margin: 0 2px;
      border-collapse: collapse;
    }
    .m-sqrt-hook {
      display: table-cell;
      vertical-align: bottom;
      width: 0.65em;
      padding: 0;
    }
    .m-sqrt-svg {
      display: block;
      width: 0.65em;
      height: 1.35em;
    }
    .m-radicand {
      display: table-cell;
      vertical-align: middle;
      border-top: 1.15px solid #0f172a;
      padding: 1px 4px 0 2px;
      line-height: 1.25;
      white-space: nowrap;
    }

    /* Tall curly-brace equation system (cases) */
    .m-cases {
      display: inline-flex;
      align-items: stretch;
      vertical-align: middle;
      margin: 0 4px;
    }
    .m-brace-cell {
      display: flex;
      align-items: stretch;
      width: 12px;
      margin-right: 4px;
    }
    .m-brace-svg {
      width: 12px;
      height: 100%;
    }
    .m-cases-rows {
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 6px;
      text-align: left;
      padding: 2px 0;
    }
    .m-case-row {
      display: block;
      white-space: nowrap;
    }
    .m-big-delim {
      font-size: 1.25em;
      vertical-align: middle;
      margin: 0 1px;
    }
  
    /* ---------- 图片：手写分段、自绘按字号 ---------- */
    .hw-group { margin: 2px 0 10px 0; }
    figure.hw-chunk { margin: 0 auto; padding: 0; border: none; break-inside: avoid; }
    figure.hw-chunk img { display: block; max-width: 100%; height: auto; max-height: none; margin: 0 auto; }
    figure.svg-fig { border: none; padding: 0; margin: 6px 0 10px 0; background: transparent; break-inside: avoid; }
    figure.svg-fig img, .rich-content figure.svg-fig img.diagram-img {
      display: block; max-width: 100%; max-height: none; height: auto; margin: 0 auto;
      border: 1px solid #e2e8f0; border-radius: 6px; padding: 3px; background: #fff;
    }

    /* ---------- 封面 ---------- */
    @page cover { @top-left { content: none; } @bottom-right { content: none; } }
    .cover { page: cover; break-after: page; padding-top: 52mm; text-align: center; }
    .cover-kicker { font-size: 10pt; font-weight: 700; color: var(--accent); letter-spacing: 0.08em; }
    .cover-title { font-size: 34pt; color: var(--primary); margin: 12px 0 8px 0; }
    .cover-subtitle { font-size: 13pt; color: var(--muted); margin: 0 0 26px 0; }
    .cover-stats { display: inline-block; font-size: 12pt; font-weight: 700; color: #fff; background: var(--primary); border-radius: 20px; padding: 6px 22px; }
    .cover-mods { list-style: none; padding: 0; margin: 30px auto 0 auto; width: 120mm; text-align: left; columns: 2; font-size: 11pt; line-height: 2; color: var(--text); }
    .cover-foot { margin-top: 60mm; font-size: 10pt; color: var(--muted); }

    /* ---------- 前言页通用 ---------- */
    .front-heading { font-size: 18pt; color: var(--primary); margin: 0 0 6px 0; padding-bottom: 6px; border-bottom: 3px solid var(--primary); }
    .front-note { font-size: 9.5pt; color: var(--muted); margin: 0 0 12px 0; }
    .supp-key { color: #15803d; font-weight: 700; }

    /* ---------- 思维导图 ---------- */
    .mindmap { break-after: page; }
    .ov-root { width: 60mm; margin: 4px auto 0 auto; text-align: center; font-size: 13pt; font-weight: 800; color: #fff; background: var(--primary); border-radius: 8px; padding: 6px 0; }
    .ov-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 0 0 6mm 0; padding-top: 10px; border-top: 2px solid var(--primary); position: relative; break-inside: avoid; }
    .ov-mod { border: 1.5px solid var(--primary); border-radius: 6px; padding: 5px 7px; background: var(--surface); position: relative; }
    .ov-mod::before { content: ""; position: absolute; top: -12px; left: 50%; height: 10px; border-left: 2px solid var(--primary); }
    .ov-title { font-size: 9.5pt; font-weight: 800; color: var(--primary); margin-bottom: 3px; }
    .ov-mod ul { margin: 0; padding-left: 2px; list-style: none; font-size: 8pt; line-height: 1.45; color: var(--text); }
    .mm-module { display: flex; align-items: stretch; margin: 0 0 5mm 0; break-inside: avoid; }
    .mm-root { flex: 0 0 30mm; display: flex; align-items: center; justify-content: center; text-align: center; font-size: 10.5pt; font-weight: 800; color: #fff; background: var(--primary); border-radius: 8px; padding: 6px; margin-right: 6px; }
    .mm-branches { flex: 1; border-left: 2px solid var(--primary); padding-left: 0; }
    .mm-row { display: flex; align-items: center; margin: 3px 0; }
    .mm-row::before { content: ""; flex: 0 0 8px; border-top: 2px solid var(--primary); }
    .mm-sub { flex: 0 0 40mm; font-size: 8.8pt; font-weight: 700; color: var(--accent); border: 1.5px solid var(--accent); border-radius: 5px; padding: 3px 5px; background: #f0fdfa; margin-right: 6px; }
    .mm-sub.mm-none { flex: 0 0 0; border: none; padding: 0; margin: 0; background: none; }
    .mm-items { flex: 1; list-style: none; margin: 0; padding: 2px 0 2px 8px; border-left: 1.5px solid #94a3b8; font-size: 8.2pt; line-height: 1.38; }
    .mm-items li { position: relative; padding-left: 4px; }
    .mm-items li::before { content: ""; position: absolute; left: -8px; top: 0.7em; width: 7px; border-top: 1.2px solid #94a3b8; }
    .mm-items li.supp { color: #15803d; }

    /* ---------- 目录 ---------- */
    .toc { break-after: page; }
    .toc a { color: inherit; text-decoration: none; }
    .toc-l1, .toc-l2, .toc-l3 { display: flex; align-items: baseline; break-inside: avoid; }
    .toc-l1 { font-size: 11pt; font-weight: 800; color: var(--primary); margin: 9px 0 2px 0; break-after: avoid; }
    .toc-l2 { font-size: 9.8pt; font-weight: 700; color: var(--accent); margin: 3px 0 1px 4mm; }
    .toc-l2.toc-item { font-weight: 400; color: var(--text); margin-left: 4mm; }
    .toc-l3 { font-size: 9.3pt; color: var(--text); margin: 0 0 0 9mm; line-height: 1.5; }
    .dots { flex: 1; border-bottom: 1px dotted #94a3b8; margin: 0 4px; transform: translateY(-3px); min-width: 8mm; }
    .pg { display: inline-block; min-width: 8mm; text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }

    /* ---------- 原稿序号索引 ---------- */
    .orig-index { break-before: page; }
    .idx-table { width: 100%; border-collapse: collapse; font-size: 9pt; }
    .idx-table th { background: var(--primary); color: #fff; text-align: left; padding: 4px 6px; }
    .idx-table td { padding: 3px 6px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
    .idx-table tr { break-inside: avoid; }
    .idx-table a { color: var(--text); text-decoration: none; }
    .idx-group td { background: #ccfbf1; color: var(--accent); font-weight: 800; padding-top: 5px; }
    .idx-num { white-space: nowrap; font-weight: 700; color: var(--primary); }
    .idx-pages, .idx-pg { white-space: nowrap; color: var(--muted); }
    .idx-where { color: var(--muted); font-size: 8.5pt; }

    /* ---------- 补充笔记空白页 ---------- */
    @page notes {
      @top-left { content: none; }
      @bottom-right { content: none; }
      @bottom-center { content: "补充笔记"; font-family: 'Noto Sans CJK SC', sans-serif; font-size: 9pt; color: #94a3b8; }
    }
    .notes-page { page: notes; break-before: page; }
    .notes-fill { height: 1px; }

    /* ---------- 插入的整页 PDF（知识架构）：先留空白页，渲染后叠上原页 ---------- */
    @page ext { margin: 0; @top-left { content: none; } @bottom-right { content: none; } }
    .ext-page { page: ext; height: 290mm; break-after: page; }

    .book-body { break-before: auto; }
"""


_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int:
    s = re.sub(r"[^零一二三四五六七八九十百]", "", s)
    if not s:
        return 10 ** 6
    total, cur = 0, 0
    for ch in s:
        if ch == "十":
            total += (cur or 1) * 10; cur = 0
        elif ch == "百":
            total += (cur or 1) * 100; cur = 0
        else:
            cur = _CN_NUM[ch]
    return total + cur


def _first_page(pages: str) -> int:
    m = re.search(r"P(\d+)", pages or "")
    return int(m.group(1)) if m else 10 ** 6


def _pg(dest: str) -> str:
    """页码占位：第一遍渲染写 000，第二遍按 PDF 里的命名目标替换成真实页码。"""
    return f'<span class="pg" data-dest="{dest}">000</span>'


def _short_module(title: str) -> str:
    return re.sub(r"^[一二三四五六七八九十]+、", "", title)


def build_html(
    outline_path: Path,
    selected_modules: set[str] | None = None,
    subsection_keyword: str = "",
    custom_title: str = "",
    custom_subtitle: str = "",
    full_book: bool = True,
) -> str:
    outline = yaml.safe_load(outline_path.read_text(encoding="utf-8"))
    doc_title = custom_title or outline.get("title", "解析几何小题笔记合集")
    doc_subtitle = custom_subtitle or outline.get("subtitle", "")
    modules = outline.get("modules", [])

    if selected_modules:
        modules = [m for m in modules if str(m.get("id")) in selected_modules]
        full_book = False

    filtered_modules = []
    for mod in modules:
        subs = mod.get("subsections", [])
        if subsection_keyword:
            full_book = False
            kept = []
            for s in subs:
                if subsection_keyword in s.get("title", ""):
                    kept.append(s)
                else:  # 不分小节的模块按条目标题筛选
                    its = [it for it in s.get("items", []) if subsection_keyword in it.get("title", "")]
                    if its and not s.get("title"):
                        kept.append(dict(s, items=its))
            subs = kept
        if subs:
            mod_copy = dict(mod)
            mod_copy["subsections"] = subs
            filtered_modules.append(mod_copy)

    if not filtered_modules:
        raise ValueError("未匹配到任何模块或小节，请检查筛选参数或 outline.yaml。")

    toc_rows = []        # 目录
    map_blocks = []      # 思维导图
    overview_nodes = []  # 思维导图总览
    body_blocks = []
    index_rows = []      # 原稿序号索引
    total_items = 0

    for mod in filtered_modules:
        mod_id = f"mod-{mod['id']}"
        mod_title = html.escape(mod["title"])
        toc_rows.append(f'<div class="toc-l1"><a href="#{mod_id}">{mod_title}</a><span class="dots"></span>{_pg(mod_id)}</div>')
        body_blocks.append(f'<section class="module-section" id="{mod_id}">')
        body_blocks.append(
            f'<div class="module-banner"><span class="module-tag">MODULE {html.escape(str(mod["id"]))}</span>'
            f'<h2>{mod_title}</h2></div>'
        )
        map_rows = []
        ov_subs = []
        for s_idx, sub in enumerate(mod.get("subsections", []), start=1):
            sub_id = f"{mod_id}-sub-{s_idx}"
            sub_title = html.escape(sub.get("title", "") or "")
            items = sub.get("items", [])
            if sub_title:
                toc_rows.append(f'<div class="toc-l2"><a href="#{sub_id}">{sub_title}</a><span class="dots"></span>{_pg(sub_id)}</div>')
                body_blocks.append(f'<h3 class="sub-heading" id="{sub_id}">{sub_title}</h3>')
                ov_subs.append(f"<li>{sub_title}</li>")
            item_nodes = []
            for i_idx, item in enumerate(items, start=1):
                total_items += 1
                item_id = f"{sub_id}-item-{i_idx}"
                raw_title = item["title"]
                item_title = html.escape(raw_title)
                orig_num = str(item.get("orig_num", "") or "")
                pages = str(item.get("pages", "") or "")
                badge_text = html.escape(f"原：{orig_num} ｜ {pages}" if pages else orig_num)
                cls = "toc-l3" if sub_title else "toc-l2 toc-item"
                toc_rows.append(f'<div class="{cls}"><a href="#{item_id}">{item_title}</a><span class="dots"></span>{_pg(item_id)}</div>')
                supp = "补充专题" in orig_num or "独立专题" in orig_num or "电子笔记" in orig_num
                item_nodes.append(f'<li class="{"supp" if supp else ""}">{item_title}</li>')
                if pages and not supp:
                    sub_clean = re.sub(r"^\d+\.\s*", "", sub.get("title", "") or "")
                    where = _short_module(mod["title"]) + (f" › {sub_clean}" if sub_title else "")
                    index_rows.append((orig_num, pages, where, raw_title, item_id))

                md_path = (outline_path.parent / item["file"]).resolve()
                inner_html = render_item_markdown(md_path)
                body_blocks.append(f'<article class="topic-block" id="{item_id}">')
                body_blocks.append(
                    f'<div class="topic-header"><span class="topic-title">{item_title}</span>'
                    f'<span class="topic-badge">{badge_text}</span></div>'
                )
                body_blocks.append(inner_html)
                body_blocks.append("</article>")
            if sub_title:
                map_rows.append(f'<div class="mm-row"><div class="mm-sub">{sub_title}</div><ul class="mm-items">{"".join(item_nodes)}</ul></div>')
            else:
                map_rows.append(f'<div class="mm-row"><div class="mm-sub mm-none"></div><ul class="mm-items">{"".join(item_nodes)}</ul></div>')
        body_blocks.append("</section>")
        map_blocks.append(
            f'<div class="mm-module"><div class="mm-root">{mod_title}</div><div class="mm-branches">{"".join(map_rows)}</div></div>'
        )
        n_items = sum(len(s.get("items", [])) for s in mod.get("subsections", []))
        ov_list = f'<ul>{"".join(ov_subs)}</ul>' if ov_subs else f'<ul><li>不分小节，共 {n_items} 条</li></ul>'
        overview_nodes.append(f'<div class="ov-mod"><div class="ov-title">{mod_title}</div>{ov_list}</div>')

    # ---------- 原稿序号索引 ----------
    def _sort_key(row):
        num = row[0]
        if num.startswith("大写"):
            return (0, _cn_to_int(num), _first_page(row[1]))
        if num.startswith("小写"):
            m = re.search(r"\d+", num)
            return (1, int(m.group()) if m else 10 ** 6, _first_page(row[1]))
        return (2, _first_page(row[1]), 0)

    index_rows.sort(key=_sort_key)
    group_names = ["大写序号（一、二、三……）", "小写序号（1. 2. 3. ……）", "原稿中没有序号的条目（按原稿页码）"]
    idx_html, last_group = [], None
    for row in index_rows:
        g = _sort_key(row)[0]
        if g != last_group:
            idx_html.append(f'<tr class="idx-group"><td colspan="5">{group_names[g]}</td></tr>')
            last_group = g
        num, pages, where, title, item_id = row
        num_disp = re.sub(r"^(大写|小写)\s*", "", num)
        idx_html.append(
            f'<tr><td class="idx-num">{html.escape(num_disp)}</td><td class="idx-pages">{html.escape(pages)}</td>'
            f'<td class="idx-where">{html.escape(where)}</td>'
            f'<td><a href="#{item_id}">{html.escape(title)}</a></td><td class="idx-pg">{_pg(item_id)}</td></tr>'
        )

    scope_desc = " · ".join(m["title"] for m in filtered_modules)
    css = BASE_CSS.replace("__DOC_TITLE__", html.escape(doc_title))

    front = []
    back = []
    if full_book:
        front.append(f"""
    <section class="cover">
      <div class="cover-kicker">ANALYTIC GEOMETRY · 高考解析几何小题体系</div>
      <h1 class="cover-title">{html.escape(doc_title)}</h1>
      <p class="cover-subtitle">{html.escape(doc_subtitle)}</p>
      <div class="cover-stats">{len(filtered_modules)} 个模块 · {total_items} 个知识点</div>
      <ul class="cover-mods">{"".join(f"<li>{html.escape(m['title'])}</li>" for m in filtered_modules)}</ul>
      <div class="cover-foot">手写原稿高清图 · 补充专题文字笔记 · 自绘配图</div>
    </section>
    {_ext_pages_html()}
    <section class="mindmap" id="mindmap">
      <h2 class="front-heading">整体思维导图</h2>
      <p class="front-note">先看全书八个模块和各自的小节，后面几页逐个模块展开到每一条。<span class="supp-key">绿色</span>的条目是补充专题或电子笔记，其余对应手写原稿。</p>
      <div class="ov-root">解析几何小题</div>
      <div class="ov-grid">{"".join(overview_nodes)}</div>
      {"".join(map_blocks)}
    </section>
    <section class="toc" id="toc">
      <h2 class="front-heading">目录</h2>
      {_ext_toc_html()}
      <div class="toc-l1"><a href="#mindmap">整体思维导图</a><span class="dots"></span>{_pg("mindmap")}</div>
      {"".join(toc_rows)}
      <div class="toc-l1"><a href="#orig-index">原稿序号索引</a><span class="dots"></span>{_pg("orig-index")}</div>
      <div class="toc-l1"><a href="#notes">补充笔记（空白页）</a><span class="dots"></span>{_pg("notes")}</div>
    </section>""")
        back.append(f"""
    <section class="orig-index" id="orig-index">
      <h2 class="front-heading">原稿序号索引</h2>
      <p class="front-note">原稿里写着「第 30 条已证过」「见前面第 12 条」时，在这里按原序号查它现在的位置和页码，点标题可以直接跳过去。补充专题和电子笔记不在原稿里，没有列出。</p>
      <table class="idx-table">
        <thead><tr><th>原序号</th><th>原稿页码</th><th>现在的位置</th><th>标题</th><th>页码</th></tr></thead>
        <tbody>{"".join(idx_html)}</tbody>
      </table>
    </section>""")
        back.append('<section class="notes-page" id="notes"><div class="notes-fill"></div></section>')
        back.extend('<section class="notes-page"><div class="notes-fill"></div></section>' for _ in range(9))
    else:
        front.append(f"""
    <header class="hero-header">
      <h1>{html.escape(doc_title)}</h1>
      <div class="hero-stats">收录范围：{html.escape(scope_desc)}（共 {total_items} 个知识点）</div>
    </header>""")

    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(doc_title)}</title>
  <style>{css}</style>
</head>
<body>
  <main class="document-flow">
    {"".join(front)}
    <div class="book-body">{"".join(body_blocks)}</div>
    {"".join(back)}
  </main>
</body>
</html>
"""


FRONT_PDF = REPO_ROOT / "front" / "解析几何小题知识架构.pdf"
FRONT_TITLE = "解析几何小题知识架构"


def _ext_page_count() -> int:
    if not FRONT_PDF.is_file():
        return 0
    try:
        from pypdf import PdfReader
    except ImportError:
        return 0
    return len(PdfReader(str(FRONT_PDF)).pages)


def _ext_pages_html() -> str:
    """封面后面给知识架构 PDF 留出同样页数的空白页，渲染完再把原页叠上去。"""
    n = _ext_page_count()
    if not n:
        return ""
    pages = "".join('<div class="ext-page"></div>' for _ in range(n))
    return f'<section class="ext-pages" id="arch">{pages}</section>'


def _ext_toc_html() -> str:
    if not _ext_page_count():
        return ""
    return f'<div class="toc-l1"><a href="#arch">{FRONT_TITLE}</a><span class="dots"></span>{_pg("arch")}</div>'


def overlay_front_pdf(pdf_path: Path) -> None:
    """把 front/ 里的知识架构 PDF 逐页叠到预留的空白页上（目录页码、链接都不受影响）。"""
    n = _ext_page_count()
    if not n:
        return
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=str(pdf_path))
    start = None
    for name, dest in (PdfReader(str(pdf_path)).named_destinations or {}).items():
        if name.lstrip("/") == "arch":
            start = PdfReader(str(pdf_path)).get_destination_page_number(dest)
    if start is None:
        print("提示：没找到知识架构的预留页，未插入", file=sys.stderr)
        return
    front = PdfReader(str(FRONT_PDF))
    for i in range(n):
        target = writer.pages[start + i]
        src = front.pages[i]
        sx = float(target.mediabox.width) / float(src.mediabox.width)
        sy = float(target.mediabox.height) / float(src.mediabox.height)
        from pypdf import Transformation
        target.merge_transformed_page(src, Transformation().scale(sx, sy))
    with open(pdf_path, "wb") as f:
        writer.write(f)


def fill_page_numbers(html_str: str, pdf_path: Path) -> str:
    """读取第一遍渲染出的 PDF 里的命名目标，把目录和索引里的 000 换成真实页码。"""
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    pages = {}
    for name, dest in (reader.named_destinations or {}).items():
        try:
            pages[name.lstrip("/")] = reader.get_destination_page_number(dest) + 1
        except Exception:
            pass

    def _sub(m: re.Match) -> str:
        p = pages.get(m.group(1))
        return f'<span class="pg" data-dest="{m.group(1)}">{p if p else ""}</span>'

    return re.sub(r'<span class="pg" data-dest="([^"]+)">000</span>', _sub, html_str)


def find_chrome() -> str | None:
    """依次查找环境变量 CHROME_PATH 和系统里常见的 Chrome / Chromium 可执行文件。"""
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for cand in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/opt/pw-browsers/chromium",
    ):
        if Path(cand).exists():
            return cand
    return None


def render_with_chrome(html_str: str, out_pdf: Path) -> None:
    """用无头 Chrome 打印 PDF，排版与在浏览器里预览 HTML 完全一致。"""
    chrome = find_chrome()
    if not chrome:
        print("未找到 Chrome/Chromium，请安装 Chrome，或设置 CHROME_PATH，或改用 --engine weasyprint", file=sys.stderr)
        sys.exit(1)
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "book.html"
        html_path.write_text(html_str, encoding="utf-8")
        cmd = [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=20000",
            f"--print-to-pdf={out_pdf.resolve()}", html_path.resolve().as_uri(),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    parser = argparse.ArgumentParser(description="按 outline.yaml 自动合并排版解析几何小题笔记为 PDF")
    parser.add_argument("--outline", default=str(REPO_ROOT / "outline.yaml"), help="outline.yaml 路径")
    parser.add_argument("--modules", default="", help="仅合并指定模块编号，如 01 或 01,02（默认合并全部）")
    parser.add_argument("--subsection", default="", help="仅合并标题包含该关键词的小节，如 对称问题")
    parser.add_argument("--title", default="", help="自定义封面标题")
    parser.add_argument("--subtitle", default="", help="自定义副标题")
    parser.add_argument("-o", "--output", default=str(REPO_ROOT / "output" / "解析几何小题合集.pdf"), help="输出 PDF 路径")
    parser.add_argument("--html-only", default="", help="仅输出自包含 HTML 文件路径")
    parser.add_argument("--engine", choices=["chrome", "weasyprint"], default="chrome",
                        help="PDF 渲染引擎：chrome（默认，与浏览器预览一致）或 weasyprint（备用）")
    args = parser.parse_args()

    selected = {m.strip().zfill(2) for m in args.modules.split(",") if m.strip()} or None
    html_str = build_html(
        Path(args.outline),
        selected_modules=selected,
        subsection_keyword=args.subsection,
        custom_title=args.title,
        custom_subtitle=args.subtitle,
    )

    if args.html_only:
        out_html = Path(args.html_only)
        out_html.parent.mkdir(parents=True, exist_ok=True)
        out_html.write_text(html_str, encoding="utf-8")
        print(f"已导出 HTML：{out_html}")
        return

    out_pdf = Path(args.output)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if args.engine == "chrome":
        render_with_chrome(html_str, out_pdf)
        if 'data-dest="' in html_str:
            try:
                html_str = fill_page_numbers(html_str, out_pdf)
                render_with_chrome(html_str, out_pdf)  # 第二遍：填好目录和索引页码
                overlay_front_pdf(out_pdf)
            except ImportError:
                print("提示：未安装 pypdf，目录页码留空（pip install pypdf）", file=sys.stderr)
    else:
        try:
            from weasyprint import HTML
        except ImportError:
            print("未安装 weasyprint，请先运行：pip install weasyprint", file=sys.stderr)
            sys.exit(1)
        HTML(string=html_str, base_url=str(REPO_ROOT)).write_pdf(str(out_pdf))
    print(f"已生成合并 PDF：{out_pdf} ({out_pdf.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
