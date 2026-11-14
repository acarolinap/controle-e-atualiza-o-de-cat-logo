import json
import os

with open("saida/produtos.json", encoding="utf-8") as f:
    produtos = json.load(f)

fantasmas = []
for p in produtos:
    caminho_img = f"saida/assets/img/{p['sku']}.png"
    if not os.path.exists(caminho_img):
        fantasmas.append(p["sku"])

print(f"Encontrados {len(fantasmas)} itens sem imagem:")
print(", ".join(fantasmas))