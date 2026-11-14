import json
import re

# O padrão rígido antigo (que achou 642 produtos)
padrao_rigido = re.compile(r"^[A-Z]{1,5}-?\d{1,6}[A-Z]?$")

with open("saida/produtos.json", encoding="utf-8") as f:
    produtos = json.load(f)

# Filtra tudo que passou agora, mas que tinha sido bloqueado antes
diferentes = [p["sku"] for p in produtos if not padrao_rigido.match(p["sku"])]

print(f"Temos {len(diferentes)} códigos suspeitos:")
print(", ".join(diferentes))