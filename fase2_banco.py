"""Fase 2 - Sincronização do JSON com o banco PostgreSQL.

Uso: python fase2_banco.py
"""
import json
import psycopg2
from psycopg2.extras import RealDictCursor

# Configurações com base no docker-compose.yml
DB_CONFIG = {
    "dbname": "lukton_catalog",
    "user": "admin",
    "password": "admin",
    "host": "localhost",
    "port": "5432"
}

def criar_tabela(conn):
    with conn.cursor() as cur:
        cur.execute("""
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
        """)
    conn.commit()

def sincronizar_banco(conn, json_path="saida/produtos.json"):
    with open(json_path, 'r', encoding='utf-8') as f:
        produtos = json.load(f)

    novos = 0
    atualizados = 0

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for p in produtos:
            preco = p.get("preco_brl")
            pcs_cx = p.get("pcs_cx")

            # Verifica se o SKU já existe para contabilizar o diff da auditoria
            cur.execute("SELECT preco_brl, pcs_cx FROM catalogo_produtos WHERE sku = %s", (p["sku"],))
            existente = cur.fetchone()

            if not existente:
                novos += 1
            else:
                # Converte o preço do banco (Decimal) para float para a comparação
                preco_banco = float(existente["preco_brl"]) if existente["preco_brl"] else None
                if preco_banco != preco or existente["pcs_cx"] != pcs_cx:
                    atualizados += 1

            # UPSERT: Insere ou atualiza os dados
            cur.execute("""
                INSERT INTO catalogo_produtos (sku, preco_brl, pcs_cx, nome_pt, nome_cn, status_montagem)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET
                    preco_brl = EXCLUDED.preco_brl,
                    pcs_cx = EXCLUDED.pcs_cx,
                    nome_pt = EXCLUDED.nome_pt,
                    nome_cn = EXCLUDED.nome_cn,
                    status_montagem = EXCLUDED.status_montagem,
                    data_atualizacao = CURRENT_TIMESTAMP;
            """, (
                p["sku"], preco, pcs_cx,
                p.get("nome_pt"), p.get("nome_cn"), p.get("status_montagem")
            ))

    conn.commit()
    return novos, atualizados, len(produtos)

if __name__ == "__main__":
    print("Conectando ao PostgreSQL local...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        criar_tabela(conn)
        
        print("Iniciando sincronização dos produtos...")
        novos, alterados, total = sincronizar_banco(conn)
        
        print("\n=== RESUMO DA AUDITORIA ===")
        print(f"Total de SKUs processados: {total}")
        print(f"Novos SKUs catalogados:    {novos}")
        print(f"SKUs com preço alterado:   {alterados}")
        print("===========================\n")
        
        conn.close()
    except Exception as e:
        print(f"Erro na execução: {e}")