import json
from collections import Counter

with open("saida/produtos.json", encoding="utf-8") as f:
    produtos = json.load(f)

skus = [p["sku"] for p in produtos]
contagem = Counter(skus)
duplicados = {sku: qtd for sku, qtd in contagem.items() if qtd > 1}

print(f"Total de itens capturados: {len(skus)}")
print(f"Total de SKUs ÚNICOS: {len(set(skus))}")

if duplicados:
    print(f"\nExistem {len(duplicados)} SKUs repetidos no catálogo:")
    for sku, qtd in duplicados.items():
        print(f"- {sku} aparece {qtd} vezes")