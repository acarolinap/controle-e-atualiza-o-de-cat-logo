import os
import json
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
JSON_PATH = BASE_DIR / "produtos_extraidos.json"


def conectar():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "lukton_catalogo"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def criar_tabelas(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS catalogo_produtos (
            sku            VARCHAR(40) PRIMARY KEY,
            nome_cn        TEXT,
            nome_pt        TEXT,
            preco          NUMERIC(10, 2),
            pcs_cx         INTEGER,
            imagem         TEXT,
            fonte_origem   VARCHAR(20),
            ativo          BOOLEAN DEFAULT TRUE,
            atualizado_em  TIMESTAMP DEFAULT NOW()
        );
    """)
    # Garante que todas as colunas existam caso a tabela tenha sido criada em versão anterior
    cur.execute("""
        ALTER TABLE catalogo_produtos
            ADD COLUMN IF NOT EXISTS nome_cn TEXT,
            ADD COLUMN IF NOT EXISTS nome_pt TEXT,
            ADD COLUMN IF NOT EXISTS preco NUMERIC(10, 2),
            ADD COLUMN IF NOT EXISTS pcs_cx INTEGER,
            ADD COLUMN IF NOT EXISTS imagem TEXT,
            ADD COLUMN IF NOT EXISTS fonte_origem VARCHAR(20),
            ADD COLUMN IF NOT EXISTS ativo BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS atualizado_em TIMESTAMP DEFAULT NOW();
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS catalogo_layout (
            id             SERIAL PRIMARY KEY,
            pagina         INTEGER NOT NULL,
            ordem_pagina   INTEGER NOT NULL,
            sku            VARCHAR(40) NOT NULL REFERENCES catalogo_produtos(sku) ON DELETE CASCADE,
            imagem_slot    TEXT,
            UNIQUE (pagina, ordem_pagina)
        );
    """)
    cur.execute("""
        ALTER TABLE catalogo_layout
            ADD COLUMN IF NOT EXISTS imagem_slot TEXT;
    """)


def carregar_json() -> list[dict]:
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def importar_produtos(cur, itens: list[dict]) -> int:
    # Consolida dados únicos por SKU (priorizando novidades para dados mestre, se houver)
    por_sku: dict[str, dict] = {}
    for it in itens:
        sku = it["sku"]
        if sku not in por_sku or it["fonte"] == "novidades":
            por_sku[sku] = it
        else:
            existente = por_sku[sku]
            for campo in ("preco", "pcs_cx", "nome_cn", "imagem"):
                if not existente.get(campo) and it.get(campo):
                    existente[campo] = it[campo]

    for sku, it in por_sku.items():
        cur.execute("""
            INSERT INTO catalogo_produtos (sku, nome_cn, preco, pcs_cx, imagem, fonte_origem)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (sku) DO UPDATE SET
                nome_cn       = COALESCE(EXCLUDED.nome_cn, catalogo_produtos.nome_cn),
                preco         = COALESCE(EXCLUDED.preco, catalogo_produtos.preco),
                pcs_cx        = COALESCE(EXCLUDED.pcs_cx, catalogo_produtos.pcs_cx),
                imagem        = COALESCE(EXCLUDED.imagem, catalogo_produtos.imagem),
                fonte_origem  = EXCLUDED.fonte_origem,
                atualizado_em = NOW();
        """, (sku, it.get("nome_cn"), it.get("preco"), it.get("pcs_cx"), it.get("imagem"), it.get("fonte")))

    return len(por_sku)


def reconstruir_layout(cur, itens: list[dict]) -> tuple[int, int]:
    """
    Grava todos os 767 slots do catálogo geral respeitando página, ordem na página e SKUs repetidos.
    """
    cur.execute("TRUNCATE TABLE catalogo_layout RESTART IDENTITY;")

    itens_geral = [it for it in itens if it["fonte"] == "geral"]
    itens_geral.sort(key=lambda x: (x["pagina"], x["ordem_pagina"]))

    total_inseridos = 0
    paginas_set = set()

    for it in itens_geral:
        cur.execute("""
            INSERT INTO catalogo_layout (pagina, ordem_pagina, sku, imagem_slot)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (pagina, ordem_pagina) DO UPDATE SET
                sku         = EXCLUDED.sku,
                imagem_slot = EXCLUDED.imagem_slot;
        """, (it["pagina"], it["ordem_pagina"], it["sku"], it.get("imagem")))
        total_inseridos += 1
        paginas_set.add(it["pagina"])

    return total_inseridos, len(paginas_set)


def main():
    if not JSON_PATH.exists():
        raise FileNotFoundError(f"JSON não encontrado: {JSON_PATH}. Rode fase1_extrator.py primeiro.")

    itens = carregar_json()
    conn = conectar()
    try:
        with conn:
            with conn.cursor() as cur:
                criar_tabelas(cur)
                total_skus = importar_produtos(cur, itens)
                total_slots, total_paginas = reconstruir_layout(cur, itens)

        print("Importação concluída:")
        print(f"  -> {total_skus} SKUs únicos salvos em 'catalogo_produtos'")
        print(f"  -> {total_slots} slots (incluindo repetidos) salvos em 'catalogo_layout'")
        print(f"  -> {total_paginas} páginas mapeadas no layout")
    finally:
        conn.close()


if __name__ == "__main__":
    main()