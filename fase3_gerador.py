"""Fase 3 - Geração do Catálogo PDF com Jinja2 e Playwright (em lotes de páginas).

Uso: python fase3_gerador.py
     python fase3_gerador.py --paginas 1-12            (teste rápido: só essas páginas de origem)
     python fase3_gerador.py --logo "caminho\\da\\logo.png"

Regras:
  * O PDF ESPELHA o catálogo em mandarim: cada página de origem vira uma página brasileira,
    com os mesmos cards na mesma ordem. SKUs repetidos no original ficam repetidos.
    (Dados: tabela catalogo_cards, gerada pela Fase 2c.)
  * Só entram cards cujo SKU não esteja marcado como inativo em catalogo_produtos
    (status_publicacao <> 'ativo' tira o SKU de todas as páginas).
  * Título vermelho (PROMOÇÃO / NOVIDADES) só nas páginas que têm esse título no original
    (tabela catalogo_paginas).
  * Preço verde (normal) ou vermelho (card com preco_promocional, o "特价" do original);
    nesse caso o preço normal aparece riscado ao lado, em qualquer página.
  * Nome em português: o de catalogo_produtos (traduções) tem prioridade sobre o do card.
  * Valores ausentes nunca são inventados: sem nome em português, sai só o nome em chinês;
    sem status, a faixa fica só com a logo.
  * Os lotes têm um número de páginas inteiro (PAGINAS_POR_LOTE).
  * Logo: --logo > variável de ambiente LOGO_PATH > primeiro arquivo "logo*" encontrado em
    LOGO_DIRS. A margem transparente da logo é recortada automaticamente (precisa do Pillow);
    se não achar a logo, usa o texto.
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

SAIDA_DIR = "saida_catalogo"             # HTML e PDFs gerados (coberto pelo .gitignore: saida*/)

CARDS_POR_PAGINA = 9                     # 3 colunas x 3 linhas
PAGINAS_POR_LOTE = 12                    # 12 páginas por PDF parcial

SQL = """
    SELECT c.pagina, c.posicao, c.sku, c.preco_brl, c.preco_promocional, c.pcs_cx,
           COALESCE(NULLIF(p.nome_pt, ''), c.nome_pt) AS nome_pt,
           c.nome_cn, c.status_montagem, c.imagem, g.secao
    FROM catalogo_cards c
    LEFT JOIN catalogo_produtos p ON p.sku = c.sku
    LEFT JOIN catalogo_paginas g ON g.pagina = c.pagina
    WHERE COALESCE(p.status_publicacao, 'ativo') = 'ativo'
      AND c.pagina BETWEEN %(inicio)s AND %(fim)s
    ORDER BY c.pagina, c.posicao;
"""


# -------------------------------------------------------------------- dados
def buscar_cards_banco(inicio=1, fim=10**6):
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('catalogo_cards') AS t")
            if cur.fetchone()["t"] is None:
                raise SystemExit("A tabela catalogo_cards não existe. Rode antes o fase2c_paginas.py.")
            cur.execute(SQL, {"inicio": inicio, "fim": fim})
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


def imagem_para_uri(caminho):
    if not caminho:
        return None
    p = Path(caminho)
    return p.resolve().as_uri() if p.exists() else None


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
        promo = r.get("preco_promocional")
        com_preco_promo = promo is not None and float(promo) > 0
        produtos.append({
            "pagina": r["pagina"],
            "sku": r.get("sku"),
            "secao": (r.get("secao") or "").strip().lower() or None,
            "nome_pt": limpar_texto(r.get("nome_pt")),
            "nome_cn": limpar_texto(r.get("nome_cn")),
            "pcs_cx": r.get("pcs_cx"),
            "promocional": com_preco_promo,
            "preco_fmt": formatar_preco(promo if com_preco_promo else r.get("preco_brl")),
            "preco_antigo_fmt": formatar_preco(r.get("preco_brl")) if com_preco_promo else None,
            "status_pt": status_pt,
            "status_cn": status_cn,
            "imagem": imagem_para_uri(r.get("imagem")),
        })
    return produtos


def relatar_lacunas(produtos):
    def ref(p):
        return f"pág {p['pagina']}: {p['sku'] or 'sem SKU'}"
    lacunas = {
        "cards sem SKU (preencher à mão)": [ref(p) for p in produtos if not p["sku"]],
        "cards sem imagem": [ref(p) for p in produtos if not p["imagem"]],
        "cards sem nome em português": [ref(p) for p in produtos if not p["nome_pt"]],
        "cards sem preço": [ref(p) for p in produtos if p["preco_fmt"] is None],
        "cards sem status de montagem": [ref(p) for p in produtos if not p["status_pt"]],
        "cards em página PROMOÇÃO sem preço promocional (preço normal, em verde)": [
            ref(p) for p in produtos if p["secao"] == "promocao" and not p["preco_antigo_fmt"]],
        "cards com seção desconhecida (sem título)": [
            ref(p) for p in produtos if p["secao"] and p["secao"] not in TITULOS],
    }
    for nome, itens in lacunas.items():
        if itens:
            amostra = "; ".join(itens[:4]) + (" ..." if len(itens) > 4 else "")
            print(f"  aviso: {len(itens):4d} {nome} ({amostra})")


def em_paginas(produtos, por_pagina=CARDS_POR_PAGINA):
    """Uma página brasileira por página de origem (espelho do original), na mesma ordem."""
    paginas = []
    atual = None
    for p in produtos:
        if atual is None or p["pagina"] != atual["pagina"]:
            atual = {"pagina": p["pagina"], "produtos": [], "secao": p["secao"],
                     "titulo": TITULOS.get(p["secao"])}
            paginas.append(atual)
        atual["produtos"].append(p)
    saida = []                         # segurança: página com mais de 9 cards vira duas
    for pg in paginas:
        for i in range(0, len(pg["produtos"]), por_pagina):
            saida.append({**pg, "produtos": pg["produtos"][i:i + por_pagina]})
    return saida


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


def intervalo_paginas(texto):
    if not texto:
        return 1, 10**6
    if "-" in texto:
        a, b = texto.split("-", 1)
        return int(a), int(b)
    return int(texto), int(texto)


def gerar_pdf_catalogo(paginas_origem=None, logo=None):
    verificar_template()
    inicio, fim = intervalo_paginas(paginas_origem)
    print("Buscando os cards no banco de dados...")
    produtos = preparar_produtos(buscar_cards_banco(inicio, fim))
    paginas = em_paginas(produtos)
    print(f"{len(produtos)} cards -> {len(paginas)} páginas")
    resumo_secoes(paginas)
    relatar_lacunas(produtos)
    if not produtos:
        print("Nada a gerar: nenhum card encontrado para esse intervalo.")
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
    ap.add_argument("--paginas", help="só estas páginas de origem, ex.: 1-12 (teste rápido)")
    ap.add_argument("--logo", help="caminho da logo (png/svg/webp/jpg)")
    a = ap.parse_args()
    gerar_pdf_catalogo(a.paginas, a.logo)