"""Fase 2c - Tabela de cards: uma linha por card do catálogo em mandarim, REPETIDOS INCLUÍDOS.

Motivo: a tabela catalogo_produtos tem uma linha por SKU, então os SKUs que se repetem no
catálogo (780 cards, 737 SKUs) ficavam uma vez só e o PDF brasileiro encolhia (~83 páginas).
Aqui cada card ocupa a mesma página e a mesma posição do original; a Fase 3 gera uma página
brasileira para cada página do catálogo em mandarim (104 páginas no PDF de 12/11).

Uso:
  python fase2c_paginas.py saida_geral/produtos.json --traduzido saida/produtos.json ^
      --pdf "catalogo geral LUKTON 12.11.pdf" --dry-run
  python fase2c_paginas.py saida_geral/produtos.json --traduzido saida/produtos.json ^
      --pdf "catalogo geral LUKTON 12.11.pdf"

Regras:
  * Ordem e página de cada card = ordem do produtos.json do catálogo geral (a da Fase 1).
  * Cada card guarda os SEUS valores (preço, PCS/CX, status): o mesmo SKU pode ter valores
    diferentes em páginas diferentes, e o original é respeitado.
  * O catálogo em mandarim é a verdade: cada card mostra exatamente o que o original diz (preço,
    PCS/CX, status, nome em chinês), mesmo que haja conflito ou erro no original.
    O catálogo traduzido (--traduzido) só empresta o NOME EM PORTUGUÊS dos SKUs que ele traz, e
    só quando o card não tem nome em português próprio. (--traduzido-completo restaura o
    comportamento antigo, em que ele sobrescrevia preço, PCS/CX, status, nomes e imagem.)
  * Imagem: cada card usa o PNG da sua própria ocorrência (SKU.png, SKU__pN.png, SKU__pN_2.png),
    com a mesma regra de nomes da Fase 1. Para SKUs do traduzido vale a imagem de saida/assets/img.
  * Com --pdf: grava o título de cada página (NOVIDADES / PROMOÇÃO) e o preço promocional dos
    cards em promoção (特价 / promocao), card a card.
  * A tabela é refeita do zero a cada execução (TRUNCATE + INSERT, numa transação só).
Relatório: saida/auditoria_cards.csv
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "lukton_catalog"),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "admin"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}

DDL = """
CREATE TABLE IF NOT EXISTS catalogo_cards (
    pagina INTEGER NOT NULL,
    posicao INTEGER NOT NULL,
    sku VARCHAR(50),
    preco_brl NUMERIC(10, 2),
    preco_promocional NUMERIC(10, 2),
    pcs_cx INTEGER,
    nome_pt TEXT,
    nome_cn TEXT,
    status_montagem VARCHAR(20),
    imagem TEXT,
    PRIMARY KEY (pagina, posicao)
);
CREATE TABLE IF NOT EXISTS catalogo_paginas (
    pagina INTEGER PRIMARY KEY,
    secao VARCHAR(20)
);
"""


# ------------------------------------------------------------------ utilitários
def ler_json(caminho):
    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def texto(v):
    v = (str(v).strip() if v is not None else "")
    return v if v not in ("", "/") else None


def numero(v, tipo):
    if v is None or v == "":
        return None
    try:
        return int(v) if tipo is int else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def nome_arquivo(sku, pagina, usados):
    """Mesma regra de nomes da Fase 1: SKU.png; repetido -> SKU__pN.png; colisão -> _2, _3."""
    base = sku if sku not in usados else f"{sku}__p{pagina}"
    nome, n = base, 2
    while nome in usados:
        nome = f"{base}_{n}"
        n += 1
    usados.add(nome)
    return nome + ".png"


# ------------------------------------------------------------------ montagem
def montar_cards(geral, traduzido, pasta_geral, pasta_trad, completo=False):
    """Retorna (cards, problemas). Cada card é um dict pronto para a tabela."""
    trad = {}
    for p in traduzido:
        sku = texto(p.get("sku"))
        if sku:
            trad[sku.upper()] = p

    cards, problemas = [], []
    nomes_emprestados = 0
    usados, posicao = set(), {}
    for p in geral:
        pagina = int(p["pagina"])
        posicao[pagina] = posicao.get(pagina, 0) + 1
        sku_original = texto(p.get("sku"))
        sku = sku_original.upper() if sku_original else None

        card = {
            "pagina": pagina,
            "posicao": posicao[pagina],
            "sku": sku,
            "preco_brl": numero(p["preco_brl"] if p.get("preco_brl") is not None else p.get("preco_cn"), float),
            "preco_promocional": None,
            "pcs_cx": numero(p.get("pcs_cx"), int),
            "nome_pt": texto(p.get("nome_pt")),
            "nome_cn": texto(p.get("nome_cn")),
            "status_montagem": (texto(p.get("status_montagem")) or "").lower() or None,
            "imagem": None,
        }

        # imagem da própria ocorrência (só existe quando o SKU foi lido)
        if sku_original:
            arquivo = Path(pasta_geral) / nome_arquivo(sku_original, pagina, usados)
            if arquivo.exists():
                card["imagem"] = arquivo.as_posix()
            else:
                problemas.append(("sem_imagem", card, f"não existe {arquivo.as_posix()}"))
        else:
            problemas.append(("sem_sku", card, "SKU não lido no PDF: preencher à mão"))

        t = trad.get(sku) if sku else None
        if t and not completo:
            # espelho do original: do traduzido só vem o nome em português, e só se o card não tem
            nome = texto(t.get("nome_pt"))
            if nome and not card["nome_pt"]:
                card["nome_pt"] = nome
                nomes_emprestados += 1
        elif t:
            # modo antigo: o traduzido (mais novo) prevalece para os SKUs que ele traz
            for campo_card, campo_t, tipo in (("preco_brl", "preco_brl", float), ("pcs_cx", "pcs_cx", int)):
                v = numero(t.get(campo_t), tipo)
                if v is not None:
                    card[campo_card] = v
            for campo in ("nome_pt", "nome_cn"):
                v = texto(t.get(campo))
                if v:
                    card[campo] = v
            status = (texto(t.get("status_montagem")) or "").lower()
            if status:
                card["status_montagem"] = status
            img_t = Path(pasta_trad) / f"{sku}.png"
            if img_t.exists():
                card["imagem"] = img_t.as_posix()
        cards.append(card)
    return cards, problemas, nomes_emprestados


def aplicar_promocoes(cards, promos):
    """Liga cada card em promoção do PDF ao card (mesma página e SKU). Retorna os não ligados."""
    sem_par = []
    for pr in promos:
        alvo = next((c for c in cards if c["pagina"] == pr["pagina"] and c["sku"] == pr["sku"]
                     and c["preco_promocional"] is None), None)
        if alvo:
            alvo["preco_promocional"] = pr["preco_promocional"]
        else:
            sem_par.append(pr)
    return sem_par


# ------------------------------------------------------------------ conferência com o PDF
def contar_cards_pdf(pdf):
    """Cards por página do PDF original (cada card tem uma linha 'PCS/CX')."""
    import re
    import pymupdf
    doc = pymupdf.open(pdf)
    return {n: len(re.findall(r"PCS/CX", pg.get_text("text"), re.I)) for n, pg in enumerate(doc, 1)}


def conferir_com_pdf(cards, esperado):
    """Compara cards por página (JSON x PDF). Retorna a lista de divergências."""
    por_pagina = {}
    for c in cards:
        por_pagina.setdefault(c["pagina"], []).append(c)
    divergencias = []
    for n in sorted(set(esperado) | set(por_pagina)):
        qtd = len(por_pagina.get(n, []))
        if qtd != esperado.get(n, 0):
            skus = ", ".join(c["sku"] or "?" for c in por_pagina.get(n, []))
            divergencias.append((n, qtd, esperado.get(n, 0), skus))
    return divergencias


# ------------------------------------------------------------------ banco / relatório
def gravar(cards, titulos):
    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("TRUNCATE catalogo_cards, catalogo_paginas")
            execute_values(cur, """
                INSERT INTO catalogo_cards (pagina, posicao, sku, preco_brl, preco_promocional,
                                            pcs_cx, nome_pt, nome_cn, status_montagem, imagem)
                VALUES %s""", [(c["pagina"], c["posicao"], c["sku"], c["preco_brl"], c["preco_promocional"],
                                c["pcs_cx"], c["nome_pt"], c["nome_cn"], c["status_montagem"], c["imagem"])
                               for c in cards])
            paginas = sorted({c["pagina"] for c in cards})
            execute_values(cur, "INSERT INTO catalogo_paginas (pagina, secao) VALUES %s",
                           [(n, titulos.get(n)) for n in paginas])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def salvar_csv(caminho, problemas, sem_par, divergencias=()):
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["tipo", "pagina", "posicao", "sku", "detalhe"])
        for tipo, c, detalhe in problemas:
            w.writerow([tipo, c["pagina"], c["posicao"], c["sku"] or "", detalhe])
        for pr in sem_par:
            w.writerow(["promocao_sem_card", pr["pagina"], "", pr["sku"],
                        f"promoção {pr['preco_promocional']} sem card correspondente no JSON"])
        for n, qtd, esp, skus in divergencias:
            w.writerow(["pagina_divergente", n, "", "", f"JSON {qtd} cards, PDF {esp} -> {skus}"])


def main():
    ap = argparse.ArgumentParser(description="Monta a tabela de cards (com repetidos).")
    ap.add_argument("geral", help="produtos.json do catálogo geral (Fase 1)")
    ap.add_argument("--traduzido", help="produtos.json do catálogo traduzido (prevalece)")
    ap.add_argument("--pdf", help="PDF do catálogo em mandarim (títulos das páginas e promoções)")
    ap.add_argument("--imagens", default=os.path.join("saida_geral", "assets", "img"))
    ap.add_argument("--imagens-traduzido", default=os.path.join("saida", "assets", "img"))
    ap.add_argument("--auditoria", default=os.path.join("saida", "auditoria_cards.csv"))
    ap.add_argument("--dry-run", action="store_true", help="calcula e gera o relatório, sem gravar")
    ap.add_argument("--traduzido-completo", action="store_true",
                    help="o traduzido sobrescreve preço, PCS/CX, status, nomes e imagem (não recomendado: "
                         "foge do catálogo em mandarim)")
    ap.add_argument("--forcar", action="store_true",
                    help="grava mesmo que o número de cards por página não bata com o PDF")
    a = ap.parse_args()

    geral = ler_json(a.geral)
    traduzido = ler_json(a.traduzido) if a.traduzido else []
    cards, problemas, emprestados = montar_cards(geral, traduzido, a.imagens, a.imagens_traduzido,
                                                 completo=a.traduzido_completo)

    titulos, sem_par, promos = {}, [], []
    if a.pdf:
        from fase1b_secoes import analisar
        titulos, promos = analisar(a.pdf)
        sem_par = aplicar_promocoes(cards, promos)
    else:
        print("AVISO: sem --pdf, as páginas ficam sem título e nenhum card sai como promoção.")

    divergencias, esperado = [], {}
    if a.pdf:
        esperado = contar_cards_pdf(a.pdf)
        divergencias = conferir_com_pdf(cards, esperado)
    salvar_csv(a.auditoria, problemas, sem_par, divergencias)

    por_pagina = {}
    for c in cards:
        por_pagina[c["pagina"]] = por_pagina.get(c["pagina"], 0) + 1
    skus = [c["sku"] for c in cards if c["sku"]]
    repetidos = len(skus) - len(set(skus))
    n_promo = sum(1 for c in cards if c["preco_promocional"] is not None)
    print(f"Cards: {len(cards)} em {len(por_pagina)} páginas (a Fase 3 gera 1 página por página de origem)")
    print(f"SKUs únicos: {len(set(skus))} | ocorrências repetidas mantidas: {repetidos}")
    if traduzido:
        print("Traduzido: " + ("sobrescreveu os valores do original (--traduzido-completo)" if a.traduzido_completo
              else f"só o nome em português foi aproveitado, em {emprestados} cards (valores = catálogo em mandarim)"))
    print(f"Páginas com título: " + (", ".join(f"{n}:{t}" for n, t in titulos.items() if t) or "nenhuma"))
    print(f"Cards em promoção ligados: {n_promo} de {len(promos)} detectados no PDF")
    for tipo in ("sem_sku", "sem_imagem"):
        qtd = sum(1 for t, _, _ in problemas if t == tipo)
        if qtd:
            lista = ", ".join(f"pág {c['pagina']}#{c['posicao']}" for t, c, _ in problemas if t == tipo)
            print(f"  aviso: {qtd} cards {tipo.replace('_', ' ')} ({lista[:150]})")
    if sem_par:
        print(f"  aviso: {len(sem_par)} promoções do PDF sem card correspondente no JSON")
    print(f"Relatório: {a.auditoria}")

    if divergencias:
        print(f"\n!!! ATENÇÃO: {len(divergencias)} páginas com número de cards diferente do PDF "
              f"(JSON: {len(cards)} cards; PDF: {sum(esperado.values())} cards).")
        faltam = [(n, esp - qtd) for n, qtd, esp, _ in divergencias if qtd < esp]
        sobram = [(n, qtd - esp) for n, qtd, esp, _ in divergencias if qtd > esp]
        if faltam:
            print("  faltam cards (pág:qtde): " + ", ".join(f"{n}:{d}" for n, d in faltam))
        if sobram:
            print("  sobram cards (pág:qtde): " + ", ".join(f"{n}:{d}" for n, d in sobram))
        for n, qtd, esp, skus in divergencias[:6]:
            print(f"  pág {n}: JSON {qtd} cards, PDF {esp} -> {skus[:140]}")
        if len(divergencias) > 6:
            print(f"  ... e mais {len(divergencias) - 6} páginas (veja o relatório)")
        if sobram:
            print("  Sobram cards: o produtos.json parece duplicado ou de outra execução da Fase 1.")
        if faltam:
            print("  Faltam cards: a Fase 1 não leu esses cards (ou o extrator em uso é outra versão).")
        print("  Detalhes de todas as páginas: " + a.auditoria + " (linhas 'pagina_divergente').")

    if a.dry_run:
        print("\nDRY-RUN: nada foi gravado.")
        return
    if divergencias and not a.forcar:
        print("Nada foi gravado: corrija o produtos.json (ou use --forcar, não recomendado).")
        sys.exit(1)
    try:
        gravar(cards, titulos)
    except Exception as erro:
        print(f"Erro (nada foi gravado): {erro}")
        sys.exit(1)
    print(f"\nGRAVADO: {len(cards)} cards em catalogo_cards e {len(por_pagina)} páginas em catalogo_paginas.")


if __name__ == "__main__":
    main()