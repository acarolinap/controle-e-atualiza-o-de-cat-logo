import json
for p in json.load(open("saida/produtos.json", encoding="utf-8")):
    print(p["sku"], p["preco_brl"], p["pcs_cx"], p["status_montagem"], "|", p["nome_pt"], "|", p["nome_cn"])