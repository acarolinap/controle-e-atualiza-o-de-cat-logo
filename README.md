# Controle e atualização do catálogo LUKTON

Pipeline em Python que lê o **catálogo geral em mandarim** (PDF), extrai cada produto e gera o **catálogo em português** (PDF), mantendo **as mesmas páginas, os mesmos produtos e a mesma ordem do original**.

```
PDF em mandarim ──► Fase 1 ──► Fase 2c ──► Fase 3 ──► catálogo PDF em português
                  (extrai)   (banco)    (gera PDF)
                                  └──► verificar_ordem.py (confere a ordem)
```

## Como funciona

O PDF gerado **espelha** o original: cada página de origem vira uma página brasileira, com os cards na mesma posição (3 colunas × 3 linhas). SKUs que se repetem no original continuam repetidos.

| Etapa | Script | O que faz |
|---|---|---|
| 1 | `fase1_extrator.py` | Lê o PDF (PyMuPDF), localiza cada card pela linha `PCS/CX`, extrai SKU, nomes (CN/PT), preço, PCS/CX, status de montagem e recorta a foto. Gera `produtos.json` e `assets/img/`. |
| 1b | `fase1b_secoes.py` | Detecta os títulos de página (NOVIDADES / PROMOÇÃO) e os cards em promoção (`特价`). É chamado pela Fase 2c. |
| 2c | `fase2c_paginas.py` | Grava no PostgreSQL a tabela `catalogo_cards` (uma linha por card, repetidos incluídos) e `catalogo_paginas`. Compara a quantidade de cards por página com o PDF e **recusa gravar se houver divergência**. |
| 3 | `fase3_gerador.py` | Lê `catalogo_cards`, renderiza o template (`templates/catalogo.html`) com Jinja2 e gera o PDF com Playwright, em lotes de 12 páginas, unindo tudo em `catalogo_final_lukton.pdf`. |
| – | `verificar_ordem.py` | Confere a ordem dos cards: (1) PDF original × JSON e (2) JSON × PDF final. Gera `saida/auditoria_ordem.csv`. |

### Scripts auxiliares

`achar_duplicados.py`, `achar_fantasmas.py` (itens sem imagem), `auditar_faltantes.py` (sem preço, nome ou PCS/CX), `verificar.py` (SKUs fora do padrão) e `resumo.py` leem `saida/produtos.json` e servem para auditorias rápidas.

`fase2_banco.py` e `fase1_extrator_antigo.py` são versões antigas do fluxo, mantidas apenas como referência.

## Requisitos

- Python 3.11+
- PostgreSQL com as tabelas `catalogo_produtos` (traduções de nomes e `status_publicacao`) e, geradas pela Fase 2c, `catalogo_cards` e `catalogo_paginas`
- Bibliotecas:

```powershell
pip install pymupdf psycopg2-binary jinja2 playwright pypdf pillow python-dotenv
playwright install chromium
```

### Configuração do banco

Os scripts leem estas variáveis de ambiente (ou um arquivo `.env`, que **não deve ir para o Git**):

```
DB_NAME=lukton_catalog
DB_USER=...
DB_PASSWORD=...
DB_HOST=localhost
DB_PORT=5432
```

## Estrutura de pastas

```
.
├── fase1_extrator.py
├── fase1b_secoes.py
├── fase2c_paginas.py
├── fase3_gerador.py
├── verificar_ordem.py
├── templates/
│   ├── catalogo.html        # template do catálogo
│   └── assets/logo.*        # logo (recortada automaticamente)
├── saida/                   # catálogo traduzido (produtos.json + imagens) e relatórios
├── saida_geral/             # extração do catálogo em mandarim (produtos.json + imagens)
└── saida_catalogo/          # lotes e PDF final gerado
```

As pastas `saida*/` são geradas pelos scripts e ficam fora do Git.

## Como atualizar quando chegar um catálogo novo

Coloque o PDF novo na pasta do projeto e rode, na ordem:

```powershell
python fase1_extrator.py "catalogo novo.pdf" --perfil geral --out saida_geral/
python fase2c_paginas.py saida_geral/produtos.json --traduzido saida/produtos.json --pdf "catalogo novo.pdf"
python fase3_gerador.py
python verificar_ordem.py "catalogo novo.pdf"
```

Resultado: `saida_catalogo/catalogo_final_lukton.pdf`.

Opções úteis:

- `fase1_extrator.py --paginas 1-3` e `fase3_gerador.py --paginas 1-12`: testes rápidos em poucas páginas.
- `fase2c_paginas.py --dry-run`: calcula e gera o relatório sem gravar no banco.
- `fase3_gerador.py --logo "caminho\logo.png"`: define a logo.

### Como saber se deu tudo certo

1. A Fase 2c termina com `GRAVADO: ... cards em catalogo_cards`. Se aparecer "Nada foi gravado", o número de cards por página não bate com o PDF; veja `saida/auditoria_cards.csv`.
2. A Fase 3 lista no log as lacunas (cards sem SKU, imagem, nome em português, preço ou status).
3. O `verificar_ordem.py` aponta qualquer card fora de posição em `saida/auditoria_ordem.csv`.

## Regras importantes

- **O catálogo em mandarim é a fonte da verdade** para preço, PCS/CX, status e posição de cada card. O catálogo traduzido só empresta o nome em português.
- **Ordem dos cards:** linha por linha (de cima para baixo) e, dentro de cada linha, da esquerda para a direita. Os cards de uma mesma linha têm diferença de 1–3 pt em Y, por isso a ordenação agrupa por linha antes de ordenar por X (função `ordem_de_leitura`).
- **Valores ausentes nunca são inventados:** sem nome em português sai só o nome em chinês; sem status, a faixa fica só com a logo.
- **Promoções:** card com `特价` mostra o preço promocional em vermelho e o preço normal riscado.
- **Inativos:** SKUs com `status_publicacao <> 'ativo'` em `catalogo_produtos` são removidos de todas as páginas.
- A tabela `catalogo_cards` é **refeita do zero** a cada execução da Fase 2c; correções manuais feitas direto nela são perdidas.

## Limitações conhecidas

- O extrator foi ajustado ao layout atual (3 colunas, faixa de título e posição das promoções). Um PDF com layout diferente exige ajuste e será acusado pela conferência de cards por página.
- Cards cujo SKU não foi lido pelo extrator precisam ser preenchidos manualmente; no catálogo de 12/11 foram dois (pág. 6, posição 3, e pág. 100, posição 6).
- Cards cujo nome impresso é o próprio código do modelo têm o SKU repetido no mesmo card; o verificador já trata esse caso.