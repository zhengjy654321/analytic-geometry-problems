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
        if tex.startswith(r"\frac", i):
            num, next_i = _extract_brace_arg(tex, i + 5)
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


def render_item_markdown(md_path: Path) -> str:
    """将单个知识点的 Markdown 渲染为 HTML（兼容纯裁剪图笔记与含 LaTeX 公式/表格的文字笔记）。"""
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
                    data_uri = encode_image_data_uri(img_abs)
                    parts.append(
                        f'<figure class="note-figure">'
                        f'<img src="{data_uri}" alt="{html.escape(alt)}" />'
                        f'</figure>'
                    )
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
            cls = "diagram-img" if img_abs.suffix.lower() == ".svg" else "handwritten-img"
            return f'<figure class="note-figure {cls}-fig"><img class="{cls}" src="{encode_image_data_uri(img_abs)}" alt="{html.escape(alt)}" /></figure>'
        return m.group(0)

    body_md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace_img, body_md)
    rendered = convert_markdown_with_math(body_md)
    return f'<div class="rich-content">{rendered}</div>'


def build_html(
    outline_path: Path,
    selected_modules: set[str] | None = None,
    subsection_keyword: str = "",
    custom_title: str = "",
    custom_subtitle: str = "",
) -> str:
    outline = yaml.safe_load(outline_path.read_text(encoding="utf-8"))
    doc_title = custom_title or outline.get("title", "解析几何小题笔记合集")
    doc_subtitle = custom_subtitle or outline.get("subtitle", "")
    modules = outline.get("modules", [])

    if selected_modules:
        modules = [m for m in modules if str(m.get("id")) in selected_modules]

    filtered_modules = []
    for mod in modules:
        subs = mod.get("subsections", [])
        if subsection_keyword:
            subs = [s for s in subs if subsection_keyword in s.get("title", "")]
        if subs:
            mod_copy = dict(mod)
            mod_copy["subsections"] = subs
            filtered_modules.append(mod_copy)

    if not filtered_modules:
        raise ValueError("未匹配到任何模块或小节，请检查筛选参数或 outline.yaml。")

    toc_blocks = []
    body_blocks = []

    for mod in filtered_modules:
        mod_id = f"mod-{mod['id']}"
        mod_title = html.escape(mod["title"])
        sub_toc_items = []

        body_blocks.append(f'<section class="module-section" id="{mod_id}">')
        body_blocks.append(
            f'<div class="module-banner">'
            f'<span class="module-tag">MODULE {html.escape(str(mod["id"]))}</span>'
            f'<h2>{mod_title}</h2>'
            f'</div>'
        )

        for s_idx, sub in enumerate(mod.get("subsections", []), start=1):
            sub_id = f"{mod_id}-sub-{s_idx}"
            sub_title = html.escape(sub["title"])
            item_count = len(sub.get("items", []))
            sub_toc_items.append(
                f'<li><a href="#{sub_id}">{sub_title}</a>'
                f'<span class="toc-meta">{item_count} 项</span></li>'
            )

            body_blocks.append(f'<h3 class="sub-heading" id="{sub_id}">{sub_title}</h3>')

            for item in sub.get("items", []):
                item_title = html.escape(item["title"])
                orig_num = html.escape(str(item.get("orig_num", "")))
                pages = html.escape(str(item.get("pages", "")))
                badge_text = f"原：{orig_num} ｜ {pages}" if pages else orig_num
                md_path = (outline_path.parent / item["file"]).resolve()
                inner_html = render_item_markdown(md_path)

                body_blocks.append('<article class="topic-block">')
                body_blocks.append(
                    f'<div class="topic-header">'
                    f'<span class="topic-title">{item_title}</span>'
                    f'<span class="topic-badge">{badge_text}</span>'
                    f'</div>'
                )
                body_blocks.append(inner_html)
                body_blocks.append("</article>")

        body_blocks.append("</section>")

        toc_blocks.append(
            f'<div class="toc-module">'
            f'<div class="toc-module-title"><a href="#{mod_id}">{mod_title}</a></div>'
            f'<ul class="toc-sublist">{"".join(sub_toc_items)}</ul>'
            f'</div>'
        )

    total_items = sum(
        len(sub.get("items", []))
        for mod in filtered_modules
        for sub in mod.get("subsections", [])
    )
    toc_cols = "1fr" if len(filtered_modules) == 1 else "repeat(2, 1fr)"
    scope_desc = " · ".join(
        f"{m['title']}" if not subsection_keyword else f"{m['title']} / {m['subsections'][0]['title']}"
        for m in filtered_modules
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(doc_title)}</title>
  <style>
    :root {{
      --primary: #1e3a5f;
      --accent: #0f766e;
      --surface: #f8fafc;
      --surface-warm: #fffbeb;
      --border: #cbd5e1;
      --text: #1e293b;
      --muted: #475569;
      --bg: #ffffff;
    }}

    @page {{
      size: A4;
      margin: 16mm 15mm 18mm 15mm;
      background: var(--bg);
      @top-left {{
        content: "{html.escape(doc_title)}";
        font-family: 'Noto Sans CJK SC', 'Inter', sans-serif;
        font-size: 9pt;
        color: var(--muted);
      }}
      @bottom-right {{
        content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
        font-family: 'Noto Sans CJK SC', 'Inter', sans-serif;
        font-size: 9pt;
        color: var(--muted);
        white-space: nowrap;
      }}
    }}

    @page :first {{
      @top-left {{ content: none; }}
    }}

    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: transparent; }}
    body {{
      font-family: 'Noto Sans CJK SC', 'Inter', -apple-system, sans-serif;
      font-size: 10.5pt;
      line-height: 1.65;
      color: var(--text);
      letter-spacing: normal;
    }}

    main.document-flow {{ width: 100%; }}

    .hero-header {{
      border-bottom: 3px solid var(--primary);
      padding-bottom: 12px;
      margin-bottom: 16px;
    }}
    .hero-kicker {{
      font-size: 9.5pt;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 4px;
    }}
    h1 {{
      font-size: 22pt;
      font-weight: 800;
      color: var(--primary);
      margin: 0 0 6px 0;
      line-height: 1.25;
    }}
    .hero-subtitle {{ font-size: 10.5pt; color: var(--muted); margin: 0 0 8px 0; }}
    .hero-stats {{
      font-size: 10pt;
      color: var(--text);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 12px;
      display: inline-block;
    }}

    .toc-box {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 18px;
    }}
    .toc-heading {{
      font-size: 12pt;
      font-weight: 700;
      color: var(--primary);
      margin: 0 0 8px 0;
      padding-bottom: 5px;
      border-bottom: 1px solid var(--border);
    }}
    .toc-grid {{ display: grid; grid-template-columns: {toc_cols}; gap: 14px; }}
    .toc-module-title {{ font-size: 10.5pt; font-weight: 700; color: var(--primary); margin-bottom: 4px; }}
    .toc-module-title a {{ color: var(--primary); text-decoration: none; }}
    .toc-sublist {{ list-style: none; margin: 0; padding: 0; }}
    .toc-sublist li {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      font-size: 10pt;
      padding: 3px 0;
      border-bottom: 1px dashed #e2e8f0;
    }}
    .toc-sublist a {{ color: var(--text); text-decoration: none; }}
    .toc-meta {{ font-size: 9.5pt; color: var(--muted); flex-shrink: 0; margin-left: 8px; }}

    .module-section {{ margin-top: 16px; break-inside: auto; }}
    .module-banner {{
      background: var(--primary);
      color: #ffffff;
      border-radius: 6px;
      padding: 10px 16px;
      margin: 14px 0 12px 0;
      break-inside: avoid;
      break-after: avoid;
    }}
    .module-tag {{ display: inline-block; font-size: 9pt; font-weight: 700; color: #bae6fd; margin-bottom: 2px; }}
    .module-banner h2 {{ font-size: 15.5pt; font-weight: 700; color: #ffffff; margin: 0; line-height: 1.3; }}

    h3.sub-heading {{
      font-size: 13pt;
      font-weight: 700;
      color: var(--accent);
      margin: 14px 0 8px 0;
      padding-bottom: 4px;
      border-bottom: 1.5px solid #ccfbf1;
      break-after: avoid;
    }}

    .topic-block {{ margin: 0 0 16px 0; break-inside: auto; }}
    .topic-header {{
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
    }}
    .topic-title {{ font-size: 11.5pt; font-weight: 700; color: var(--primary); }}
    .topic-badge {{ font-size: 9.5pt; color: var(--muted); white-space: nowrap; flex-shrink: 0; }}
    .topic-note {{
      font-size: 10pt;
      color: #92400e;
      background: var(--surface-warm);
      border-left: 3px solid #d97706;
      padding: 6px 10px;
      margin: 0 0 6px 0;
      border-radius: 0 4px 4px 0;
      break-inside: avoid;
      break-after: avoid;
    }}

    figure.note-figure {{
      margin: 0 0 8px 0;
      padding: 4px;
      border: 1px solid #e2e8f0;
      border-radius: 4px;
      background: #ffffff;
      break-inside: avoid;
    }}
    figure.note-figure img {{
      display: block;
      width: 100%;
      max-height: 192mm;
      height: auto;
      object-fit: contain;
      margin: 0 auto;
    }}

    /* Rich Markdown */
    .rich-content {{ padding: 2px 4px 4px 4px; }}
    .rich-content h2 {{
      font-size: 11.5pt;
      font-weight: 700;
      color: var(--primary);
      margin: 12px 0 6px 0;
      padding-bottom: 2px;
      border-bottom: 1px solid #e2e8f0;
      break-after: avoid;
    }}
    .rich-content h3 {{
      font-size: 10.5pt;
      font-weight: 700;
      color: var(--accent);
      margin: 10px 0 4px 0;
      break-after: avoid;
    }}
    .rich-content p {{ margin: 0 0 7px 0; }}
    .rich-content ul, .rich-content ol {{ margin: 0 0 8px 0; padding-left: 22px; }}
    .rich-content li {{ margin-bottom: 5px; }}
    .rich-content li > ul {{ margin-top: 4px; margin-bottom: 4px; }}
    .rich-content table {{
      width: 100%;
      border-collapse: collapse;
      margin: 8px 0 12px 0;
      font-size: 10pt;
      break-inside: auto;
    }}
    .rich-content th {{
      background: var(--primary);
      color: #ffffff;
      text-align: left;
      padding: 6px 10px;
      font-weight: 700;
    }}
    .rich-content td {{
      padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }}
    .rich-content tbody tr:nth-child(even) {{ background: var(--surface); }}
    .rich-content tr {{ break-inside: avoid; }}

    /* Publication Math Typography: stacked fractions, full-bar roots, tall SVG curly braces */
    .math-inline, .math-display {{
      font-family: 'Noto Serif CJK SC', 'DejaVu Serif', 'Times New Roman', serif;
      color: inherit;
    }}
    .math-inline {{
      display: inline;
      white-space: nowrap;
    }}
    .math-display {{
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
    }}
    .math-inline i, .math-display i {{ font-style: italic; }}
    .math-inline sup, .math-display sup {{ font-size: 0.72em; vertical-align: super; line-height: 0; }}
    .math-inline sub, .math-display sub {{ font-size: 0.72em; vertical-align: sub; line-height: 0; }}
    .m-op {{ font-style: normal; margin-right: 2px; }}
    .m-text {{ font-family: 'Noto Sans CJK SC', sans-serif; font-style: normal; font-size: 0.92em; }}
    .m-vec {{ display: inline-block; position: relative; padding-top: 0.45em; }}
    .m-vec::before {{ content: '→'; position: absolute; top: -0.25em; left: 0; right: 0; text-align: center; font-size: 0.72em; font-style: normal; line-height: 1; }}
    .rich-content img.diagram-img {{ display: block; max-width: 96%; max-height: 86mm; width: auto; height: auto; margin: 8px auto; border: 1px solid #e2e8f0; border-radius: 6px; padding: 4px; background: #ffffff; break-inside: avoid; }}
    .rich-content img.handwritten-img {{ display: block; width: 100%; max-width: 100%; max-height: 192mm; height: auto; object-fit: contain; margin: 6px auto; }}
    .diagram-img-fig {{ border: none !important; padding: 0 !important; background: transparent !important; }}
    .m-quad {{ display: inline-block; width: 1.2em; }}

    /* Stacked fraction: numerator over horizontal bar over denominator */
    .m-frac {{
      display: inline-table;
      vertical-align: middle;
      text-align: center;
      margin: 0 2px;
      line-height: 1.15;
      font-size: 0.92em;
    }}
    .m-frac > .m-num {{
      display: table-row;
      border-bottom: 1.1px solid #0f172a;
    }}
    .m-frac > .m-num, .m-frac > .m-den {{
      display: block;
      padding: 0 4px;
    }}
    .m-frac > .m-num {{
      border-bottom: 1.1px solid #0f172a;
      padding-bottom: 1.5px;
      margin-bottom: 1.5px;
    }}

    /* Full-overbar square root */
    .m-sqrt {{
      display: inline-table;
      vertical-align: middle;
      margin: 0 2px;
      border-collapse: collapse;
    }}
    .m-sqrt-hook {{
      display: table-cell;
      vertical-align: bottom;
      width: 0.65em;
      padding: 0;
    }}
    .m-sqrt-svg {{
      display: block;
      width: 0.65em;
      height: 1.35em;
    }}
    .m-radicand {{
      display: table-cell;
      vertical-align: middle;
      border-top: 1.15px solid #0f172a;
      padding: 1px 4px 0 2px;
      line-height: 1.25;
      white-space: nowrap;
    }}

    /* Tall curly-brace equation system (cases) */
    .m-cases {{
      display: inline-flex;
      align-items: stretch;
      vertical-align: middle;
      margin: 0 4px;
    }}
    .m-brace-cell {{
      display: flex;
      align-items: stretch;
      width: 12px;
      margin-right: 4px;
    }}
    .m-brace-svg {{
      width: 12px;
      height: 100%;
    }}
    .m-cases-rows {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 6px;
      text-align: left;
      padding: 2px 0;
    }}
    .m-case-row {{
      display: block;
      white-space: nowrap;
    }}
    .m-big-delim {{
      font-size: 1.25em;
      vertical-align: middle;
      margin: 0 1px;
    }}
  </style>
</head>
<body>
  <main class="document-flow">
    <header class="hero-header">
      <div class="hero-kicker">ANALYTIC GEOMETRY · 高考解析几何小题体系</div>
      <h1>{html.escape(doc_title)}</h1>
      <p class="hero-subtitle">{html.escape(doc_subtitle)}</p>
      <div class="hero-stats">收录范围：{html.escape(scope_desc)}（共 {total_items} 个知识点）</div>
    </header>

    <nav class="toc-box">
      <div class="toc-heading">目录导航</div>
      <div class="toc-grid">
        {"".join(toc_blocks)}
      </div>
    </nav>

    {"".join(body_blocks)}
  </main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="按 outline.yaml 自动合并排版解析几何小题笔记为 PDF")
    parser.add_argument("--outline", default=str(REPO_ROOT / "outline.yaml"), help="outline.yaml 路径")
    parser.add_argument("--modules", default="", help="仅合并指定模块编号，如 01 或 01,02（默认合并全部）")
    parser.add_argument("--subsection", default="", help="仅合并标题包含该关键词的小节，如 对称问题")
    parser.add_argument("--title", default="", help="自定义封面标题")
    parser.add_argument("--subtitle", default="", help="自定义副标题")
    parser.add_argument("-o", "--output", default=str(REPO_ROOT / "output" / "解析几何小题合集.pdf"), help="输出 PDF 路径")
    parser.add_argument("--html-only", default="", help="仅输出自包含 HTML 文件路径")
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

    try:
        from weasyprint import HTML
    except ImportError:
        print("未安装 weasyprint，请先运行：pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    out_pdf = Path(args.output)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str, base_url=str(REPO_ROOT)).write_pdf(str(out_pdf))
    print(f"已生成合并 PDF：{out_pdf} ({out_pdf.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
