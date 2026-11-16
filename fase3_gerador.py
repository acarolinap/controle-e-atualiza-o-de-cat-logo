"""Fase 3 - Geração do Catálogo PDF com Jinja2 e Playwright (em lotes de páginas).

Uso: python fase3_gerador.py
     python fase3_gerador.py --limite 18               (teste rápido)
     python fase3_gerador.py --logo "caminho\\da\\logo.png"

Regras:
  * Só entram produtos com status_publicacao = 'ativo', na ordem do catálogo
    (pagina_origem, sku).
  * 9 produtos por página (grade 3 x 3), com cabeçalho e rodapé repetidos.
  * SEÇÕES: a coluna opcional `secao` do banco ('promocao' ou 'novidades')
    indica o tipo da página no catálogo em mandarim. O título vermelho
    (PROMOÇÃO / NOVIDADES) só aparece em páginas dessa seção, e uma página
    nunca mistura seções: quando a seção muda, começa uma página nova.
    Itens sem seção ficam em páginas sem título.
  * Preço verde (normal) ou vermelho (item com preco_promocional preenchido, que
    é o "特价" do card no catálogo em mandarim); nesse caso o preço normal aparece
    riscado ao lado. Isso vale em qualquer página, com ou sem título PROMOÇÃO.
  * A imagem de cada SKU é procurada em IMAGENS_DIRS, na ordem: o catálogo
    traduzido (saida/) tem prioridade sobre o geral (saida_geral/).
  * Valores ausentes nunca são inventados: sem nome em português, sai só o
    nome em chinês; sem status, a faixa fica só com a logo.
  * Os lotes têm um número de páginas inteiro (PAGINAS_POR_LOTE), para que
    nenhuma página fique dividida entre dois PDFs parciais.
  * Logo: --logo > variável de ambiente LOGO_PATH > primeiro arquivo "logo*"
    encontrado em LOGO_DIRS. A margem transparente da logo é recortada
    automaticamente (precisa do Pillow); se não achar a logo, usa o texto.
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
WHATSAPP = "(11) 95132-9999"
INSTAGRAM = "@LUKTONIMPORTS"

TITULOS = {                              # seção do banco -> texto vermelho do cabeçalho
    "promocao": "PROMOÇÃO",
    "novidades": "NOVIDADES",
}

TEMPLATES_DIR = "templates"
TEMPLATE = "catalogo.html"
LOGO_DIRS = [                            # onde procurar a logo (relativo à pasta do projeto)
    os.path.join(TEMPLATES_DIR, "assets"),
    "templates_assets",
    "assets",
    os.path.join("assets", "logo"),
    ".",
]
LOGO_EXTS = (".png", ".svg", ".webp", ".jpg", ".jpeg")

IMAGENS_DIRS = [                         # ordem = prioridade
    os.path.join("saida", "assets", "img"),
    os.path.join("saida_geral", "assets", "img"),
]
SAIDA_DIR = "saida_catalogo"             # HTML e PDFs gerados (coberto pelo .gitignore: saida*/)

CARDS_POR_PAGINA = 9                     # 3 colunas x 3 linhas
PAGINAS_POR_LOTE = 12                    # 12 x 9 = 108 produtos por PDF parcial

COLUNAS_OPCIONAIS = ("preco_promocional", "secao")

SQL = """
    SELECT l.pagina AS pagina_layout, l.ordem_pagina,
           p.sku, p.preco_brl, {preco_promocional} AS preco_promocional, {secao} AS secao,
           p.pcs_cx, p.nome_pt, p.nome_cn, p.status_montagem, p.pagina_origem
    FROM catalogo_layout l
    JOIN catalogo_produtos p ON p.sku = l.sku
    WHERE p.status_publicacao = 'ativo'
    ORDER BY l.pagina ASC, l.ordem_pagina ASC;
"""


# -------------------------------------------------------------------- dados
def buscar_produtos_banco():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # colunas opcionais: se não existirem, entram como NULL
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name = 'catalogo_produtos'""")
            existentes = {r["column_name"] for r in cur.fetchall()}
            colunas = {c: (c if c in existentes else "NULL") for c in COLUNAS_OPCIONAIS}
            cur.execute(SQL.format(**colunas))
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


def localizar_logo(caminho=None):
    """Retorna (uri, caminho) da logo, ou (None, None) se não achar.

    Prioridade: argumento/--logo > variável LOGO_PATH > 1º arquivo 'logo*'
    em LOGO_DIRS. Um caminho informado que não existe é erro (não cai no texto).
    """
    caminho = caminho or os.getenv("LOGO_PATH")
    if caminho:
        p = Path(caminho)
        if not p.is_file():
            raise FileNotFoundError(f"Logo não encontrada: {p.resolve()}")
        return p.resolve().as_uri(), p.resolve()
    for pasta in LOGO_DIRS:
        d = Path(pasta)
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file() and f.stem.lower().startswith("logo") and f.suffix.lower() in LOGO_EXTS:
                    return f.resolve().as_uri(), f.resolve()
    return None, None


def recortar_margem_logo(caminho):
    """Recorta a margem transparente da logo, para que o tamanho no PDF seja o da arte.

    Retorna o caminho a usar (o recorte, salvo em SAIDA_DIR, ou o original se
    não for possível recortar: SVG, sem transparência ou Pillow ausente).
    """
    if caminho.suffix.lower() == ".svg":
        return caminho
    try:
        from PIL import Image
        with Image.open(caminho) as im:
            im = im.convert("RGBA")
            caixa = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
            if not caixa or caixa == (0, 0, *im.size):
                return caminho
            os.makedirs(SAIDA_DIR, exist_ok=True)
            destino = Path(SAIDA_DIR) / "logo_recortada.png"
            im.crop(caixa).save(destino)
            return destino.resolve()
    except Exception as erro:          # nunca derruba a geração por causa da logo
        print(f"  aviso: não foi possível recortar a margem da logo ({erro}); usando o original.")
        return caminho


def preparar_produtos(linhas):
    """Converte as linhas do banco no formato usado pelo template."""
    produtos = []
    for r in linhas:
        status_pt, status_cn = status_para_rotulos(r.get("status_montagem"))
        secao = (r.get("secao") or "").strip().lower() or None
        promo = r.get("preco_promocional")
        com_preco_promo = promo is not None and float(promo) > 0
        produtos.append({
            "pagina_layout": r["pagina_layout"],
            "sku": r["sku"],
            "secao": secao,
            "nome_pt": limpar_texto(r.get("nome_pt")),
            "nome_cn": limpar_texto(r.get("nome_cn")),
            "pcs_cx": r.get("pcs_cx"),
            "promocional": com_preco_promo,
            "preco_fmt": formatar_preco(promo if com_preco_promo else r.get("preco_brl")),
            "preco_antigo_fmt": formatar_preco(r.get("preco_brl")) if com_preco_promo else None,
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
        "em página PROMOÇÃO sem preço promocional (preço normal, em verde)": [
            p["sku"] for p in produtos if p["secao"] == "promocao" and not p["preco_antigo_fmt"]],
        "com seção desconhecida (sem título)": [
            p["sku"] for p in produtos if p["secao"] and p["secao"] not in TITULOS],
    }
    for nome, skus in lacunas.items():
        if skus:
            amostra = ", ".join(skus[:6]) + (" ..." if len(skus) > 6 else "")
            print(f"  aviso: {len(skus):4d} {nome} ({amostra})")


def em_paginas(produtos, por_pagina=CARDS_POR_PAGINA):
    """Agrupa exatamente pelo número da página original do catálogo chinês."""
    grupos = {}
    for p in produtos:
        pag = p["pagina_layout"]
        grupos.setdefault(pag, []).append(p)

    paginas = []
    for pag in sorted(grupos):
        itens = grupos[pag]
        secao = next((x["secao"] for x in itens if x["secao"]), None)
        paginas.append({
            "produtos": itens,
            "secao": secao,
            "titulo": TITULOS.get(secao),
        })
    return paginas


def resumo_secoes(paginas):
    contagem = {}
    for pg in paginas:
        chave = TITULOS.get(pg["secao"]) or "(sem título)"
        contagem[chave] = contagem.get(chave, 0) + 1
    print("Páginas por título: " + ", ".join(f"{k}: {v}" for k, v in contagem.items()))


# ---------------------------------------------------------------- renderização
def verificar_template():
    """Interrompe com uma mensagem clara se o catalogo.html for de uma versão anterior."""
    caminho = Path(TEMPLATES_DIR) / TEMPLATE
    if not caminho.is_file():
        raise SystemExit(f"Template não encontrado: {caminho.resolve()}")
    if "pagina.produtos" not in caminho.read_text(encoding="utf-8"):
        raise SystemExit(
            f"O template {caminho.resolve()} é de uma versão anterior (cada página é um dicionário "
            "com 'produtos' e 'titulo', e o template antigo não sabe lê-lo). "
            "Baixe de novo o catalogo.html e salve por cima desse arquivo.")


def criar_ambiente():
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )


def renderizar_html(env, paginas, logo_uri):
    return env.get_template(TEMPLATE).render(
        paginas=paginas, whatsapp=WHATSAPP, instagram=INSTAGRAM, logo_uri=logo_uri,
    )


def html_para_pdf(browser, html_path, pdf_path):
    page = browser.new_page()
    try:
        page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=120000)
        page.evaluate("document.fonts.ready.then(() => true)")
        page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True)
    finally:
        page.close()


def gerar_pdf_catalogo(limite=None, logo=None):
    verificar_template()
    print("Buscando produtos ativos no banco de dados...")
    produtos = preparar_produtos(buscar_produtos_banco())
    if limite:
        produtos = produtos[:limite]
    paginas = em_paginas(produtos)
    print(f"{len(produtos)} produtos -> {len(paginas)} páginas")
    resumo_secoes(paginas)
    relatar_lacunas(produtos)
    if not produtos:
        print("Nada a gerar: nenhum produto com status_publicacao = 'ativo'.")
        return

    logo_uri, logo_path = localizar_logo(logo)
    if logo_uri:
        print(f"Logo: {logo_path}")
        logo_uri = recortar_margem_logo(logo_path).as_uri()
    else:
        print("AVISO: logo não encontrada, usando o texto LUKTON. Procurei em: "
              + ", ".join(str(Path(d).resolve()) for d in LOGO_DIRS))

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
            html_path.write_text(renderizar_html(env, lote, logo_uri), encoding="utf-8")
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
    ap.add_argument("--limite", type=int, help="gera só os N primeiros produtos (teste)")
    ap.add_argument("--logo", help="caminho da logo (png/svg/webp/jpg)")
    a = ap.parse_args()
    gerar_pdf_catalogo(a.limite, a.logo)