"""Fase 3 - Geração do Catálogo PDF com Jinja2 e Playwright (Em Lotes/Blocos).

Uso: python fase3_gerador.py
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from jinja2 import Template
from playwright.sync_api import sync_playwright

DB_CONFIG = {
    "dbname": "lukton_catalog",
    "user": "admin",
    "password": "admin",
    "host": "localhost",
    "port": "5432"
}

def buscar_produtos_banco():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT sku, preco_brl, pcs_cx, nome_pt, nome_cn, status_montagem FROM catalogo_produtos ORDER BY sku;")
        produtos = cur.fetchall()
    conn.close()
    return produtos

def gerar_pdf_catalogo():
    print("Buscando produtos do banco de dados...")
    produtos = buscar_produtos_banco()
    total_produtos = len(produtos)
    print(f"Total de {total_produtos} produtos encontrados.")

    template_path = os.path.join("templates", "catalogo.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html_template = f.read()

    os.makedirs("saida", exist_ok=True)
    
    TAMANHO_LOTE = 120
    lotes = [produtos[i:i + TAMANHO_LOTE] for i in range(0, total_produtos, TAMANHO_LOTE)]
    
    pdf_parciais = []

    print("Iniciando Playwright para renderização em lotes...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--js-flags=--max-old-space-size=4096"
            ]
        )
        
        for idx, lote in enumerate(lotes, 1):
            print(f"Processando lote {idx} de {len(lotes)} ({len(lote)} produtos)...")
            t = Template(html_template)
            html_renderizado = t.render(produtos=lote)

            html_path = os.path.abspath(os.path.join("saida", f"lote_{idx}.html"))
            pdf_path = os.path.abspath(os.path.join("saida", f"lote_{idx}.pdf"))

            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_renderizado)

            page = browser.new_page()
            page.goto(f"file:///{html_path}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            
            page.pdf(
                path=pdf_path,
                format="A4",
                print_background=True,
                margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"}
            )
            page.close()
            pdf_parciais.append(pdf_path)

        browser.close()

    print("Unificando os PDFs parciais em um único catálogo final...")
    from pypdf import PdfWriter
    
    merger = PdfWriter()
    
    for pdf in pdf_parciais:
        merger.append(pdf)
        
    pdf_final = os.path.abspath(os.path.join("saida", "catalogo_final_lukton.pdf"))
    with open(pdf_final, "wb") as output_file:
        merger.write(output_file)
    merger.close()

    print(f"\n[SUCESSO] Catálogo PDF gerado com sucesso em:\n-> {pdf_final}\n")

if __name__ == "__main__":
    gerar_pdf_catalogo()