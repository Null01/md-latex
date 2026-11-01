#!/usr/bin/env python3
"""
Pruebas unitarias de md_to_latex.py.

Ejecutar desde la carpeta md-to-latex/:
    python3 -m unittest -v test_md_to_latex

Cada prueba protege un comportamiento que ya se rompió alguna vez al modificar el script.
La prueba de compilación real se omite sola si no hay pdflatex instalado.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import md_to_latex as m  # noqa: E402

SCRIPT = os.path.join(HERE, "md_to_latex.py")


def body(md):
    return m.parse_markdown_to_latex(md)


class EscapeTests(unittest.TestCase):
    def test_escapa_caracteres_especiales(self):
        self.assertEqual(m.escape_latex("a_b & c % d # e $"), r"a\_b \& c \% d \# e \$")

    def test_escapa_llaves_y_virgulilla(self):
        out = m.escape_latex("{x}~^")
        self.assertIn(r"\{x\}", out)
        self.assertIn(r"\textasciitilde{}", out)
        self.assertIn(r"\textasciicircum{}", out)

    def test_escapes_de_markdown_no_se_duplican(self):
        # Regresión: "\#" en el .md producía "\\#" y LaTeX fallaba con "Missing } inserted"
        out = m.convert_inline_formatting(r"| \# | alert\_id | \{x\} |")
        self.assertNotIn(r"\\#", out)
        self.assertIn(r"\#", out)
        self.assertIn(r"alert\_id", out)
        self.assertIn(r"\{x\}", out)

    def test_unescape_markdown(self):
        self.assertEqual(m.unescape_markdown(r"a\_b \{c\}"), "a_b {c}")


class InlineTests(unittest.TestCase):
    def test_negrita_y_cursiva(self):
        self.assertIn(r"\textbf{hola}", m.convert_inline_formatting("**hola**"))
        self.assertIn(r"\textit{hola}", m.convert_inline_formatting("*hola*"))

    def test_codigo_en_linea_se_escapa(self):
        out = m.convert_inline_formatting(r"`GET /api/{alert\_id}`")
        self.assertIn(r"\texttt{", out)
        compact = out.replace(r"\allowbreak{}", "")
        self.assertIn(r"\{alert\_id\}", compact)
        self.assertNotIn("QQQ", out)

    def test_codigo_en_tabla_es_partible_en_cualquier_caracter(self):
        out = m.convert_inline_formatting("`abcdef`", breakable=True)
        self.assertIn(r"\penalty", out)
        self.assertNotIn(r"\penalty", m.convert_inline_formatting("`abcdef`"))

    def test_math_inline_se_respeta(self):
        self.assertIn(r"$x_1$", m.convert_inline_formatting("valor $x_1$"))

    def test_enlace(self):
        out = m.convert_inline_formatting("[sitio](https://example.com)")
        self.assertIn(r"\href{https://example.com}{sitio}", out)

    def test_br_se_vuelve_salto_de_linea(self):
        out = m.convert_inline_formatting("uno<br>dos")
        self.assertIn(r"\newline{}", out)
        self.assertNotIn("<br>", out)

    def test_sin_tokens_internos_residuales(self):
        out = m.convert_inline_formatting(r"**a** `b` \_ [c](#d) $e$")
        self.assertNotIn("QQQ", out)


class HeadingTests(unittest.TestCase):
    def test_niveles_basicos(self):
        out = body("# A\n\n## B\n\n### C\n")
        self.assertIn(r"\section{A}", out)
        self.assertIn(r"\subsection{B}", out)
        self.assertIn(r"\subsubsection{C}", out)

    def test_cuerpo_sin_h1_arranca_en_seccion_1(self):
        # Regresión: el índice salía como 0.1, 0.2...
        out = body("## Contexto\n\n### Detalle\n")
        self.assertIn(r"\section{Contexto}", out)
        self.assertIn(r"\subsection{Detalle}", out)
        self.assertNotIn(r"\subsubsection", out)

    def test_numeracion_manual_se_elimina(self):
        out = body("# 3 · Capa de presentación\n\n## 2.1. Actor\n\n## 4) Otro\n")
        self.assertIn(r"\section{Capa de presentación}", out)
        self.assertIn(r"\subsection{Actor}", out)
        self.assertIn(r"\subsection{Otro}", out)

    def test_titulo_que_no_es_numeracion_se_conserva(self):
        out = body("# 2026 plan\n")
        self.assertIn(r"\section{2026 plan}", out)

    def test_hashes_dentro_de_codigo_no_cuentan_como_titulos(self):
        out = body("## Real\n\n```bash\n# comentario\n```\n")
        self.assertIn(r"\section{Real}", out)


class TocTests(unittest.TestCase):
    TOC = (
        "1. [Contexto](#contexto)\n"
        "2. [Alcance](#alcance)\n"
        "   1. [Actor](#actor)\n"
        "\n---\n\n## Contexto\n\nTexto.\n"
    )

    def test_indice_manual_inicial_se_omite(self):
        out = body(self.TOC)
        self.assertNotIn(r"\begin{enumerate}", out)
        self.assertNotIn(r"\href{#", out)
        self.assertIn(r"\section{Contexto}", out)

    def test_lista_normal_no_se_omite(self):
        out = body("1. uno\n2. dos\n\n## T\n")
        self.assertIn(r"\begin{enumerate}", out)

    def test_enlaces_externos_al_inicio_no_son_indice(self):
        lines = ["- [Sitio](https://example.com)", "", "Texto"]
        self.assertEqual(m.strip_leading_markdown_toc(lines), lines)


class ListAndBlockTests(unittest.TestCase):
    def test_listas(self):
        self.assertIn(r"\begin{itemize}", body("- a\n- b\n"))
        self.assertIn(r"\begin{enumerate}", body("1. a\n2. b\n"))

    def test_bloque_de_codigo(self):
        out = body("```python\nx = 1\n```\n")
        self.assertIn(r"\begin{lstlisting}[language=Python]", out)
        self.assertIn("x = 1", out)

    def test_lenguaje_desconocido_no_rompe_listings(self):
        out = body("```json\n{}\n```\n")
        self.assertIn(r"\begin{lstlisting}", out)
        self.assertNotIn("language=json", out)

    def test_ecuacion_en_bloque(self):
        out = body("$$\nx^2\n$$\n")
        self.assertIn(r"\[", out)
        self.assertIn("x^2", out)

    def test_imagen_se_vuelve_figura(self):
        out = body("![Mi figura](img/a.png)\n")
        self.assertIn(r"\includegraphics", out)
        self.assertIn(r"\caption{Mi figura}", out)


class TableTests(unittest.TestCase):
    MD = (
        "| A | B |\n|---|---|\n"
        "| `core` → `notify` | texto largo de ejemplo |\n"
        "| x<br>y | 2 |\n"
    )

    def test_usa_longtable_con_columnas_p(self):
        out = body(self.MD)
        self.assertIn(r"\begin{longtable}", out)
        self.assertIn(r"p{\dimexpr", out)
        self.assertIn(r"\endhead", out)

    def test_anchos_son_enteros_validos_para_dimexpr(self):
        # Regresión: un decimal dentro de \dimexpr imprimía ".1287" y desbordaba la tabla
        import re
        out = body(self.MD)
        for expr in re.findall(r"\\dimexpr(.*?)\\relax", out):
            self.assertNotRegex(expr, r"\*\d*\.\d+")

    def test_tolerancia_alta_dentro_de_tablas(self):
        # Regresión: sin esto LaTeX partía palabras en vez de bajarlas de renglón
        out = body(self.MD)
        self.assertIn(r"\pretolerance=-1", out)
        self.assertIn(r"\tolerance=9999", out)

    def test_encabezado_y_filas(self):
        out = body(self.MD)
        self.assertIn(r"\textbf{A}", out)
        self.assertIn(r"\newline{}", out)

    def test_tabla_con_filas_desiguales(self):
        out = body("| A | B | C |\n|---|---|---|\n| 1 | 2 |\n")
        self.assertIn(r"\begin{longtable}", out)


class HeaderTests(unittest.TestCase):
    def test_normalize_key(self):
        self.assertEqual(m.normalize_key(" Institución "), "institucion")

    def test_parse_header_con_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "header.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write('---\n# comentario\ntitulo: "Mi título"\nautor: Ana\n---\n')
            meta = m.parse_header_file(path)
        self.assertEqual(meta["titulo"], "Mi título")
        self.assertEqual(meta["autor"], "Ana")

    def test_header_inexistente(self):
        self.assertEqual(m.parse_header_file("/no/existe/header.md"), {})

    def test_extract_frontmatter(self):
        meta, rest = m.extract_frontmatter("---\ntitulo: X\n---\n# Cuerpo\n")
        self.assertEqual(meta["titulo"], "X")
        self.assertTrue(rest.startswith("# Cuerpo"))


class DocumentTemplateTests(unittest.TestCase):
    META = {
        "titulo": "Título de prueba",
        "autor": "Ana Pérez / CC. 1",
        "asignatura": "Arquitectura I (2026-2) — Universidad X",
        "fecha": "Noviembre 2026",
    }

    def setUp(self):
        self.doc = m.build_latex_document(self.META, "CUERPO", source_dir="sources")

    def test_encabezado_solo_asignatura_y_fecha(self):
        self.assertIn(r"\fancyhead[L]{\small\color{black!80} Arquitectura I (2026-2)}", self.doc)
        self.assertIn("Noviembre 2026", self.doc.split(r"\fancyhead[R]")[1].split("\n")[0])
        # El título no debe aparecer en el encabezado de páginas interiores
        self.assertNotIn("Título de prueba", self.doc.split(r"\fancyhead[L]")[1].split("\n")[0])

    def test_pie_sin_autor_y_con_pagina(self):
        footer = self.doc.split(r"\fancyfoot")[1:]
        self.assertTrue(any("Página" in f for f in footer))
        self.assertNotIn(r"\fancyfoot[L]", self.doc)

    def test_numeracion_arranca_en_1_despues_de_la_portada(self):
        after_cover = self.doc.split(r"\end{titlepage}")[1]
        self.assertIn(r"\pagenumbering{arabic}", after_cover.split(r"\tableofcontents")[0])

    def test_total_de_paginas_descuenta_la_portada(self):
        self.assertIn(r"\ztotpages-1", self.doc)

    def test_simbolos_unicode_declarados(self):
        for code in ("2264", "2265", "2192"):
            self.assertIn(rf"\DeclareUnicodeCharacter{{{code}}}", self.doc)

    def test_paquetes_necesarios(self):
        for pkg in ("longtable", "ragged2e", "listings", "zref-totpages", "graphicx"):
            self.assertIn(pkg, self.doc)

    def test_incluye_cuerpo_e_indice(self):
        self.assertIn("CUERPO", self.doc)
        self.assertIn(r"\tableofcontents", self.doc)


class CliTests(unittest.TestCase):
    """Ejecuta el script real en una carpeta temporal (sin compilar: --no-compile)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "header.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitulo: Prueba\nautor: Ana\nasignatura: Materia — U\n---\n")
        with open(os.path.join(self.src, "body.md"), "w", encoding="utf-8") as f:
            f.write("## Intro\n\nHola **mundo**.\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, "src", "--no-compile", *args],
            cwd=self.tmp, capture_output=True, text=True,
        )

    def test_sin_output_crea_el_documento_por_defecto(self):
        res = self.run_cli()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "documento.tex")))

    def test_con_output_no_crea_el_por_defecto(self):
        res = self.run_cli("--output", "mio")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "mio.tex")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "documento.tex")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "testing.tex")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "testing.pdf")))

    def test_faltan_datos_de_portada_sale_con_codigo_2(self):
        with open(os.path.join(self.src, "header.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitulo: Solo título\n---\n")
        res = subprocess.run(
            [sys.executable, SCRIPT, "src", "--check-only"],
            cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("MISSING", res.stdout)

    def test_check_only_listo(self):
        res = subprocess.run(
            [sys.executable, SCRIPT, "src", "--check-only"],
            cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("READY", res.stdout)


@unittest.skipUnless(shutil.which("pdflatex") or os.path.exists(m.PDFLATEX_PATH), "pdflatex no instalado")
class CompileTests(unittest.TestCase):
    """Compilación real: atrapa cualquier cambio que genere LaTeX inválido."""

    def test_documento_con_casos_dificiles_compila(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(src)
            with open(os.path.join(src, "header.md"), "w", encoding="utf-8") as f:
                f.write("---\ntitulo: Prueba\nautor: Ana\nasignatura: Materia — U\nfecha: Nov 2026\n---\n")
            with open(os.path.join(src, "body.md"), "w", encoding="utf-8") as f:
                f.write(
                    "1. [Intro](#intro)\n\n---\n\n"
                    "## 1 · Intro\n\nTexto con `a_b`, \\_ y ≤ 5 → listo & 100%.\n\n"
                    "### Tabla\n\n"
                    "| \\# | Ruta | Origen |\n|---|---|---|\n"
                    "| 1 | `GET /api/alerts/\\{alert\\_id\\}/price-history` | `core` → `notify` |\n"
                    "| 2 | texto<br>con salto | x |\n\n"
                    "```python\nprint('hola')\n```\n\n"
                    "$$\nx^2 + y^2\n$$\n"
                )
            res = subprocess.run(
                [sys.executable, SCRIPT, "src", "--output", "out"],
                cwd=d, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout[-2000:] + res.stderr)
            self.assertTrue(os.path.exists(os.path.join(d, "out.pdf")))
            self.assertFalse(os.path.exists(os.path.join(d, "documento.pdf")))


class InjectionTests(unittest.TestCase):
    """El .md es entrada no confiable: nada de lo que escriba puede leer/escribir archivos."""

    def assertNoInput(self, tex):
        self.assertNotRegex(tex, r"\\(input|include|write|openin|csname)\b")

    def test_matematica_en_linea_con_input_se_neutraliza(self):
        out = body(r"Mira $\input{/etc/passwd}$ aquí")
        self.assertNoInput(out.replace(r"\textbackslash{}", ""))
        self.assertIn(r"\texttt{", out)

    def test_matematica_en_bloque_con_input_se_neutraliza(self):
        out = body("$$\n\\input{/etc/passwd}\n$$")
        self.assertNotIn(r"\[", out)
        self.assertNoInput(out.replace(r"\textbackslash{}", ""))

    def test_matematica_en_una_linea_con_write_se_neutraliza(self):
        out = body(r"$$\immediate\write18{id}$$")
        self.assertNotIn(r"\[", out)

    def test_truco_hexadecimal_y_arroba_se_rechazan(self):
        for evil in (r"$^^5cinput{x}$", r"$\@input{x}$", r"$\csname input\endcsname$"):
            self.assertIn(r"\texttt{", body(evil), evil)

    def test_matematica_legitima_se_conserva(self):
        md = r"$\frac{a}{b} + \sum_{i=1}^{n} x_i \leq \alpha$"
        self.assertEqual(body(md), md)
        blk = body("$$\n\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}\n$$")
        self.assertIn(r"\begin{aligned}", blk)

    def test_enlace_no_puede_cerrar_el_argumento(self):
        out = body(r"[x](https://a.com/}\input{/etc/hosts}\href{b)")
        self.assertNoInput(out)
        self.assertNotIn("}\\", out.split("href")[1].split("{x}")[0])

    def test_esquema_javascript_o_file_se_omite(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd"):
            out = body(f"[click]({url})")
            self.assertNotIn(r"\href", out)
            self.assertIn("click", out)

    def test_enlace_normal_se_conserva(self):
        self.assertIn(r"\href{https://ejemplo.com/a?b=1}{sitio}", body("[sitio](https://ejemplo.com/a?b=1)"))

    def test_imagen_con_ruta_peligrosa_se_omite(self):
        for path in ("/etc/passwd", "../secreto.png", "a}\\input{b", "x%y.png"):
            out = body(f"![alt]({path})")
            self.assertNotIn("includegraphics", out, path)
        self.assertIn("includegraphics", body("![alt](diagramas/uno.png)"))

    def test_codigo_no_puede_cerrar_lstlisting(self):
        out = body("```text\n\\end{lstlisting}\\input{/etc/shells}\n```")
        self.assertEqual(out.count(r"\end{lstlisting}"), 1)

    def test_encabezado_repositorio_no_inyecta(self):
        meta = {"titulo": "T", "autor": "A", "repositorio": "https://x.com/}\\input{/etc/hosts} (rama a}\\input{b)"}
        tex = m.build_latex_document(meta, "")
        self.assertNoInput(tex)

    def test_carpeta_de_fuentes_con_llaves_no_entra_a_graphicspath(self):
        tex = m.build_latex_document({"titulo": "T", "autor": "A"}, "", source_dir="a}\\input{b")
        self.assertNoInput(tex)

    def test_html_de_mermaid_no_ejecuta_codigo_del_diagrama(self):
        evil = "graph TD\n A[x]\n</div><script>alert(1)</script>"
        html_doc = m.build_mermaid_html(evil, "/tmp/x/templates/mermaid.min.js")
        self.assertNotIn("</div><script>alert", html_doc)
        self.assertIn('securityLevel: "strict"', html_doc)
        self.assertIn("Content-Security-Policy", html_doc)

    def test_chrome_no_usa_no_sandbox_por_defecto(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('MD_LATEX_NO_SANDBOX', src)
        self.assertNotIn('            "--no-sandbox",', src)


class InputValidationTests(unittest.TestCase):
    """Antes de convertir, el script valida que haya body (y header o metadatos alternativos)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text="contenido"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args, "--no-compile"],
                              capture_output=True, text=True, cwd=self.tmp)

    def test_carpeta_con_body_y_header_resuelve(self):
        body_p, header_p = self.write("body.md"), self.write("header.md", "titulo: T")
        src, body_path, header_path = m.resolve_inputs(self.tmp)
        self.assertEqual((src, body_path, header_path), (self.tmp, body_p, header_p))

    def test_carpeta_sin_body_falla(self):
        self.write("header.md", "titulo: T")
        with self.assertRaises(m.InputError) as ctx:
            m.resolve_inputs(self.tmp)
        self.assertIn("body", str(ctx.exception).lower())

    def test_body_vacio_falla(self):
        self.write("body.md", "  \n")
        with self.assertRaises(m.InputError):
            m.resolve_inputs(self.tmp)

    def test_header_explicito_inexistente_falla(self):
        self.write("body.md")
        with self.assertRaises(m.InputError):
            m.resolve_inputs(self.tmp, header_arg=os.path.join(self.tmp, "nada.md"))

    def test_body_explicito_inexistente_falla(self):
        with self.assertRaises(m.InputError):
            m.resolve_inputs("", body_arg=os.path.join(self.tmp, "nada.md"))

    def test_carpeta_inexistente_falla(self):
        with self.assertRaises(m.InputError):
            m.resolve_inputs("", source_dir_arg=os.path.join(self.tmp, "no-existe"))

    def test_sin_header_es_valido_si_hay_body(self):
        self.write("body.md")
        _, _, header_path = m.resolve_inputs(self.tmp)
        self.assertIsNone(header_path)

    def test_body_alterno_no_confunde_header_local_con_contenido(self):
        self.write("header.local.md", "titulo: T")
        self.write("capitulo.md")
        _, body_path, _ = m.resolve_inputs(self.tmp)
        self.assertTrue(body_path.endswith("capitulo.md"))

    def test_cli_sale_con_codigo_1_y_lista_los_problemas(self):
        res = self.run_cli(self.tmp, "--header", os.path.join(self.tmp, "nada.md"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("No se puede iniciar", res.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "documento.tex")))

    def test_cli_valida_antes_de_generar_nada(self):
        res = self.run_cli(self.tmp)
        self.assertEqual(res.returncode, 1)
        self.assertEqual(os.listdir(self.tmp), [])


class PathAndSkillTests(unittest.TestCase):
    def test_carpeta_de_salida_inexistente_se_crea_y_ruta_absoluta_funciona(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "fuentes")
            os.makedirs(src)
            for name, text in (("body.md", "# Hola\n\nTexto."), ("header.md", "titulo: T\nautor: A\nasignatura: S")):
                with open(os.path.join(src, name), "w", encoding="utf-8") as f:
                    f.write(text)
            out = os.path.join(d, "salida", "nueva", "doc")
            res = subprocess.run([sys.executable, SCRIPT, src, "-o", out, "--no-compile"],
                                 capture_output=True, text=True, cwd=d)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            with open(out + ".tex", encoding="utf-8") as f:
                self.assertIn(os.path.abspath(src) + "/img/", f.read())

    def test_graphicspath_usa_ruta_absoluta(self):
        tex = m.build_latex_document({"titulo": "T", "autor": "A"}, "", source_dir="relativa")
        self.assertIn(os.path.abspath("relativa"), tex)

    def test_skill_tiene_frontmatter_y_apunta_a_un_script_existente(self):
        skill = os.path.join(HERE, "skills", "md-to-latex", "SKILL.md")
        with open(skill, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: md-to-latex\n"))
        self.assertIn("description:", text.split("---")[1])
        self.assertTrue(os.path.exists(os.path.normpath(os.path.join(os.path.dirname(skill), "..", "..", "md_to_latex.py"))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
