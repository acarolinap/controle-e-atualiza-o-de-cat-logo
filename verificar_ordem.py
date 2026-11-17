"""Verifica se os cards estão na mesma ordem do catálogo original.

Etapa 1: PDF original (mandarim) x produtos.json da Fase 1 (card a card, pela posição)
         -> acusa erro de LEITURA/ORDENAÇÃO na Fase 1.
Etapa 2: produtos.json x PDF final gerado (Fase 3)
         -> acusa erro de GERAÇÃO (template / Fase 3).

Uso:
  python verificar_ordem.py "catalogo geral LUKTON 12.11.pdf"
  python verificar_ordem.py "catalogo geral LUKTON 12.11.pdf" --json saida_geral/produtos.json ^
      --final saida_catalogo/catalogo_final_lukton.pdf

Relatório: saida/auditoria_ordem.csv
"""
import argparse
import csv
import json
import re
from pathlib import Path

import pymupdf

# legenda do card no original: "nome / SKU" no fim da linha (mesma regra da fase1b)
SKU_FIM_RE = re.compile(r"/\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\s*$")
CARDS_POR_PAGINA = 9          # igual ao fase3_gerador.py


def ordem_de_leitura_itens(itens, tol_linha=25.0):
    """Igual a ordem_de_leitura, mas devolve o primeiro campo de cada item (payload)."""
    return ordem_de_leitura(itens, tol_linha)


def ordem_de_leitura(itens, tol_linha=30.0):
    """itens: (sku, x, y). Linha por linha (cima->baixo), esquerda->direita em cada linha."""
    linhas = []
    for it in sorted(itens, key=lambda t: t[2]):
        if linhas and abs(it[2] - linhas[-1][0][2]) < tol_linha:
            linhas[-1].append(it)
        else:
            linhas.append([it])
    return [it[0] for ln in linhas for it in sorted(ln, key=lambda t: t[1])]


SKU_SOLTO_RE = re.compile(r"[A-Za-z]{1,5}\d{0,5}(?:-[A-Za-z0-9]{1,6})+")


def skus_na_palavra(txt, conhecidos):
    """SKUs conhecidos dentro de uma palavra (ex.: '包好/ZZJ-01' -> ZZJ-01)."""
    return [m.group(0).upper() for m in SKU_SOLTO_RE.finditer(txt) if m.group(0).upper() in conhecidos]


def cards_original(page, conhecidos):
    """Para cada card do original (âncora 'PCS/CX', em ordem de leitura), devolve o conjunto de
    SKUs que aparecem na zona do card. Não depende de como a Fase 1 interpreta o texto."""
    palavras = page.get_text("words")
    larg = page.rect.width
    ancoras = []
    for x0, y0, x1, y1, txt, *_ in palavras:
        if txt.startswith("PCS/CX"):
            ancoras.append(((x0, y0, x1, y1), (x0 + x1) / 2, y0))
    zonas = []
    for bb in ordem_de_leitura_itens(ancoras):
        cx = (bb[0] + bb[2]) / 2
        col = min(2, int(cx / (larg / 3)))
        zonas.append((col * larg / 3, bb[1] - 90, (col + 1) * larg / 3, bb[3] + 3))
    saida = []
    for zx0, zy0, zx1, zy1 in zonas:
        achados = []
        for x0, y0, x1, y1, txt, *_ in palavras:
            if zx0 <= (x0 + x1) / 2 <= zx1 and zy0 <= (y0 + y1) / 2 <= zy1:
                achados += skus_na_palavra(txt, conhecidos)
        saida.append(set(achados))
    return saida


def skus_final(page, conhecidos):
    achados = []
    for x0, y0, x1, y1, txt, *_ in page.get_text("words"):
        for t in skus_na_palavra(txt, conhecidos):
            cx = (x0 + x1) / 2
            # card cujo nome impresso é o próprio código do modelo: o SKU aparece 2x no mesmo
            # card (linha do SKU e linha do nome). Conta uma vez só.
            if any(t == a[0] and abs(cx - a[1]) < 80 and abs(y0 - a[2]) < 40 for a in achados):
                continue
            achados.append((t, cx, y0))
    return ordem_de_leitura(achados)


def esperado_por_pagina(produtos):
    """{pagina_origem: [skus na ordem do JSON]} (cards sem SKU ficam de fora)."""
    saida = {}
    for p in produtos:
        saida.setdefault(int(p["pagina"]), [])
        if p.get("sku"):
            saida[int(p["pagina"])].append(p["sku"].upper())
    return saida


def diferencas(esperado, achado):
    """Lista de (posicao, esperado, achado) onde as sequências diferem."""
    out = []
    for i in range(max(len(esperado), len(achado))):
        e = esperado[i] if i < len(esperado) else "(falta)"
        a = achado[i] if i < len(achado) else "(falta)"
        if e != a:
            out.append((i + 1, e, a))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_original")
    ap.add_argument("--json", default="saida_geral/produtos.json")
    ap.add_argument("--final", default="saida_catalogo/catalogo_final_lukton.pdf")
    ap.add_argument("--csv", default="saida/auditoria_ordem.csv")
    a = ap.parse_args()

    produtos = json.loads(Path(a.json).read_text(encoding="utf-8"))
    esperado = esperado_por_pagina(produtos)
    conhecidos = {s for lst in esperado.values() for s in lst}
    linhas_csv = []

    # ---- etapa 1: original x JSON (card a card, pela zona do PDF)
    doc = pymupdf.open(a.pdf_original)
    ruins1 = []
    for n, page in enumerate(doc, 1):
        zonas = cards_original(page, conhecidos)
        cards_json = [p for p in produtos if int(p["pagina"]) == n]
        problemas = []
        if len(zonas) != len(cards_json):
            problemas.append((0, f"{len(cards_json)} cards no JSON", f"{len(zonas)} cards no PDF"))
        for k, c in enumerate(cards_json):
            sku = (c.get("sku") or "").upper()
            if k >= len(zonas):
                break
            if sku not in zonas[k]:
                problemas.append((k + 1, sku or "(sem SKU)", ", ".join(sorted(zonas[k])) or "(nenhum)"))
        if problemas:
            ruins1.append(n)
            for pos, js, orig in problemas:
                linhas_csv.append(["1-json_x_original", n, pos, js, orig])
    print(f"Etapa 1 (JSON x original): {len(ruins1)} páginas com diferença"
          + (f" -> {ruins1}" if ruins1 else " - tudo na mesma ordem"))

    # ---- etapa 2: JSON x PDF final (1 página final por 9 cards da página de origem)
    if Path(a.final).is_file():
        final = pymupdf.open(a.final)
        esperadas_finais = []
        for n in sorted(esperado):
            cards = [p for p in produtos if int(p["pagina"]) == n]
            for i in range(0, len(cards), CARDS_POR_PAGINA):
                bloco = [c["sku"].upper() for c in cards[i:i + CARDS_POR_PAGINA] if c.get("sku")]
                esperadas_finais.append((n, bloco))
        if final.page_count != len(esperadas_finais):
            print(f"AVISO: o PDF final tem {final.page_count} páginas, o esperado era "
                  f"{len(esperadas_finais)} (cards removidos pelo filtro de inativos?).")
        ruins2 = []
        for i, (n, bloco) in enumerate(esperadas_finais):
            if i >= final.page_count:
                break
            lido = skus_final(final[i], conhecidos)
            dif = diferencas(bloco, lido)
            if dif:
                ruins2.append((i + 1, n))
                for pos, esp, ach in dif:
                    linhas_csv.append(["2-json_x_final", n, pos, esp, ach])
        print(f"Etapa 2 (JSON x PDF final): {len(ruins2)} páginas com diferença"
              + (f" -> (pág. final, pág. origem): {ruins2[:15]}" if ruins2 else " - tudo na mesma ordem"))
    else:
        print(f"Etapa 2 pulada: {a.final} não existe (rode a Fase 3 antes).")

    Path(a.csv).parent.mkdir(parents=True, exist_ok=True)
    with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["etapa", "pagina_origem", "posicao", "esperado_ou_original", "achado_ou_json"])
        w.writerows(linhas_csv)
    print(f"Relatório: {a.csv}")


if __name__ == "__main__":
    main()