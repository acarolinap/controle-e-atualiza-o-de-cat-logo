"""Fase 3 - Geração do Catálogo PDF com Jinja2 e Playwright (em lotes de páginas).

Uso: python fase3_gerador.py
     python fase3_gerador.py --titulo "NOVIDADES" --limite 18   (teste rápido)

Regras:
  * Só entram produtos com status_publicacao = 'ativo', na ordem do catálogo
    (pagina_origem, sku).
  * 9 produtos por página (grade 3 x 3), com cabeçalho e rodapé repetidos.
  * A imagem de cada SKU é procurada em IMAGENS_DIRS, na ordem: o catálogo
    traduzido (saida/) tem prioridade sobre o geral (saida_geral/).
  * Preço verde (normal) ou vermelho (promocional, coluna preco_promocional).
  * Valores ausentes nunca são inventados: sem nome em português, sai só o
    nome em chinês; sem status, a faixa fica só com a logo.
  * Os lotes têm um número de páginas inteiro (PAGINAS_POR_LOTE), para que
    nenhuma página fique dividida entre dois PDFs parciais.
"""
import argparse
import os
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "lukton_catalog"),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "admin"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}

# --------------------------------------------------------------- configuração
TITULO = "PROMOÇÃO"                      # texto vermelho abaixo da logo
WHATSAPP = "(11) 95132-9999"
INSTAGRAM = "@LUKTONIMPORTS"

TEMPLATES_DIR = "templates"
TEMPLATE = "catalogo.html"
LOGO_ARQUIVOS = [                        # logo oficial (opcional); vale o primeiro que existir
    os.path.join(TEMPLATES_DIR, "assets", nome) for nome in ("logo.png", "logo.svg", "logo.webp")
]

IMAGENS_DIRS = [                         # ordem = prioridade
    os.path.join("saida", "assets", "img"),
    os.path.join("saida_geral", "assets", "img"),
]
SAIDA_DIR = "saida_catalogo"             # HTML e PDFs gerados (coberto pelo .gitignore: saida*/)

CARDS_POR_PAGINA = 9                     # 3 colunas x 3 linhas
PAGINAS_POR_LOTE = 12                    # 12 x 9 = 108 produtos por PDF parcial

SQL = """
    SELECT sku, preco_brl, {promo} AS preco_promocional, pcs_cx, nome_pt, nome_cn,
           status_montagem, pagina_origem
    FROM catalogo_produtos
    WHERE status_publicacao = 'ativo'
    ORDER BY pagina_origem NULLS LAST, sku;
"""


# -------------------------------------------------------------------- dados
def buscar_produtos_banco():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # a coluna preco_promocional é opcional: sem ela, nenhum item é promocional
            cur.execute("""SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'catalogo_produtos'
                             AND column_name = 'preco_promocional'""")
            promo = "preco_promocional" if cur.fetchone() else "NULL::numeric"
            cur.execute(SQL.format(promo=promo))
            return cur.fetchall()
    finally:
        conn.close()


def formatar_preco(valor):
    """1234.5 -> '1.234,50'. Preço 0 fica '00,00', como vem no PDF de origem."""
    if valor is None:
        return None
    valor = float(valor)
    if valor == 0:
        return "00,00"
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def limpar_texto(valor):
    valor = (valor or "").strip()
    return valor if valor not in ("", "/") else None


def status_para_rotulos(status):
    status = (status or "").strip().lower()
    if status == "montado":
        return "MONTADO", "包好"
    if status == "desmontado":
        return "DESMONTADO", "散装"
    return None, None


def localizar_imagem(sku):
    for pasta in IMAGENS_DIRS:
        caminho = Path(pasta) / f"{sku}.png"
        if caminho.exists():
            return caminho.resolve().as_uri()
    return None


def localizar_logo():
    for caminho in LOGO_ARQUIVOS:
        if Path(caminho).exists():
            return Path(caminho).resolve().as_uri()
    return None


def preparar_produtos(linhas):
    """Converte as linhas do banco no formato usado pelo template.

    Promoção: se preco_promocional estiver preenchido (> 0), o preço normal
    (preco_brl) aparece riscado e o promocional, em vermelho. Caso contrário,
    o preço é o normal, na cor verde.
    """
    produtos = []
    for r in linhas:
        status_pt, status_cn = status_para_rotulos(r.get("status_montagem"))
        promo = r.get("preco_promocional")
        em_promocao = promo is not None and float(promo) > 0
        produtos.append({
            "sku": r["sku"],
            "nome_pt": limpar_texto(r.get("nome_pt")),
            "nome_cn": limpar_texto(r.get("nome_cn")),
            "pcs_cx": r.get("pcs_cx"),
            "promocional": em_promocao,
            "preco_fmt": formatar_preco(promo if em_promocao else r.get("preco_brl")),
            "preco_antigo_fmt": formatar_preco(r.get("preco_brl")) if em_promocao else None,
            "status_pt": status_pt,
            "status_cn": status_cn,
            "imagem": localizar_imagem(r["sku"]),
        })
    return produtos


def relatar_lacunas(produtos):
    lacunas = {
        "sem imagem": [p["sku"] for p in produtos if not p["imagem"]],
        "sem nome em português": [p["sku"] for p in produtos if not p["nome_pt"]],
        "sem preço": [p["sku"] for p in produtos if p["preco_fmt"] is None],
        "sem status de montagem": [p["sku"] for p in produtos if not p["status_pt"]],
    }
    for nome, skus in lacunas.items():
        if skus:
            amostra = ", ".join(skus[:6]) + (" ..." if len(skus) > 6 else "")
            print(f"  aviso: {len(skus):4d} {nome} ({amostra})")


def em_paginas(produtos, por_pagina=CARDS_POR_PAGINA):
    return [produtos[i:i + por_pagina] for i in range(0, len(produtos), por_pagina)]


# ---------------------------------------------------------------- renderização
def criar_ambiente():
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )


def renderizar_html(env, paginas, titulo):
    logo_uri = localizar_logo()
    return env.get_template(TEMPLATE).render(
        paginas=paginas, titulo=titulo, whatsapp=WHATSAPP,
        instagram=INSTAGRAM, logo_uri=logo_uri,
    )


def html_para_pdf(browser, html_path, pdf_path):
    page = browser.new_page()
    try:
        page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=120000)
        page.evaluate("document.fonts.ready.then(() => true)")
        page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True)
    finally:
        page.close()


def gerar_pdf_catalogo(titulo=TITULO, limite=None):
    print("Buscando produtos ativos no banco de dados...")
    produtos = preparar_produtos(buscar_produtos_banco())
    if limite:
        produtos = produtos[:limite]
    paginas = em_paginas(produtos)
    print(f"{len(produtos)} produtos -> {len(paginas)} páginas")
    relatar_lacunas(produtos)
    if not produtos:
        print("Nada a gerar: nenhum produto com status_publicacao = 'ativo'.")
        return

    os.makedirs(SAIDA_DIR, exist_ok=True)
    env = criar_ambiente()
    lotes = [paginas[i:i + PAGINAS_POR_LOTE] for i in range(0, len(paginas), PAGINAS_POR_LOTE)]
    pdfs_parciais = []

    print("Iniciando Playwright para renderização em lotes...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--js-flags=--max-old-space-size=4096",
            ],
        )
        for idx, lote in enumerate(lotes, 1):
            print(f"Processando lote {idx} de {len(lotes)} ({len(lote)} páginas)...")
            html_path = Path(SAIDA_DIR) / f"lote_{idx}.html"
            pdf_path = Path(SAIDA_DIR) / f"lote_{idx}.pdf"
            html_path.write_text(renderizar_html(env, lote, titulo), encoding="utf-8")
            html_para_pdf(browser, html_path, pdf_path)
            pdfs_parciais.append(pdf_path.resolve())
        browser.close()

    print("Unificando os PDFs parciais em um único catálogo final...")
    from pypdf import PdfWriter

    merger = PdfWriter()
    for pdf in pdfs_parciais:
        merger.append(str(pdf))
    pdf_final = Path(SAIDA_DIR, "catalogo_final_lukton.pdf").resolve()
    with open(pdf_final, "wb") as saida:
        merger.write(saida)
    merger.close()

    print(f"\n[SUCESSO] Catálogo PDF gerado com sucesso em:\n-> {pdf_final}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gera o catálogo PDF a partir do banco.")
    ap.add_argument("--titulo", default=TITULO, help="texto vermelho do cabeçalho (padrão: PROMOÇÃO)")
    ap.add_argument("--limite", type=int, help="gera só os N primeiros produtos (teste)")
    a = ap.parse_args()
    gerar_pdf_catalogo(a.titulo, a.limite)