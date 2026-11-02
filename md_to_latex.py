#!/usr/bin/env python3
"""
Convertidor de Markdown a LaTeX con Portada Profesional
"""

import sys
import os
import pathlib
import re
import hashlib
import html
import json
import urllib.parse
import subprocess
import argparse
import shutil
import tempfile
from datetime import datetime

PDFLATEX_PATH = "/Library/TeX/texbin/pdflatex"
if not os.path.exists(PDFLATEX_PATH):
    PDFLATEX_PATH = shutil.which("pdflatex") or "pdflatex"

_INSTALL_HINTS = {
    "pdflatex": "Instala TeX Live o MacTeX (macOS: brew install --cask mactex-no-gui; "
                "Debian/Ubuntu: sudo apt install texlive-latex-extra texlive-lang-spanish texlive-extra-utils).",
    "pdfcrop": "pdfcrop viene con TeX Live (macOS: sudo tlmgr install pdfcrop; "
               "Debian/Ubuntu: sudo apt install texlive-extra-utils).",
    "chrome": "Instala Google Chrome, Chromium o Edge (macOS: brew install --cask google-chrome; "
              "Debian/Ubuntu: sudo apt install chromium; Windows: winget install Google.Chrome) "
              "o indica su ruta con la variable MD_LATEX_CHROME.",
}
_hinted = set()


def find_chrome(env=None, exists=os.path.isfile, which=shutil.which):
    """Localiza un navegador Chromium (Chrome, Chromium o Edge) en macOS, Linux o Windows.

    MD_LATEX_CHROME fuerza una ruta concreta. Devuelve None si no encuentra ninguno.
    """
    env = os.environ if env is None else env
    forced = env.get("MD_LATEX_CHROME")
    if forced:
        return forced if exists(forced) else None

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = env.get(var)
        if base:
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
            candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for path in candidates:
        if exists(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "chrome", "msedge", "microsoft-edge"):
        found = which(name)
        if found:
            return found
    return None


def tool_exists(path):
    """True si `path` es un ejecutable existente (ruta absoluta) o está en el PATH."""
    return bool(path) and (os.path.isfile(path) and os.access(path, os.X_OK) or shutil.which(path) is not None)


def warn_missing_tool(key, consequence):
    """Avisa una sola vez por herramienta que falta e indica cómo instalarla (README: Instalación)."""
    if key in _hinted:
        return
    _hinted.add(key)
    print(f"[!] No se encontró {key}: {consequence}")
    print(f"    {_INSTALL_HINTS[key]} Más detalle en el README, sección «Instalación de dependencias».")


def normalize_key(key):
    """Normaliza claves eliminando acentos y espacios."""
    k = key.strip().lower()
    k = k.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    k = re.sub(r'[^a-z0-9_]', '', k)
    return k


def parse_header_file(header_path):
    """Lee y parsea un archivo header.md con YAML o pares clave: valor."""
    if not os.path.exists(header_path):
        return {}
    with open(header_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    metadata = {}
    # 1. Si tiene frontmatter delimitado por ---
    fm_match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL | re.MULTILINE)
    raw_text = fm_match.group(1) if fm_match else content

    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Quitar guión inicial de listas markdown si existe (- titulo: ...)
        if line.startswith("- "):
            line = line[2:].strip()
        if ":" in line:
            key, val = line.split(":", 1)
            norm_key = normalize_key(key)
            val = val.strip().strip('"').strip("'")
            metadata[norm_key] = val

    return metadata


def extract_frontmatter(content):
    """Extrae metadatos de YAML frontmatter si existe dentro del mismo archivo."""
    metadata = {}
    pattern = r"^---\s*\n(.*?)\n---\s*\n"
    match = re.match(pattern, content, re.DOTALL)
    if match:
        raw_yaml = match.group(1)
        body = content[match.end():]
        for line in raw_yaml.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- "):
                line = line[2:].strip()
            if ":" in line:
                key, val = line.split(":", 1)
                norm_key = normalize_key(key)
                val = val.strip().strip('"').strip("'")
                metadata[norm_key] = val
        return metadata, body
    return metadata, content


LATEX_SPECIALS = {
    '&': r'\&', '%': r'\%', '#': r'\#', '_': r'\_', '$': r'\$',
    '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}',
    '^': r'\textasciicircum{}', '\\': r'\textbackslash{}',
}
MD_ESCAPABLE = r'\\`*_{}\[\]()#+\-.!|<>~$%&'


def escape_latex(text):
    """Escapa los caracteres especiales de LaTeX en texto plano."""
    return "".join(LATEX_SPECIALS.get(c, c) for c in text)


def unescape_markdown(text):
    """Quita los escapes de Markdown (\\_ -> _, \\{ -> {, etc.)."""
    return re.sub(r'\\([' + MD_ESCAPABLE + r'])', r'\1', text)


# --- Defensa contra inyección LaTeX -------------------------------------------------------------
# El .md es entrada no confiable: matemáticas, URLs y rutas se pasan a LaTeX casi tal cual, así que
# se validan para que no puedan leer archivos (\input), escribir (\write) ni redefinir el documento.
_MATH_ENVS = r"aligned|align\*?|cases|matrix|pmatrix|bmatrix|vmatrix|array|split|gathered|equation\*?"
UNSAFE_MATH = re.compile(
    r"\\(?!(?:begin|end)\{(?:" + _MATH_ENVS + r")\})"
    r"(?:begin|end|input|include|includeonly|import|subimport|openin|openout|read|readline|write|"
    r"newwrite|newread|immediate|special|catcode|csname|expandafter|let|futurelet|[gex]?def|"
    r"newcommand|renewcommand|providecommand|usepackage|RequirePackage|documentclass|"
    r"verbatiminput|lstinputlisting|includegraphics|includepdf|href|url|hyperref|detokenize|"
    r"scantokens|directlua|luaexec|ShellEscape|jobname|endinput|string|lstset|ExplSyntax|"
    r"[A-Za-z]*@[A-Za-z@]*)"
    r"|\^\^"
)
SAFE_URL_PREFIXES = ("http://", "https://", "mailto:", "#")
SAFE_IMAGE_PATH = re.compile(r"[A-Za-z0-9_\-./ ]+")


def is_safe_math(expr):
    """True si la expresión matemática no contiene comandos capaces de leer/escribir archivos."""
    return not UNSAFE_MATH.search(expr)


def math_fallback(expr):
    """Texto literal inofensivo que reemplaza una expresión matemática rechazada."""
    print("[!] Expresión matemática con comandos LaTeX no permitidos; se muestra como texto.")
    return r"\texttt{" + escape_latex(" ".join(expr.split())) + "}"


def latex_safe_url(url):
    """Devuelve la URL lista para \\href/\\url, o None si el esquema no está permitido."""
    url = url.strip()
    if not url.lower().startswith(SAFE_URL_PREFIXES):
        return None
    # quote deja fuera \ { } ^ $ espacios y comillas; '%' ya codificado se conserva y se escapa
    return urllib.parse.quote(url, safe="/:?=&@+,;!*'()-._~%#").replace("%", r"\%")


def safe_image_path(path):
    """Ruta relativa y sin caracteres de LaTeX, o None."""
    path = path.strip()
    if (SAFE_IMAGE_PATH.fullmatch(path) and not path.startswith("/")
            and ".." not in path.split("/")):
        return path
    return None


def convert_inline_formatting(text, breakable=False):
    """Convierte formato inline de Markdown a LaTeX protegiendo matemáticas y código.

    breakable=True (celdas de tabla): el código en línea puede partirse en cualquier carácter
    para que nunca se desborde de su columna.
    """
    inline_codes = []
    def save_inline_code(m):
        inline_codes.append(m.group(1))
        return f"QQQINLINECODE{len(inline_codes)-1}ZZZ"

    # Proteger código en línea `code`
    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # Proteger escapes de Markdown (\_ \{ \# ...) para no re-escaparlos ni tratarlos como énfasis
    # <br> dentro de celdas/párrafos -> salto de línea de LaTeX
    text = re.sub(r'\s*<br\s*/?>\s*', ' QQQBRZZZ ', text)

    md_escapes = []
    def save_md_escape(m):
        md_escapes.append(escape_latex(m.group(1)))
        return f"QQQESC{len(md_escapes)-1}ZZZ"
    text = re.sub(r'\\([' + MD_ESCAPABLE + r'])', save_md_escape, text)

    # Proteger matemáticas inline $...$
    math_inlines = []
    def save_math(m):
        math_inlines.append(m.group(0) if is_safe_math(m.group(1)) else math_fallback(m.group(1)))
        return f"QQQMATHINLINE{len(math_inlines)-1}ZZZ"
    text = re.sub(r'\$([^\$]+)\$', save_math, text)

    # Proteger imágenes ![alt](url) antes de proteger enlaces
    images = []
    def save_image(m):
        images.append((m.group(1), m.group(2)))
        return f"QQQIMAGE{len(images)-1}ZZZ"
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', save_image, text)

    # Proteger enlaces [texto](url) antes de escapar
    links = []
    def save_link(m):
        links.append((m.group(1), m.group(2)))
        return f"QQQLINK{len(links)-1}ZZZ"
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', save_link, text)

    # Escapar caracteres de texto normales (sin tocar tokens QQQ...ZZZ)
    text = escape_latex(text)

    # Negrita e itálica combinadas ***text***
    text = re.sub(r'\*\*\*([^*]+)\*\*\*', r'\\textbf{\\textit{\1}}', text)

    # Negrita **text** o __text__
    text = re.sub(r'\*\*([^*]+)\*\*', r'\\textbf{\1}', text)
    text = re.sub(r'\b__([^_]+)__\b', r'\\textbf{\1}', text)

    # Itálica *text* o _text_ (evitar tocar subíndices de variables o palabras compuestas)
    text = re.sub(r'\*([^*]+)\*', r'\\textit{\1}', text)
    text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\\textit{\1}', text)

    # Restaurar enlaces
    for i, (ltxt, lurl) in enumerate(links):
        clean_ltxt = convert_inline_formatting(ltxt, breakable)
        safe_url = latex_safe_url(lurl)
        if safe_url is None:
            print(f"[!] Enlace con esquema no permitido omitido: {lurl[:40]}")
            text = text.replace(f"QQQLINK{i}ZZZ", clean_ltxt)
        else:
            text = text.replace(f"QQQLINK{i}ZZZ", f"\\href{{{safe_url}}}{{{clean_ltxt}}}")

    # Restaurar imágenes
    for i, (alt, img_path) in enumerate(images):
        clean_alt = escape_latex(alt) if alt else ""
        img_path = safe_image_path(img_path)
        if img_path is None:
            print("[!] Ruta de imagen no permitida omitida (solo rutas relativas simples).")
            text = text.replace(f"QQQIMAGE{i}ZZZ", f"[imagen omitida: {clean_alt}]")
            continue
        caption_code = f"\\caption{{{clean_alt}}}" if clean_alt else ""
        img_code = (
            f"\\begin{{figure}}[htbp]\n"
            f"  \\centering\n"
            f"  \\includegraphics[width=0.85\\linewidth,height=0.45\\textheight,keepaspectratio]{{{img_path}}}\n"
            f"  {caption_code}\n"
            f"\\end{{figure}}"
        )
        text = text.replace(f"QQQIMAGE{i}ZZZ", img_code)

    # Restaurar matemáticas inline intactas
    for i, math_expr in enumerate(math_inlines):
        text = text.replace(f"QQQMATHINLINE{i}ZZZ", math_expr)

    text = text.replace("QQQBRZZZ", r"\newline{}")

    # Restaurar escapes de Markdown
    for i, esc in enumerate(md_escapes):
        text = text.replace(f"QQQESC{i}ZZZ", esc)

    # Restaurar código en línea (escapado y con puntos de corte de línea permitidos)
    for i, code_expr in enumerate(inline_codes):
        raw_code = unescape_markdown(code_expr)
        if breakable:
            safe = "".join(escape_latex(c) + r"\penalty3000\relax{}" for c in raw_code)
        else:
            safe = escape_latex(raw_code)
            safe = safe.replace(r'\_', r'\_\allowbreak{}').replace('/', r'/\allowbreak{}')
        text = text.replace(f"QQQINLINECODE{i}ZZZ", f"\\texttt{{{safe}}}")

    return text


def build_mermaid_html(mermaid_code, mermaid_js):
    """HTML que Chrome imprime a PDF. El código Mermaid viaja como dato JSON (nunca como HTML) para
    que un bloque con </div><script> no pueda ejecutar JavaScript en el navegador."""
    code_json = json.dumps(mermaid_code).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    src = html.escape(pathlib.Path(mermaid_js).as_uri(), quote=True)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src file: 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:">
<script src="{src}"></script>
<style>
@page {{ margin: 0; }}
body {{ margin: 0; padding: 25px; background: white; }}
</style>
</head>
<body>
<div class="mermaid" id="diagram"></div>
<script>
document.getElementById("diagram").textContent = {code_json};
mermaid.initialize({{ startOnLoad: false, theme: "neutral", securityLevel: "strict" }});
(async () => {{
  await mermaid.run({{ querySelector: ".mermaid" }});
  // Página PDF del tamaño exacto del diagrama: evita que Chrome lo parta en hojas carta
  const r = document.querySelector(".mermaid svg").getBoundingClientRect();
  const st = document.createElement("style");
  const w = Math.ceil(r.width) + 50, h = Math.ceil(r.height) + 50;
  st.textContent = "@page {{ size: " + w + "px " + h + "px; margin: 0; }} html, body {{ height: " + (h - 2) + "px; overflow: hidden; }}";
  document.head.appendChild(st);
}})();
</script>
</body>
</html>"""


def render_mermaid_diagram(mermaid_code, output_pdf_path):
    """Renderiza código Mermaid a un archivo PDF vectorial recortado usando Chrome headless y pdfcrop."""
    # 1. Intentar con Mermaid CLI (mmdc) si estuviera instalado
    mmdc_path = shutil.which("mmdc")
    if mmdc_path:
        with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as f:
            f.write(mermaid_code)
            tmp_mmd = f.name
        res = subprocess.run([mmdc_path, "-i", tmp_mmd, "-o", output_pdf_path, "-b", "transparent"], capture_output=True)
        if os.path.exists(tmp_mmd):
            try: os.remove(tmp_mmd)
            except OSError: pass
        if res.returncode == 0 and os.path.exists(output_pdf_path):
            print(f"[✓] Diagrama Mermaid compilado con mmdc: {output_pdf_path}")
            return True

    # 2. Renderizado nativo integrado usando templates/mermaid.min.js + Chrome Headless + pdfcrop
    chrome_bin = find_chrome()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    mermaid_js = os.path.join(script_dir, "templates", "mermaid.min.js")

    pdfcrop_bin = "/Library/TeX/texbin/pdfcrop"
    if not os.path.exists(pdfcrop_bin):
        pdfcrop_bin = shutil.which("pdfcrop") or "pdfcrop"

    if not tool_exists(chrome_bin):
        warn_missing_tool("chrome", "los diagramas Mermaid se incluirán como código.")
        return False
    if not tool_exists(pdfcrop_bin):
        warn_missing_tool("pdfcrop", "los diagramas Mermaid se incluirán como código.")
        return False

    if chrome_bin and os.path.exists(chrome_bin) and os.path.exists(mermaid_js):
        html_content = build_mermaid_html(mermaid_code, mermaid_js)

        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write(html_content)
            tmp_html = f.name
        
        tmp_raw_pdf = tmp_html.replace(".html", "_raw.pdf")
        tmp_user_dir = tempfile.mkdtemp(prefix="chrome_mermaid_")

        cmd = [
            chrome_bin,
            "--headless",
            "--disable-gpu",
            *(["--no-sandbox"] if os.environ.get("MD_LATEX_NO_SANDBOX") == "1" else []),  # solo Docker/root
            "--no-pdf-header-footer",
            "--virtual-time-budget=15000",
            f"--user-data-dir={tmp_user_dir}",
            f"--print-to-pdf={tmp_raw_pdf}",
            pathlib.Path(tmp_html).as_uri()
        ]
        
        res = subprocess.run(cmd, capture_output=True, text=True)
        
        if os.path.exists(tmp_raw_pdf):
            os.makedirs(os.path.dirname(output_pdf_path) or ".", exist_ok=True)
            res_crop = subprocess.run([pdfcrop_bin, "--margins", "5", tmp_raw_pdf, output_pdf_path], capture_output=True)
            
            # Limpieza
            for p in [tmp_html, tmp_raw_pdf]:
                if os.path.exists(p):
                    try: os.remove(p)
                    except OSError: pass
            try: shutil.rmtree(tmp_user_dir)
            except OSError: pass

            if res_crop.returncode == 0 and os.path.exists(output_pdf_path):
                print(f"[✓] Diagrama Mermaid compilado vectorialmente: {output_pdf_path}")
                return True

    return False


def strip_leading_markdown_toc(lines):
    """Quita un índice manual en Markdown al inicio del cuerpo (lista de enlaces a #anclas).

    LaTeX ya genera su propio índice con \\tableofcontents; mantener el manual lo duplicaría.
    """
    toc_item = re.compile(r'^\s*(?:[-*+]|\d+\.)\s+\[[^\]]+\]\(#[^)]*\)\s*$')
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    start = i
    while i < len(lines) and (toc_item.match(lines[i]) or not lines[i].strip()):
        i += 1
    if i == start or not toc_item.match(lines[start]):
        return lines
    if i < len(lines) and re.match(r'^\s*([-*_]\s*){3,}\s*$', lines[i]):
        i += 1  # la regla horizontal que separa el índice del contenido
    print(f"[*] Índice manual en Markdown omitido ({i - start} líneas): LaTeX genera el suyo")
    return lines[i:]


# Súbelo si cambias cómo se renderiza Mermaid (tema, tamaño de página...) para invalidar la caché
MERMAID_RENDER_VERSION = "1"

# Lenguajes que listings conoce (los demás se imprimen sin resaltado)
LISTINGS_LANGS = {
    "python": "Python", "py": "Python", "java": "Java", "sql": "SQL",
    "bash": "bash", "sh": "bash", "shell": "bash", "xml": "XML", "html": "HTML",
    "javascript": "Java", "js": "Java", "c": "C", "cpp": "C++", "c++": "C++",
}


def parse_markdown_to_latex(md_content, source_dir="sources"):
    """Convierte cuerpo Markdown completo a bloques LaTeX, renderizando diagramas Mermaid."""
    lines = strip_leading_markdown_toc(md_content.splitlines())
    latex_output = []
    
    in_code_block = False
    code_block_lang = ""
    code_lines = []
    mermaid_counter = 0

    in_math_block = False
    math_lines = []

    in_list = False
    list_type = None  # 'itemize' o 'enumerate'
    
    in_table = False
    table_rows = []

    def close_list():
        nonlocal in_list, list_type
        if in_list:
            latex_output.append(f"\\end{{{list_type}}}")
            in_list = False
            list_type = None

    def close_table():
        nonlocal in_table, table_rows
        if in_table and table_rows:
            num_cols = max(len(r) for r in table_rows)
            rows = [r + [""] * (num_cols - len(r)) for r in table_rows]
            # Reparto de anchos: 1) cada columna recibe como mínimo el ancho de su palabra más larga
            # (el código largo sí se parte, así que cuenta hasta 20 caracteres); 2) el resto se reparte
            # en proporción a lo que el contenido necesita. Así ninguna palabra se sale de su columna.
            def cell_metrics(cell):
                parts = re.split(r'(`[^`]*`)', cell)
                words, total_len = [], 0
                for part in parts:
                    is_code = part.startswith('`') and part.endswith('`') and len(part) > 1
                    clean = re.sub(r'[`*\\]', '', part)
                    total_len += len(clean)
                    for w in clean.split():
                        words.append(min(len(w), 20) if is_code else len(w))
                return total_len, max(words, default=0)

            mins, nats = [], []
            for c in range(num_cols):
                metrics = [cell_metrics(r[c]) for r in rows]
                mins.append(max(m[1] for m in metrics) * 1.2 + 2)
                nats.append(max(max(m[0] for m in metrics) * 1.1 + 2, mins[-1]))
            avail_chars = 100.0 - 2 * num_cols   # caracteres \small aprox. que caben en una línea
            if sum(nats) <= avail_chars:
                widths = nats
            else:
                spare = max(avail_chars - sum(mins), 0)
                extra_need = sum(n - m for n, m in zip(nats, mins)) or 1
                widths = [m + (n - m) * spare / extra_need for n, m in zip(nats, mins)]
            weights = [max(int(w * 10), 1) for w in widths]
            total = sum(weights)
            avail = r"(\linewidth-%d\tabcolsep)" % (2 * num_cols)
            col_spec = "".join(
                r">{\RaggedRight\arraybackslash}p{\dimexpr%s*%d/%d\relax}" % (avail, w, total)
                for w in weights
            )
            latex_output.append(r"{\small\pretolerance=-1\tolerance=9999\hbadness=10000")
            latex_output.append(f"\\begin{{longtable}}{{{col_spec}}}")
            header_line = " & ".join(f"\\textbf{{{convert_inline_formatting(c, True)}}}" for c in rows[0]) + r" \\"
            latex_output.append(r"\toprule")
            latex_output.append(header_line)
            latex_output.append(r"\midrule")
            latex_output.append(r"\endfirsthead")
            latex_output.append(r"\toprule")
            latex_output.append(header_line)
            latex_output.append(r"\midrule")
            latex_output.append(r"\endhead")
            latex_output.append(r"\bottomrule")
            latex_output.append(r"\endlastfoot")
            for row in rows[1:]:
                latex_output.append(" & ".join(convert_inline_formatting(c, True) for c in row) + r" \\")
            latex_output.append(r"\end{longtable}")
            latex_output.append(r"}")
            in_table = False
            table_rows = []

    # Si el cuerpo no tiene títulos de nivel 1 (empieza en ##, ###...), se sube todo para que
    # el título más alto sea la sección 1 y la numeración del índice arranque en 1 (no en 0.1).
    heading_levels = []
    fenced = False
    for ln in lines:
        if ln.strip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            hm = re.match(r'^(#{1,4})\s+', ln)
            if hm:
                heading_levels.append(len(hm.group(1)))
    heading_shift = (min(heading_levels) - 1) if heading_levels else 0

    i = 0
    while i < len(lines):
        line = lines[i]

        # Bloques de código (```)
        if line.strip().startswith("```"):
            if not in_code_block:
                close_list()
                close_table()
                in_code_block = True
                lang_candidate = line.strip()[3:].strip()
                code_block_lang = lang_candidate.lower() if lang_candidate else "text"
                code_lines = []
            else:
                in_code_block = False
                if code_block_lang == "mermaid":
                    mermaid_counter += 1
                    mermaid_code = "\n".join(code_lines)
                    # Ruta absoluta: LaTeX se compila en la carpeta del .tex, que puede no ser el cwd
                    img_dir = os.path.join(os.path.abspath(source_dir), "img")
                    # Caché: el nombre depende del código y de la versión del render, así que
                    # un diagrama sin cambios se reutiliza sin abrir Chrome.
                    digest = hashlib.sha1((MERMAID_RENDER_VERSION + mermaid_code).encode("utf-8")).hexdigest()[:12]
                    pdf_out = os.path.join(img_dir, f"mermaid_{digest}.pdf")
                    if os.path.exists(pdf_out) and os.path.getsize(pdf_out) > 0:
                        print(f"[=] Diagrama Mermaid {mermaid_counter} sin cambios (caché): {pdf_out}")
                        rendered = True
                    else:
                        rendered = render_mermaid_diagram(mermaid_code, pdf_out)
                    if rendered:
                        latex_output.append(
                            "\\begin{figure}[htbp]\n  \\centering\n"
                            f"  \\includegraphics[page=1,width=0.9\\linewidth,height=0.75\\textheight,keepaspectratio]{{{pdf_out}}}\n"
                            "\\end{figure}"
                        )
                        i += 1
                        continue
                    print("[!] No se pudo renderizar el diagrama Mermaid; se incluye como código.")
                lang_opt = f"[language={LISTINGS_LANGS[code_block_lang]}]" if code_block_lang in LISTINGS_LANGS else ""
                latex_output.append(f"\\begin{{lstlisting}}{lang_opt}")
                # El código no puede cerrar el entorno: "\end{lstlisting}" literal se rompe con un espacio
                latex_output.extend(l.replace(r"\end{lstlisting}", r"\end {lstlisting}") for l in code_lines)
                latex_output.append(r"\end{lstlisting}")
            i += 1
            continue

        if in_code_block:
            code_lines.append(line)
            i += 1
            continue

        # Bloques de matemáticas en display ($$)
        if line.strip() == "$$":
            if not in_math_block:
                close_list()
                close_table()
                in_math_block = True
                math_lines = []
            else:
                in_math_block = False
                if is_safe_math("\n".join(math_lines)):
                    latex_output.append(r"\[")
                    latex_output.extend(math_lines)
                    latex_output.append(r"\]")
                else:
                    latex_output.append(math_fallback("\n".join(math_lines)))
            i += 1
            continue

        if in_math_block:
            math_lines.append(line)
            i += 1
            continue

        # Ecuaciones en bloque en una sola línea $$...$$
        if line.strip().startswith("$$") and line.strip().endswith("$$") and len(line.strip()) > 2:
            close_list()
            close_table()
            eq_content = line.strip()[2:-2].strip()
            if is_safe_math(eq_content):
                latex_output.append(r"\[")
                latex_output.append(eq_content)
                latex_output.append(r"\]")
            else:
                latex_output.append(math_fallback(eq_content))
            i += 1
            continue

        # Tablas Markdown (| col 1 | col 2 |)
        if "|" in line and (line.strip().startswith("|") or line.strip().endswith("|")):
            close_list()
            if re.match(r'^\s*\|?\s*[-:]+[-|\s:]*$', line):
                i += 1
                continue
            cells = [c.strip() for c in line.split("|")]
            if line.strip().startswith("|") and len(cells) > 0 and cells[0] == "":
                cells.pop(0)
            if line.strip().endswith("|") and len(cells) > 0 and cells[-1] == "":
                cells.pop()
            
            if not in_table:
                in_table = True
                table_rows = [cells]
            else:
                table_rows.append(cells)
            i += 1
            continue
        else:
            if in_table:
                close_table()

        # Encabezados
        header_match = re.match(r'^(#{1,4})\s+(.+)$', line)
        if header_match:
            close_list()
            level = max(1, len(header_match.group(1)) - heading_shift)
            # Quitar numeración manual del título ("3 · ", "2.1. ", "4) ", "5 - ") porque LaTeX ya numera
            raw_title = re.sub(r'^\d+(?:\.\d+)*\s*[·.)\-–—:]\s+', '', header_match.group(2).strip())
            htext = convert_inline_formatting(raw_title)
            if level == 1:
                latex_output.append(f"\\section{{{htext}}}")
            elif level == 2:
                latex_output.append(f"\\subsection{{{htext}}}")
            elif level == 3:
                latex_output.append(f"\\subsubsection{{{htext}}}")
            elif level == 4:
                latex_output.append(f"\\paragraph{{{htext}}}")
            i += 1
            continue

        # Listas desordenadas (- item o * item)
        bullet_match = re.match(r'^\s*[-*+]\s+(.+)$', line)
        if bullet_match:
            item_text = convert_inline_formatting(bullet_match.group(1).strip())
            if not in_list or list_type != "itemize":
                close_list()
                in_list = True
                list_type = "itemize"
                latex_output.append(r"\begin{itemize}")
            latex_output.append(f"  \\item {item_text}")
            i += 1
            continue

        # Listas numeradas (1. item)
        num_match = re.match(r'^\s*\d+\.\s+(.+)$', line)
        if num_match:
            item_text = convert_inline_formatting(num_match.group(1).strip())
            if not in_list or list_type != "enumerate":
                close_list()
                in_list = True
                list_type = "enumerate"
                latex_output.append(r"\begin{enumerate}")
            latex_output.append(f"  \\item {item_text}")
            i += 1
            continue

        if line.strip() == "":
            close_list()
            latex_output.append("")
            i += 1
            continue

        close_list()

        # Citas (> texto)
        quote_match = re.match(r'^>\s*(.+)$', line)
        if quote_match:
            qtext = convert_inline_formatting(quote_match.group(1).strip())
            latex_output.append(r"\begin{quote}")
            latex_output.append(f"\\textit{{{qtext}}}")
            latex_output.append(r"\end{quote}")
            i += 1
            continue

        # Reglas horizontales (--- o ***)
        if re.match(r'^\s*([-*_]\s*){3,}\s*$', line):
            latex_output.append(r"\noindent\hrulefill\par\vspace{1em}")
            i += 1
            continue

        # Línea de texto normal
        formatted_line = convert_inline_formatting(line)
        latex_output.append(formatted_line)
        i += 1

    close_list()
    close_table()

    return "\n".join(latex_output)


def build_latex_document(metadata, body_latex, source_dir="sources"):
    """Construye el documento LaTeX completo con portada en tabla y márgenes optimizados."""
    titulo = metadata.get("titulo", metadata.get("title", "Documento Sin Título"))
    subtitulo = metadata.get("subtitulo", metadata.get("subtitle", ""))
    autor = metadata.get("autor", metadata.get("author", "Autor Desconocido"))
    institucion = metadata.get("institucion", metadata.get("institution", ""))
    area = metadata.get("area", "")
    departamento = metadata.get("departamento", metadata.get("department", ""))
    asignatura = metadata.get("asignatura", metadata.get("materia", ""))
    fecha = metadata.get("fecha", metadata.get("date", datetime.now().strftime("%d de %B de %Y")))

    # Mapeo de claves conocidas a etiquetas para la tabla de la portada
    LABEL_MAP = {
        "asignatura": "Asignatura:",
        "materia": "Materia:",
        "docente": "Docente:",
        "profesor": "Docente:",
        "profesora": "Docente:",
        "autor": "Autor:",
        "autores": "Autor(es):",
        "estudiante": "Estudiante:",
        "institucion": "Institución:",
        "universidad": "Universidad:",
        "facultad": "Facultad:",
        "area": "Área:",
        "departamento": "Departamento:",
        "repositorio": "Repositorio:",
        "repo": "Repositorio:",
        "codigo": "Código:",
        "fecha": "Fecha:",
        "periodo": "Período:",
    }

    def format_table_value(val):
        """Formatea valores de la tabla de metadatos, reconociendo URLs y ramas git."""
        val = str(val).strip()
        url_match = re.search(r'(https?://[^\s()]+)', val)
        if url_match and latex_safe_url(url_match.group(1)):
            url = latex_safe_url(url_match.group(1))
            before = val[:url_match.start()]
            after = val[url_match.end():]
            clean_before = escape_latex(before)
            
            branch_match = re.search(r'\(rama\s+([^\)]+)\)', after)
            if branch_match:
                branch_name = branch_match.group(1).strip()
                clean_after = f" (rama \\texttt{{{escape_latex(branch_name)}}})"
            else:
                clean_after = escape_latex(after)
            return f"{clean_before}\\url{{{url}}}{clean_after}"
        return escape_latex(val)

    # Construir filas dinámicas para la tabla de portada
    table_rows = []
    excluded_keys = {"titulo", "title", "subtitulo", "subtitle"}
    for k, v in metadata.items():
        if k in excluded_keys or not str(v).strip():
            continue
        label = LABEL_MAP.get(k, f"{escape_latex(k.capitalize())}:")
        formatted_val = format_table_value(v)
        table_rows.append(f"    \\textbf{{{label}}} & {formatted_val} \\\\")

    table_rows_latex = "\n".join(table_rows)

    # Encabezado institucional superior si aplica (solo si no está repetido en la tabla)
    top_inst_latex = ""
    if institucion and institucion.lower() not in asignatura.lower():
        top_inst_latex = r"{\scshape\Large\color{primarycolor} " + escape_latex(institucion) + r"\par}\vspace{0.3cm}"
    if area or departamento:
        partes = [p for p in [area, departamento] if p and p.lower() not in asignatura.lower()]
        if partes:
            top_inst_latex += r"{\large\color{accentcolor} " + escape_latex(" | ".join(partes)) + r"\par}\vspace{0.3cm}"

    # Encabezado de páginas interiores: solo asignatura (izquierda) y fecha (derecha)
    header_left = escape_latex(asignatura.split('—')[0].strip()) if asignatura else ""

    # Normalizar ruta de carpeta de fuentes para graphicspath
    clean_src = os.path.abspath(source_dir or "sources").rstrip("/\\")
    if re.search(r'[{}\\%#$^~&]', clean_src):
        print("[!] La carpeta de fuentes tiene caracteres no válidos para LaTeX; se usa 'sources' en \\graphicspath.")
        clean_src = "sources"

    doc = r"""\documentclass[11pt,letterpaper]{article}

% --- Paquetes Esenciales ---
\usepackage[utf8]{inputenc}
% Símbolos Unicode frecuentes que inputenc no define por defecto
\DeclareUnicodeCharacter{2264}{\ensuremath{\leq}}
\DeclareUnicodeCharacter{2265}{\ensuremath{\geq}}
\DeclareUnicodeCharacter{2260}{\ensuremath{\neq}}
\DeclareUnicodeCharacter{2248}{\ensuremath{\approx}}
\DeclareUnicodeCharacter{2192}{\ensuremath{\rightarrow}}
\DeclareUnicodeCharacter{2190}{\ensuremath{\leftarrow}}
\DeclareUnicodeCharacter{2194}{\ensuremath{\leftrightarrow}}
\DeclareUnicodeCharacter{00D7}{\ensuremath{\times}}
\usepackage[T1]{fontenc}
\usepackage[spanish,es-tabla]{babel}
% Márgenes optimizados para mayor aprovechamiento horizontal y separación vertical del header
\usepackage[left=2cm,right=2cm,top=3cm,bottom=2.5cm,headheight=18pt,headsep=28pt]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
% Rutas de búsqueda automática para imágenes (incluyendo carpeta sources y sus subcarpetas)
\graphicspath{{./}{""" + clean_src + r"""/}{""" + clean_src + r"""/img/}{""" + clean_src + r"""/imagenes/}{""" + clean_src + r"""/images/}{""" + clean_src + r"""/assets/}{./img/}{./imagenes/}{./images/}{./assets/}}
\usepackage{booktabs}
\usepackage{array}
\usepackage{ragged2e}  % \RaggedRight: alineado a la izquierda pero con guionado de palabras
\setlength{\emergencystretch}{3em}
\usepackage{longtable}
\usepackage{tabularx}
\usepackage{xcolor}
\usepackage{listings}
\usepackage{fancyhdr}
\usepackage{zref-totpages}  % total de páginas físicas (para descontar la portada)
\usepackage{titlesec}
\usepackage{hyperref}

% --- Colores Corporativos ---
\definecolor{primarycolor}{RGB}{20, 50, 90}     % Azul marino profundo
\definecolor{accentcolor}{RGB}{41, 128, 185}    % Azul acento
\definecolor{codebg}{RGB}{247, 249, 252}        % Fondo para código
\definecolor{codeframe}{RGB}{220, 226, 235}     % Borde de código
\definecolor{commentgreen}{RGB}{46, 125, 50}    % Verde comentarios
\definecolor{stringred}{RGB}{183, 28, 28}       % Rojo cadenas

% --- Configuración de Formato de Títulos ---
\titleformat{\section}
  {\normalfont\Large\bfseries\color{primarycolor}}{\thesection}{1em}{}[\color{primarycolor}\titlerule]
\titleformat{\subsection}
  {\normalfont\large\bfseries\color{accentcolor}}{\thesubsection}{1em}{}
\titleformat{\subsubsection}
  {\normalfont\normalsize\bfseries\color{black!85}}{\thesubsubsection}{1em}{}

% --- Configuración de Listings (Código) con soporte de acentos ---
\lstset{
    inputencoding=utf8,
    extendedchars=true,
    literate=
        {á}{{\'a}}1 {é}{{\'e}}1 {í}{{\'i}}1 {ó}{{\'o}}1 {ú}{{\'u}}1
        {Á}{{\'A}}1 {É}{{\'E}}1 {Í}{{\'I}}1 {Ó}{{\'O}}1 {Ú}{{\'U}}1
        {ñ}{{\~n}}1 {Ñ}{{\~N}}1 {¿}{{?`}}1 {¡}{{!`}}1,
    backgroundcolor=\color{codebg},
    frame=single,
    rulecolor=\color{codeframe},
    basicstyle=\ttfamily\footnotesize,
    keywordstyle=\color{primarycolor}\bfseries,
    commentstyle=\color{commentgreen}\itshape,
    stringstyle=\color{stringred},
    numbers=left,
    numberstyle=\tiny\color{black!40},
    stepnumber=1,
    numbersep=8pt,
    tabsize=2,
    breaklines=true,
    breakatwhitespace=false,
    showspaces=false,
    showstringspaces=false,
    captionpos=b
}

% --- Configuración de Hipervínculos ---
\hypersetup{
    plainpages=false,
    pdfpagelabels=true,
    colorlinks=true,
    linkcolor=primarycolor,
    citecolor=accentcolor,
    urlcolor=accentcolor,
    pdfauthor={""" + escape_latex(autor) + r"""},
    pdftitle={""" + escape_latex(titulo) + r"""}
}

% --- Encabezados y Pies de Página ---
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small\color{black!80} """ + header_left + r"""}
\fancyhead[R]{\small\color{black!70} """ + escape_latex(fecha) + r"""}
\fancyfoot[R]{\small\color{black!60} Página \thepage\ de \the\numexpr\ztotpages-1\relax}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\footrulewidth}{0.4pt}

\begin{document}

% ==============================================================================
% PORTADA CON TABLA DE METADATOS
% ==============================================================================
\begin{titlepage}
    \newgeometry{left=2cm,right=2cm,top=2.5cm,bottom=2.5cm}
    \centering
    
    """ + top_inst_latex + r"""
    \vspace{2.5cm}
    
    {\color{primarycolor}\rule{\linewidth}{1.5pt}\par}
    \vspace{0.6cm}
    {\fontsize{24}{28}\selectfont\bfseries\color{primarycolor} """ + escape_latex(titulo) + r"""\par}
    \vspace{0.4cm}
    """ + (r"{\large\itshape\color{black!70} " + escape_latex(subtitulo) + r"\par}" if subtitulo else "") + r"""
    \vspace{0.6cm}
    {\color{primarycolor}\rule{\linewidth}{1.5pt}\par}
    
    \vfill
    
    % Tabla de Metadatos institucional
    \noindent
    \renewcommand{\arraystretch}{1.4}
    \begin{tabularx}{\linewidth}{@{} l X @{}}
    \toprule[1.2pt]
""" + table_rows_latex + r"""
    \bottomrule[1.2pt]
    \end{tabularx}
    
    \vspace{0.8cm}
    \restoregeometry
\end{titlepage}
% La portada no cuenta: la numeración de páginas arranca en 1 con el índice
\pagenumbering{arabic}

% ==============================================================================
% CONTENIDO PRINCIPAL
% ==============================================================================
\clearpage
\tableofcontents
\clearpage

""" + body_latex + r"""

\end{document}
"""
    return doc


def compile_latex(tex_file_path):
    """Compila el archivo .tex a .pdf utilizando pdflatex (2 pasadas para referencias)."""
    dir_name = os.path.dirname(tex_file_path) or "."
    base_name = os.path.basename(tex_file_path)
    cmd = [PDFLATEX_PATH, "-interaction=nonstopmode", base_name]

    if not tool_exists(PDFLATEX_PATH):
        warn_missing_tool("pdflatex", f"no se puede generar el PDF; el .tex quedó en {tex_file_path}.")
        print("    Mientras tanto puedes usar --no-compile para generar solo el .tex.")
        return False

    print(f"[*] Compilando {base_name} con {PDFLATEX_PATH} (Pasada 1)...")
    res1 = subprocess.run(cmd, cwd=dir_name, capture_output=True, text=True, errors="replace")
    if res1.returncode != 0:
        print("[!] Error en la compilación LaTeX:")
        for line in res1.stdout.splitlines()[-25:]:
            print("   ", line)
        return False

    print(f"[*] Compilando (Pasada 2 para índice y referencias)...")
    res2 = subprocess.run(cmd, cwd=dir_name, capture_output=True, text=True, errors="replace")

    # Limpiar archivos temporales
    root_name = os.path.splitext(base_name)[0]
    for ext in [".aux", ".log", ".out", ".toc"]:
        aux_file = os.path.join(dir_name, root_name + ext)
        if os.path.exists(aux_file):
            try:
                os.remove(aux_file)
            except OSError:
                pass

    pdf_file = os.path.join(dir_name, root_name + ".pdf")
    if os.path.exists(pdf_file):
        print(f"[✓] Documento PDF generado exitosamente: {pdf_file}")
        return True
    return False


class InputError(Exception):
    """Entradas inválidas o ausentes; `problems` lista cada causa en lenguaje claro."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


def _check_text_file(path, label, problems):
    """Agrega a `problems` cualquier motivo por el que `path` no sirva como archivo de texto."""
    if not os.path.exists(path):
        problems.append(f"{label} no existe: '{path}'")
    elif not os.path.isfile(path):
        problems.append(f"{label} no es un archivo: '{path}'")
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                if not f.read().strip():
                    problems.append(f"{label} está vacío: '{path}'")
        except (OSError, UnicodeDecodeError) as e:
            problems.append(f"{label} no se puede leer como UTF-8 ('{path}'): {e}")


def resolve_inputs(target="", source_dir_arg=None, body_arg=None, header_arg=None):
    """Determina (source_dir, body_path, header_path) y valida que existan y tengan contenido.

    El body es obligatorio. El header es opcional si los metadatos llegan por el frontmatter del
    body o por argumentos de CLI; un --header explícito que no exista siempre es error.
    Lanza InputError con todos los problemas encontrados a la vez.
    """
    problems = []
    target = (target or "").strip()
    source_dir = "."
    body_candidate = body_arg

    if source_dir_arg:
        source_dir = source_dir_arg
        if not os.path.isdir(source_dir):
            problems.append(f"La carpeta de fuentes no existe o no es una carpeta: '{source_dir}'")
    elif target and os.path.isdir(target):
        source_dir = target
    elif target and os.path.isfile(target):
        body_candidate = target
        source_dir = os.path.dirname(target) or "."
    elif target:
        problems.append(f"'{target}' no es una carpeta ni un archivo .md existente")
    elif os.path.isdir("sources"):
        source_dir = "sources"

    # Body: el indicado por CLI o el que haya por defecto en la carpeta de fuentes
    body_path = None
    if body_candidate:
        body_path = body_candidate
        _check_text_file(body_path, "El archivo de contenido (body)", problems)
    elif not problems:
        expected = os.path.join(source_dir, "body.md")
        if os.path.isfile(expected):
            body_path = expected
        else:
            others = sorted(
                f for f in os.listdir(source_dir)
                if f.endswith(".md") and not f.startswith("header") and os.path.isfile(os.path.join(source_dir, f))
            ) if os.path.isdir(source_dir) else []
            if others:
                body_path = os.path.join(source_dir, others[0])
            elif os.path.isfile("body.md"):
                body_path = "body.md"
            else:
                problems.append(
                    f"No hay body: no se encontró body.md (ni otro .md de contenido) en '{source_dir}' "
                    "ni en el directorio actual; indica uno con --body o pasa una carpeta que lo tenga")
        if body_path:
            _check_text_file(body_path, "El archivo de contenido (body)", problems)

    # Header: el indicado por CLI (debe existir) o header.md por defecto
    header_path = header_arg
    if header_path:
        _check_text_file(header_path, "El archivo de cabecera (header)", problems)
    elif not problems:
        for candidate in (os.path.join(source_dir, "header.md"), "header.md"):
            if os.path.isfile(candidate):
                header_path = candidate
                break
        if header_path:
            _check_text_file(header_path, "El archivo de cabecera (header)", problems)
        else:
            print(f"[!] No hay header.md en '{source_dir}': los metadatos de la portada deben venir del "
                  "frontmatter del body o de --titulo, --autor, --institucion...")

    if problems:
        raise InputError(problems)
    return source_dir, body_path, header_path


def main():
    parser = argparse.ArgumentParser(description="Convertir archivos Markdown a LaTeX con portada profesional")
    parser.add_argument("target", nargs="?", default="", help="Carpeta de fuentes (ej. sources/) o archivo .md de contenido")
    parser.add_argument("--source-dir", "-s", help="Carpeta que contiene los archivos de origen (.md, imágenes, etc.)")
    parser.add_argument("--header", help="Ruta al archivo de cabecera/metadatos (por defecto busca header.md)")
    parser.add_argument("--body", help="Ruta al archivo con el contenido (alternativa al argumento posicional)")
    parser.add_argument("--output", "-o", help="Nombre o ruta del archivo de salida (.tex/.pdf)")
    parser.add_argument("--titulo", help="Título del documento")
    parser.add_argument("--subtitulo", help="Subtítulo del documento")
    parser.add_argument("--autor", help="Autor del documento")
    parser.add_argument("--institucion", help="Institución u Organización")
    parser.add_argument("--area", help="Área o Facultad")
    parser.add_argument("--departamento", help="Departamento o Asignatura")
    parser.add_argument("--fecha", help="Fecha del documento")
    parser.add_argument("--check-only", action="store_true", help="Solo verificar si faltan metadatos y salir")
    parser.add_argument("--no-compile", action="store_true", help="Solo generar el archivo .tex sin compilar a PDF")

    args = parser.parse_args()

    # 1-3. Resolver y validar carpeta de fuentes, body y header ANTES de hacer cualquier trabajo
    try:
        source_dir, body_path, header_path = resolve_inputs(
            args.target, args.source_dir, args.body, args.header)
    except InputError as e:
        print("[✗] No se puede iniciar: faltan o son inválidos los archivos de entrada:")
        for problem in e.problems:
            print(f"  - {problem}")
        print("    Uso: python3 md_to_latex.py <carpeta>/   (con body.md y header.md dentro)")
        print("         python3 md_to_latex.py --body ruta/body.md --header ruta/header.md")
        sys.exit(1)

    print(f"[*] Carpeta de fuentes y recursos: {os.path.abspath(source_dir)}")
    print(f"[*] Archivo de contenido: {body_path}")

    metadata = {}
    if header_path and os.path.exists(header_path):
        print(f"[*] Archivo de cabecera: {header_path}")
        metadata.update(parse_header_file(header_path))

    with open(body_path, "r", encoding="utf-8") as f:
        body_content = f.read()

    # Si el propio archivo body tiene frontmatter, combinarlo
    fm_meta, body_md = extract_frontmatter(body_content)
    metadata.update(fm_meta)

    # Argumentos explícitos por CLI tienen la máxima prioridad
    if args.titulo: metadata["titulo"] = args.titulo
    if args.subtitulo: metadata["subtitulo"] = args.subtitulo
    if args.autor: metadata["autor"] = args.autor
    if args.institucion: metadata["institucion"] = args.institucion
    if args.area: metadata["area"] = args.area
    if args.departamento: metadata["departamento"] = args.departamento
    if args.fecha: metadata["fecha"] = args.fecha

    # Validar campos esenciales para portada
    missing = []
    if not metadata.get("titulo") and not metadata.get("title"):
        first_h1 = re.search(r'^#\s+(.+)$', body_md, re.MULTILINE)
        if first_h1:
            metadata["titulo"] = first_h1.group(1).strip()
        else:
            missing.append("Título")
            
    if not metadata.get("autor") and not metadata.get("author"):
        missing.append("Autor")
        
    if not metadata.get("institucion") and not metadata.get("institution") and not metadata.get("asignatura") and not metadata.get("materia"):
        missing.append("Institución / Asignatura")

    if args.check_only:
        if missing:
            print("MISSING:" + ",".join(missing))
            sys.exit(2)
        else:
            print("READY")
            sys.exit(0)

    if missing:
        print(f"[!] Faltan los siguientes datos indispensables para la portada:")
        for m in missing:
            print(f"  - {m}")
        print("Por favor, proporcione estos datos antes de generar el documento.")
        sys.exit(2)

    body_latex = parse_markdown_to_latex(body_md, source_dir=source_dir)
    full_latex = build_latex_document(metadata, body_latex, source_dir=source_dir)

    if args.output:
        base_path = os.path.splitext(args.output)[0]
    else:
        base_name = os.path.splitext(os.path.basename(body_path))[0]
        base_path = "documento" if base_name in ("body", "sources") else base_name

    tex_path = base_path + ".tex"
    os.makedirs(os.path.dirname(os.path.abspath(tex_path)), exist_ok=True)

    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(full_latex)

    print(f"[✓] Archivo LaTeX generado: {tex_path}")

    if not args.no_compile:
        success = compile_latex(tex_path)
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
