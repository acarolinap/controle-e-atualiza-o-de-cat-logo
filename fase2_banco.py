"""Fase 2 - Sincroniza os JSONs da Fase 1 com o PostgreSQL.

Uso (a ORDEM dos arquivos importa: o último prevalece sobre os anteriores):
  python fase2_banco.py saida_geral/produtos.json saida/produtos.json
  python fase2_banco.py saida_geral/produtos.json saida/produtos.json --dry-run

Regras:
  * Aceita os dois perfis da Fase 1. Preço: usa `preco_brl` e, se não houver,
    `preco_cn` (o Unid.CX do catálogo geral, assumido como R$).
  * Valores vazios NUNCA apagam o que já está no banco (COALESCE): o geral não
    traz nome_pt/status e não zera o que veio do traduzido.
  * Preço 0,00 é tratado como ausente.
  * SKU nulo é pulado e vai para o relatório de auditoria.
  * SKU repetido (no mesmo arquivo ou entre arquivos): por padrão a última
    ocorrência vence (`--prioridade primeiro` inverte). Todo conflito de
    preço/volume é listado no relatório.
  * Só grava o que é novo ou mudou (data_atualizacao só muda quando há mudança).
  * Tudo numa transação: se algo falhar, nada é gravado.

Saída: um CSV de auditoria (padrão saida/auditoria_fase2.csv) para revisar/importar
na planilha antes de gerar o PDF.
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

CAMPOS = ("preco_brl", "pcs_cx", "nome_pt", "nome_cn", "status_montagem")
CAMPOS_AUDITADOS = {"preco_brl": "preco_alterado", "pcs_cx": "pcs_alterado"}
CAMPOS_CONFLITO = ("preco_brl", "pcs_cx", "status_montagem")   # divergências entre ocorrências do SKU

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


# ------------------------------------------------------------ leitura/normalização
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
    """Converte um item do JSON (qualquer perfil) no formato do banco."""
    sku = _texto(p.get("sku"))
    preco = _num(p.get("preco_brl") if p.get("preco_brl") is not None else p.get("preco_cn"), float)
    flags = []
    if preco == 0:
        flags.append("preco_zerado")
    status = _texto(p.get("status_montagem"))
    return {
        "sku": sku.upper() if sku else None,
        "preco_brl": preco,
        "pcs_cx": _num(p.get("pcs_cx"), int),
        "nome_pt": _texto(p.get("nome_pt")),
        "nome_cn": _texto(p.get("nome_cn")),
        "status_montagem": status.lower() if status else None,
        # a página só faz sentido para o catálogo geral (perfil com preco_cn)
        "pagina_origem": _num(p.get("pagina"), int) if "preco_cn" in p else None,
        "origem": f"{Path(arquivo).parent.name}/{Path(arquivo).name}:pág {p.get('pagina', '?')}",
        "flags": flags,
        "avisos_fase1": list(p.get("avisos") or []),
    }


def carregar_registros(caminhos):
    registros = []
    for c in caminhos:
        with open(c, "r", encoding="utf-8") as f:
            registros += [normalizar(p, c) for p in json.load(f)]
    return registros


# ------------------------------------------------------------------- mesclagem
def mesclar(registros, prioridade="ultimo"):
    """Junta ocorrências do mesmo SKU. Retorna (por_sku, pulados, conflitos)."""
    por_sku, pulados, conflitos = {}, [], []
    for r in registros:
        if not r["sku"]:
            pulados.append(r)
            continue
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
            # a página de origem sempre mantém a primeira ocorrência
            if campo == "pagina_origem":
                sobrescreve = atual[campo] is None
            else:
                sobrescreve = prioridade == "ultimo" or atual[campo] is None
            if sobrescreve:
                atual[campo] = novo
                atual["_orig"][campo] = r["origem"]
    return por_sku, pulados, conflitos


# --------------------------------------------------------------------- diff
def comparar(por_sku, existentes):
    """Compara com o que já está no banco. Retorna (novos, alterados, para_gravar)."""
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
                continue                      # vazio nunca apaga
            antigo = ex.get(campo)
            if campo == "preco_brl" and antigo is not None:
                antigo = round(float(antigo), 2)
            if campo == "pagina_origem" and antigo is not None:
                continue                      # mantém a primeira página registrada
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
        if r["preco_brl"] is None:
            linhas.append({"tipo": "sem_preco", "sku": r["sku"], "campo": "preco_brl",
                           "valor_anterior": "", "valor_novo": "", "origem": r["origem"],
                           "detalhe": "preço ausente ou zerado: não publicar sem corrigir"})
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
    for r in por_sku.values():
         if "preco_zerado" in r["flags"] and r["preco_brl"] == 0:
            linhas.append({"tipo": "preco_zerado", "sku": r["sku"], "campo": "preco_brl",
                           "valor_anterior": "", "valor_novo": "", "origem": r["origem"],
                           "detalhe": "Unid.CX = 0,00 no PDF de origem"})
    return linhas


def salvar_auditoria(linhas, caminho):
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    cols = ["tipo", "sku", "campo", "valor_anterior", "valor_novo", "origem", "detalhe"]
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: abre bem no Excel
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(linhas)


# ------------------------------------------------------------------- banco
def carregar_existentes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('catalogo_produtos')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("""SELECT sku, preco_brl, pcs_cx, nome_pt, nome_cn, status_montagem
                       FROM catalogo_produtos""")
        cols = [d[0] for d in cur.description]
        existentes = {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'catalogo_produtos' AND column_name = 'pagina_origem'""")
        if cur.fetchone():
            cur.execute("SELECT sku, pagina_origem FROM catalogo_produtos")
            for sku, pag in cur.fetchall():
                existentes[sku]["pagina_origem"] = pag
        return existentes


def gravar(conn, para_gravar):
    from psycopg2.extras import execute_values
    valores = [(r["sku"], r["preco_brl"], r["pcs_cx"], r["nome_pt"], r["nome_cn"],
                r["status_montagem"], r["pagina_origem"]) for r in para_gravar]
    with conn.cursor() as cur:
        execute_values(cur, UPSERT, valores)


def main():
    ap = argparse.ArgumentParser(description="Sincroniza os JSONs da Fase 1 com o PostgreSQL.")
    ap.add_argument("json", nargs="*", default=["saida/produtos.json"],
                    help="um ou mais produtos.json; o último tem prioridade")
    ap.add_argument("--prioridade", choices=["ultimo", "primeiro"], default="ultimo",
                    help="qual ocorrência vence quando o SKU se repete (padrão: ultimo)")
    ap.add_argument("--auditoria", default="saida/auditoria_fase2.csv")
    ap.add_argument("--dry-run", action="store_true", help="só calcula e gera o CSV; não grava")
    a = ap.parse_args()

    registros = carregar_registros(a.json)
    por_sku, pulados, conflitos = mesclar(registros, a.prioridade)
    print(f"Lidos: {len(registros)} registros | SKUs únicos: {len(por_sku)} "
          f"| sem SKU (pulados): {len(pulados)} | conflitos de preço/volume/status: {len(conflitos)}")

    import psycopg2
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if not a.dry_run:
            with conn.cursor() as cur:
                cur.execute(DDL)
        existentes = carregar_existentes(conn)
        novos, alterados, para_gravar = comparar(por_sku, existentes)
        if not a.dry_run:
            gravar(conn, para_gravar)
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

    n_preco = sum(1 for _, m in alterados if any(c == "preco_brl" for c, _, _ in m))
    n_pcs = sum(1 for _, m in alterados if any(c == "pcs_cx" for c, _, _ in m))
    sem_preco = sum(1 for l in linhas if l["tipo"] == "sem_preco")
    print("\n=== RESUMO DA AUDITORIA" + (" (DRY-RUN: nada gravado)" if a.dry_run else "") + " ===")
    print(f"SKUs novos:               {len(novos)}")
    print(f"SKUs com preço alterado:  {n_preco}")
    print(f"SKUs com volume alterado: {n_pcs}")
    print(f"Outros campos alterados:  {len(alterados)} SKUs no total com alguma mudança")
    print(f"Novos sem preço:          {sem_preco}")
    print(f"Registros sem SKU:        {len(pulados)}")
    print(f"Conflitos (preço/volume/status): {len(conflitos)}")
    print(f"Relatório: {a.auditoria}")
    print("=" * 44)


if __name__ == "__main__":
    main()