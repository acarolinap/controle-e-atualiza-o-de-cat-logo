"""Fase 1b - Detecta, no catálogo em mandarim, as páginas de NOVIDADES / PROMOÇÃO
e os cards em promoção, e grava no banco (colunas `secao` e `preco_promocional`).

Uso:
  python fase1b_secoes.py "catalogo geral LUKTON 12.11.pdf" --sem-banco   (só analisa o PDF)
  python fase1b_secoes.py "catalogo geral LUKTON 12.11.pdf" --dry-run     (lê o banco, não grava)
  python fase1b_secoes.py "catalogo geral LUKTON 12.11.pdf"               (grava)

Como o catálogo marca cada coisa (verificado no PDF de 12/11):
  * Título da página: texto na faixa verde abaixo de "product description".
    "NOVIDADES" (págs. 1-3) e "Promocao实际价格" (pág. 4). Os outros títulos
    (美发系列, 美妆系列, 车配, 宠物系列) são categorias e são ignorados.
  * Card em promoção: o texto "特价：17.00" (ou "promocao:7.50") acima da barra verde,
    com o preço normal (Unid.CX) riscado em vermelho. Isso vale também para cards
    em promoção dentro de páginas comuns (págs. 49, 56 e 74 no catálogo de 12/11).

Regras de gravação (recalculadas do zero a cada execução):
  * secao = título da página de origem do SKU (coluna pagina_origem do banco).
  * preco_promocional = valor do card em promoção. Se o SKU tem promoções com valores
    diferentes, vale a da página com título de promoção (senão, a primeira). O conflito
    vai para o relatório.
  * O preço normal continua em preco_brl (Fase 2).
Relatório: saida/auditoria_secoes.csv
"""
import argparse
import csv
import os
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "lukton_catalog"),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "admin"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}

# faixa do título, como fração da altura da página (o catálogo tem 969 pt de altura)
FAIXA_TITULO = (0.098, 0.165)
TITULOS = (("novidades", "novidades"), ("promocao", "promocao"))   # (secao, trecho sem acento)

PROMO_RE = re.compile(r"(?:特价|promo[cç][aã]o)\s*[：:]\s*(\d+(?:[.,]\d+)?)", re.I)
SKU_FIM_RE = re.compile(r"/\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\s*$")
UNID_RE = re.compile(r"Unid\.?\s*CX\s*[:：]?\s*(\d+(?:[.,]\d+)?)", re.I)


# ----------------------------------------------------------------- leitura do PDF
def sem_acento(texto):
    base = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in base if not unicodedata.combining(c)).lower()


def numero(txt):
    return float(txt.replace(",", "."))


def linhas(page):
    for bloco in page.get_text("dict")["blocks"]:
        for linha in bloco.get("lines", []):
            texto = "".join(s["text"] for s in linha["spans"]).strip()
            if texto:
                yield texto, pymupdf.Rect(linha["bbox"])


def titulo_pagina(page):
    r = page.rect
    faixa = pymupdf.Rect(0, r.height * FAIXA_TITULO[0], r.width, r.height * FAIXA_TITULO[1])
    texto = sem_acento(page.get_text("text", clip=faixa))
    for secao, trecho in TITULOS:
        if trecho in texto:
            return secao
    return None


def riscos_vermelhos(page):
    """Retângulos finos e vermelhos: o traço que risca o preço normal."""
    achados = []
    for d in page.get_drawings():
        r, cor = d["rect"], d.get("color") or d.get("fill")
        if r.height < 4 and r.width > 20 and cor and len(cor) >= 3 \
                and cor[0] > 0.7 and cor[1] < 0.35 and cor[2] < 0.35:
            achados.append(r)
    return achados


def _mais_proximo_acima(alvo, itens, max_dy):
    cx = (alvo.x0 + alvo.x1) / 2
    melhor = None
    for valor, b in itens:
        dy = alvo.y0 - b.y1
        if -4 <= dy <= max_dy and abs((b.x0 + b.x1) / 2 - cx) < 125:
            if melhor is None or dy < melhor[0]:
                melhor = (dy, valor, b)
    return melhor


def cards_promocionais(page, numero_pagina):
    legendas, unids = [], []
    promos = []
    for texto, bbox in linhas(page):
        m = SKU_FIM_RE.search(texto)
        if m:
            legendas.append((m.group(1).upper(), bbox))
        m = UNID_RE.search(texto)
        if m:
            unids.append((numero(m.group(1)), bbox))
        m = PROMO_RE.search(texto)
        if m:
            promos.append((numero(m.group(1)), bbox))
    riscos = riscos_vermelhos(page)

    cards = []
    for preco, bbox in promos:
        legenda = _mais_proximo_acima(bbox, legendas, 110)
        # preço normal: a linha Unid.CX logo ABAIXO do preço promocional
        cx = (bbox.x0 + bbox.x1) / 2
        abaixo = [(u.y0 - bbox.y1, v, u) for v, u in unids
                  if -4 <= u.y0 - bbox.y1 <= 45 and abs((u.x0 + u.x1) / 2 - cx) < 125]
        normal, riscado = None, False
        if abaixo:
            _, normal, ub = min(abaixo, key=lambda t: t[0])
            riscado = any(r.intersects(ub) for r in riscos)
        cards.append({
            "pagina": numero_pagina,
            "sku": legenda[1] if legenda else None,
            "preco_promocional": preco,
            "preco_normal_no_card": normal,
            "riscado": riscado,
        })
    return cards


def analisar(pdf):
    doc = pymupdf.open(pdf)
    titulos, cards = {}, []
    for n, page in enumerate(doc, 1):
        titulos[n] = titulo_pagina(page)
        cards += cards_promocionais(page, n)
    return titulos, cards


def consolidar(cards, titulos):
    """Uma promoção por SKU. Retorna (escolhidos, conflitos, sem_sku)."""
    por_sku, sem_sku = {}, []
    for c in cards:
        (por_sku.setdefault(c["sku"], []) if c["sku"] else sem_sku).append(c)
    escolhidos, conflitos = {}, []
    for sku, lista in por_sku.items():
        preferidas = [c for c in lista if titulos[c["pagina"]] == "promocao"] or lista
        escolhidos[sku] = preferidas[0]
        if len({c["preco_promocional"] for c in lista}) > 1:
            conflitos.append((sku, lista, preferidas[0]))
    return escolhidos, conflitos, sem_sku


# ------------------------------------------------------------------------ banco
def paginas_por_secao(titulos):
    saida = {}
    for n, secao in titulos.items():
        if secao:
            saida.setdefault(secao, []).append(n)
    return saida


def aplicar_no_banco(conn, por_secao, escolhidos, gravar):
    """Retorna (contagem_por_secao, skus_fora_do_banco)."""
    contagem, fora = {}, []
    with conn.cursor() as cur:
        if gravar:
            cur.execute("ALTER TABLE catalogo_produtos ADD COLUMN IF NOT EXISTS secao VARCHAR(20)")
            cur.execute("ALTER TABLE catalogo_produtos ADD COLUMN IF NOT EXISTS preco_promocional NUMERIC(10,2)")
            cur.execute("UPDATE catalogo_produtos SET secao = NULL, preco_promocional = NULL")
        for secao, paginas in por_secao.items():
            if gravar:
                cur.execute("UPDATE catalogo_produtos SET secao = %s WHERE pagina_origem = ANY(%s)",
                            (secao, paginas))
                contagem[secao] = cur.rowcount
            else:
                cur.execute("SELECT count(*) FROM catalogo_produtos WHERE pagina_origem = ANY(%s)", (paginas,))
                contagem[secao] = cur.fetchone()[0]
        if gravar:
            for sku, c in escolhidos.items():
                cur.execute("UPDATE catalogo_produtos SET preco_promocional = %s WHERE sku = %s",
                            (c["preco_promocional"], sku))
                if cur.rowcount == 0:
                    fora.append(sku)
        else:
            cur.execute("SELECT sku FROM catalogo_produtos WHERE sku = ANY(%s)", (list(escolhidos),))
            existentes = {r[0] for r in cur.fetchall()}
            fora = [s for s in escolhidos if s not in existentes]
    return contagem, fora


# --------------------------------------------------------------------- relatório
def salvar_csv(caminho, titulos, cards, escolhidos, conflitos):
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    escolhidos_ids = {id(c) for c in escolhidos.values()}
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tipo", "pagina", "sku", "preco_promocional", "preco_normal_no_card", "riscado", "detalhe"])
        for n, t in titulos.items():
            if t:
                w.writerow(["titulo_pagina", n, "", "", "", "", t])
        for c in cards:
            usado = "usado" if id(c) in escolhidos_ids else "descartado (outro valor/ocorrência)"
            w.writerow(["card_promocional", c["pagina"], c["sku"] or "?", c["preco_promocional"],
                        c["preco_normal_no_card"], "sim" if c["riscado"] else "NÃO",
                        usado if c["sku"] else "SKU não identificado"])
        for sku, lista, escolhido in conflitos:
            vals = " x ".join(f"{c['preco_promocional']} (pág {c['pagina']})" for c in lista)
            w.writerow(["conflito", escolhido["pagina"], sku, escolhido["preco_promocional"], "", "",
                        f"valores diferentes: {vals}; prevalece {escolhido['preco_promocional']}"])


def main():
    ap = argparse.ArgumentParser(description="Detecta seções e promoções no catálogo em mandarim.")
    ap.add_argument("pdf")
    ap.add_argument("--dry-run", action="store_true", help="lê o banco, mas não grava")
    ap.add_argument("--sem-banco", action="store_true", help="só analisa o PDF (não acessa o banco)")
    ap.add_argument("--auditoria", default="saida/auditoria_secoes.csv")
    a = ap.parse_args()

    titulos, cards = analisar(a.pdf)
    escolhidos, conflitos, sem_sku = consolidar(cards, titulos)
    por_secao = paginas_por_secao(titulos)
    salvar_csv(a.auditoria, titulos, cards, escolhidos, conflitos)

    print(f"PDF: {len(titulos)} páginas")
    for secao, paginas in por_secao.items():
        print(f"  título {secao.upper():10s}: páginas {paginas}")
    print(f"Cards em promoção: {len(cards)} ocorrências, {len(escolhidos)} SKUs "
          f"({len(sem_sku)} sem SKU identificado)")
    for sku, c in sorted(escolhidos.items()):
        marca = "" if c["riscado"] else "  [preço normal NÃO riscado]"
        print(f"  {sku:10s} pág {c['pagina']:3d}  promoção {c['preco_promocional']:8.2f}  "
              f"normal {c['preco_normal_no_card']}{marca}")
    for sku, lista, escolhido in conflitos:
        vals = ", ".join(f"{c['preco_promocional']} (pág {c['pagina']})" for c in lista)
        print(f"  CONFLITO {sku}: {vals} -> vale {escolhido['preco_promocional']}")

    if a.sem_banco:
        print(f"\nRelatório: {a.auditoria}  (nada foi gravado: --sem-banco)")
        return

    import psycopg2
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        contagem, fora = aplicar_no_banco(conn, por_secao, escolhidos, gravar=not a.dry_run)
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception as erro:
        conn.rollback()
        print(f"Erro (nada foi gravado): {erro}")
        sys.exit(1)
    finally:
        conn.close()

    rotulo = "SKUs que seriam marcados" if a.dry_run else "SKUs marcados"
    print(f"\n=== {'DRY-RUN: nada gravado' if a.dry_run else 'GRAVADO'} ===")
    for secao, n in contagem.items():
        print(f"{rotulo} como {secao}: {n}")
    print(f"Promoções {'a gravar' if a.dry_run else 'gravadas'}: {len(escolhidos) - len(fora)}")
    if fora:
        print(f"SKUs promocionais que NÃO estão no banco: {', '.join(fora)}")
    print(f"Relatório: {a.auditoria}")


if __name__ == "__main__":
    main()