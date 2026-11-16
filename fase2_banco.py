"""Fase 2 - Sincroniza os JSONs da Fase 1 com o PostgreSQL (preservando layout exato)."""
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

DE_PARA_SKU = {
    "BZ6092-800": "BZ6082-800",
}

CAMPOS = ("preco_brl", "pcs_cx", "nome_pt", "nome_cn", "status_montagem")
CAMPOS_AUDITADOS = {"preco_brl": "preco_alterado", "pcs_cx": "pcs_alterado"}
CAMPOS_CONFLITO = ("preco_brl", "pcs_cx", "status_montagem")

DDL = """
CREATE TABLE IF NOT EXISTS catalogo_produtos (
    sku VARCHAR(50) PRIMARY KEY,
    preco_brl NUMERIC(10, 2),
    pcs_cx INTEGER,
    nome_pt TEXT,
    nome_cn TEXT,
    status_montagem VARCHAR(20),
    status_publicacao VARCHAR(20) DEFAULT 'ativo',
    data_atualizacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE catalogo_produtos ADD COLUMN IF NOT EXISTS pagina_origem INTEGER;

CREATE TABLE IF NOT EXISTS catalogo_layout (
    pagina INTEGER NOT NULL,
    ordem_pagina INTEGER NOT NULL,
    sku VARCHAR(50) NOT NULL,
    PRIMARY KEY (pagina, ordem_pagina)
);
"""

UPSERT = """
INSERT INTO catalogo_produtos
    (sku, preco_brl, pcs_cx, nome_pt, nome_cn, status_montagem, pagina_origem)
VALUES %s
ON CONFLICT (sku) DO UPDATE SET
    preco_brl       = COALESCE(EXCLUDED.preco_brl, catalogo_produtos.preco_brl),
    pcs_cx          = COALESCE(EXCLUDED.pcs_cx, catalogo_produtos.pcs_cx),
    nome_pt         = COALESCE(EXCLUDED.nome_pt, catalogo_produtos.nome_pt),
    nome_cn         = COALESCE(EXCLUDED.nome_cn, catalogo_produtos.nome_cn),
    status_montagem = COALESCE(EXCLUDED.status_montagem, catalogo_produtos.status_montagem),
    pagina_origem   = COALESCE(catalogo_produtos.pagina_origem, EXCLUDED.pagina_origem),
    data_atualizacao = CURRENT_TIMESTAMP;
"""


def _texto(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _num(v, tipo):
    if v is None or v == "":
        return None
    try:
        return tipo(round(float(v), 2)) if tipo is float else int(v)
    except (TypeError, ValueError):
        return None


def normalizar(p, arquivo):
    sku_raw = _texto(p.get("sku"))
    sku = sku_raw.upper() if sku_raw else None
    if sku in DE_PARA_SKU:
        sku = DE_PARA_SKU[sku]

    preco = _num(p.get("preco_brl") if p.get("preco_brl") is not None else p.get("preco_cn"), float)
    flags = []
    if preco == 0:
        flags.append("preco_zerado")
    status = _texto(p.get("status_montagem"))
    eh_geral = "preco_cn" in p
    return {
        "sku": sku,
        "preco_brl": preco,
        "pcs_cx": _num(p.get("pcs_cx"), int),
        "nome_pt": _texto(p.get("nome_pt")),
        "nome_cn": _texto(p.get("nome_cn")),
        "status_montagem": status.lower() if status else None,
        "pagina_origem": _num(p.get("pagina"), int) if eh_geral else None,
        "ordem_pagina": _num(p.get("ordem_pagina"), int) if eh_geral else None,
        "eh_geral": eh_geral,
        "origem": f"{Path(arquivo).parent.name}/{Path(arquivo).name}:pág {p.get('pagina', '?')}",
        "flags": flags,
    }


def carregar_registros(caminhos):
    registros = []
    for c in caminhos:
        with open(c, "r", encoding="utf-8") as f:
            registros += [normalizar(p, c) for p in json.load(f)]
    return registros


def mesclar(registros, prioridade="ultimo"):
    por_sku, pulados, conflitos, layout = {}, [], [], []
    for r in registros:
        if not r["sku"]:
            pulados.append(r)
            continue

        # Registra a posição exata no layout se vier do catálogo geral
        if r["eh_geral"] and r["pagina_origem"] and r["ordem_pagina"]:
            layout.append((r["pagina_origem"], r["ordem_pagina"], r["sku"]))

        atual = por_sku.get(r["sku"])
        if atual is None:
            novo = dict(r)
            novo["_orig"] = {c: r["origem"] for c in (*CAMPOS, "pagina_origem") if r[c] is not None}
            por_sku[r["sku"]] = novo
            continue

        atual["flags"] = sorted(set(atual["flags"]) | set(r["flags"]))
        for campo in CAMPOS_CONFLITO:
            a, b = atual[campo], r[campo]
            if a is not None and b is not None and a != b:
                conflitos.append({
                    "tipo": "conflito", "sku": r["sku"], "campo": campo,
                    "valor_anterior": a, "valor_novo": b,
                    "origem": f"{atual['_orig'].get(campo)} -> {r['origem']}",
                    "detalhe": f"prevalece: {b if prioridade == 'ultimo' else a}",
                })
        for campo in (*CAMPOS, "pagina_origem"):
            novo = r[campo]
            if novo is None:
                continue
            # Não deixa preço nulo ou 0 sobrescrever preço válido existente
            if campo == "preco_brl" and novo == 0 and atual[campo] not in (None, 0):
                continue
            if campo == "pagina_origem":
                sobrescreve = atual[campo] is None
            else:
                sobrescreve = prioridade == "ultimo" or atual[campo] is None
            if sobrescreve:
                atual[campo] = novo
                atual["_orig"][campo] = r["origem"]
    return por_sku, pulados, conflitos, layout


def comparar(por_sku, existentes):
    novos, alterados, para_gravar = [], [], []
    for sku, r in por_sku.items():
        ex = existentes.get(sku)
        if ex is None:
            novos.append(r)
            para_gravar.append(r)
            continue
        mudancas = []
        for campo in (*CAMPOS, "pagina_origem"):
            novo = r[campo]
            if novo is None:
                continue
            antigo = ex.get(campo)
            if campo == "preco_brl" and antigo is not None:
                antigo = round(float(antigo), 2)
            if campo == "pagina_origem" and antigo is not None:
                continue
            if antigo != novo:
                mudancas.append((campo, antigo, novo))
        if mudancas:
            alterados.append((r, mudancas))
            para_gravar.append(r)
    return novos, alterados, para_gravar


def linhas_auditoria(novos, alterados, pulados, conflitos, por_sku):
    linhas = []
    for r in novos:
        linhas.append({"tipo": "novo", "sku": r["sku"], "campo": "", "valor_anterior": "",
                       "valor_novo": r["preco_brl"] if r["preco_brl"] is not None else "",
                       "origem": r["origem"], "detalhe": r["nome_pt"] or r["nome_cn"] or ""})
    for r, mudancas in alterados:
        for campo, antigo, novo in mudancas:
            linhas.append({"tipo": CAMPOS_AUDITADOS.get(campo, "campo_alterado"), "sku": r["sku"],
                           "campo": campo, "valor_anterior": "" if antigo is None else antigo,
                           "valor_novo": novo, "origem": r["_orig"].get(campo, r["origem"]),
                           "detalhe": ""})
    linhas += conflitos
    for r in pulados:
        linhas.append({"tipo": "sku_ausente", "sku": "", "campo": "", "valor_anterior": "",
                       "valor_novo": r["preco_brl"] if r["preco_brl"] is not None else "",
                       "origem": r["origem"],
                       "detalhe": " | ".join(filter(None, [r["nome_pt"], r["nome_cn"]])) or "(sem nome)"})
    return linhas


def salvar_auditoria(linhas, caminho):
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    cols = ["tipo", "sku", "campo", "valor_anterior", "valor_novo", "origem", "detalhe"]
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(linhas)


def carregar_existentes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('catalogo_produtos')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("""SELECT sku, preco_brl, pcs_cx, nome_pt, nome_cn, status_montagem, pagina_origem
                       FROM catalogo_produtos""")
        cols = [d[0] for d in cur.description]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def gravar(conn, para_gravar, layout):
    from psycopg2.extras import execute_values
    with conn.cursor() as cur:
        if para_gravar:
            valores = [(r["sku"], r["preco_brl"], r["pcs_cx"], r["nome_pt"], r["nome_cn"],
                        r["status_montagem"], r["pagina_origem"]) for r in para_gravar]
            execute_values(cur, UPSERT, valores)
        if layout:
            cur.execute("TRUNCATE TABLE catalogo_layout")
            execute_values(cur, "INSERT INTO catalogo_layout (pagina, ordem_pagina, sku) VALUES %s", layout)


def main():
    ap = argparse.ArgumentParser(description="Sincroniza os JSONs da Fase 1 com o PostgreSQL.")
    ap.add_argument("json", nargs="*", default=["saida_geral/produtos.json", "saida/produtos.json"])
    ap.add_argument("--prioridade", choices=["ultimo", "primeiro"], default="ultimo")
    ap.add_argument("--auditoria", default="saida/auditoria_fase2.csv")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    registros = carregar_registros(a.json)
    por_sku, pulados, conflitos, layout = mesclar(registros, a.prioridade)
    print(f"Lidos: {len(registros)} | SKUs únicos: {len(por_sku)} | Slots de layout: {len(layout)}")

    import psycopg2
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if not a.dry_run:
            with conn.cursor() as cur:
                cur.execute(DDL)
        existentes = carregar_existentes(conn)
        novos, alterados, para_gravar = comparar(por_sku, existentes)
        if not a.dry_run:
            gravar(conn, para_gravar, layout)
            conn.commit()
        else:
            conn.rollback()
    except Exception as e:
        conn.rollback()
        print(f"Erro (nada foi gravado): {e}")
        sys.exit(1)
    finally:
        conn.close()

    linhas = linhas_auditoria(novos, alterados, pulados, conflitos, por_sku)
    salvar_auditoria(linhas, a.auditoria)
    print(f"Novos: {len(novos)} | Alterados: {len(alterados)} | Layout gravado: {len(layout)} cards.")


if __name__ == "__main__":
    main()