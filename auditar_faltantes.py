import json

with open("saida/produtos.json", encoding="utf-8") as f:
    produtos = json.load(f)

sem_preco = [p["sku"] for p in produtos if p["preco_brl"] is None]
sem_nome = [p["sku"] for p in produtos if not p["nome_pt"]]
sem_pcs = [p["sku"] for p in produtos if p["pcs_cx"] is None]

print(f"Total de produtos analisados: {len(produtos)}")
print(f"❌ Produtos sem preço: {len(sem_preco)} -> {sem_preco[:10]}")
print(f"❌ Produtos sem nome em PT: {len(sem_nome)} -> {sem_nome[:10]}")
print(f"❌ Produtos sem quantidade (PCS/CX): {len(sem_pcs)} -> {sem_pcs[:10]}")