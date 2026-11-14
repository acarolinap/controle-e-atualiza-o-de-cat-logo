"""Fase 1 - Extrator do catálogo Lukton (PyMuPDF: pip install pymupdf).

Uso:
  python fase1_extrator.py "Catálogo Traduzido 13.11.pdf" --perfil traduzido --out saida_trad/
  python fase1_extrator.py "catalogo_geral_LUKTON_12_11.pdf" --perfil geral --paginas 1-3 --out saida_geral/
  python fase1_extrator.py "catalogo_geral_LUKTON_12_11.pdf" --perfil geral --sem-imagens --out saida_geral/

Saída: <out>/produtos.json + <out>/assets/img/{SKU}.png
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

# --- comuns
SKU_RE = re.compile(r"^[A-Z]{1,5}-\d{1,5}[A-Z]?$")             # palavra isolada
SKU_SOLTO_RE = re.compile(r"[A-Za-z]{1,5}\d{0,5}(?:-[A-Za-z0-9]{1,6})+")   # dentro de uma linha
PRECO_RE = re.compile(r"^\d{1,4},\d{2}$")
CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")
TOL_DUP = 1.5      # pt: tolerância p/ considerar caractere "sombra"
TOL_LINHA = 3.0    # pt: tolerância em Y p/ agrupar caracteres numa linha

# --- perfil traduzido (coordenadas do fitz)
RODAPE_Y = 760
CABECALHO_Y = 95

# --- perfil geral
PCS_RE = re.compile(r"PCS/CX:\s*([\d.,]+)")
UNID_RE = re.compile(r"Unid\.CX:\s*([\d.,]+)")
STATUS_TOKEN_RE = re.compile(
    r"/?\s*(?<![A-Za-z])(Desmontado|montado|散装|包好)(?![A-Za-z])\s*/?", re.I)
STATUS_MAP = {"desmontado": "desmontado", "montado": "montado",
              "散装": "desmontado", "包好": "montado"}
PLACEHOLDER_NOME = "中文品名"
FOTO_GERAL_W = (100, 260)   # largura da foto do card (~135 a ~215pt conforme a seção)
FOTO_GERAL_H = (75, 200)    # altura da foto do card


# ---------------------------------------------------------------- PyMuPDF
def _load_fitz(pdf_path):
    import pymupdf as fitz
    doc = fitz.open(pdf_path)

    def paginas(selecao=None):
        for n in (selecao or range(1, doc.page_count + 1)):
            page = doc[n - 1]
            chars = []
            for b in page.get_text("rawdict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        for c in s["chars"]:
                            x0, y0, x1, y1 = c["bbox"]
                            chars.append((c["c"], x0, y0, x1, y1))
            imgs = [tuple(i["bbox"]) for i in page.get_image_info()]
            dims = (page.rect.width, page.rect.height)

            def render(rect, dpi=300, _p=page):
                return _p.get_pixmap(clip=fitz.Rect(*rect), dpi=dpi)

            def salvar(pix, caminho):
                pix.save(caminho)

            yield n, chars, imgs, dims, render, salvar

    paginas.total = doc.page_count
    return paginas


def carregar(pdf_path):
    return _load_fitz(pdf_path), "pymupdf"


def parse_paginas(txt, total):
    """'1-3,7' -> [1,2,3,7] (apenas páginas existentes)."""
    if not txt:
        return None
    nums = []
    for parte in txt.split(","):
        parte = parte.strip()
        if "-" in parte:
            a, b = parte.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            rng = [int(parte)]
        nums.extend(n for n in rng if 1 <= n <= total)
    return nums


# ------------------------------------------------------------ texto/sombras
def dedup_chars(chars):
    """Remove invisíveis, normaliza (NFKC) e descarta caracteres-sombra:
    mesmo glifo, mesma posição (±TOL_DUP). 'SS' legítimo é preservado."""
    chars = [(unicodedata.normalize("NFKC", c[0]),) + tuple(c[1:]) for c in chars
             if unicodedata.category(c[0][:1] or " ") not in ("Cf", "Cc")]
    mantidos = []
    for c in sorted(chars, key=lambda t: (round(t[2]), t[1])):
        if any(m[0] == c[0] and abs(m[1] - c[1]) < TOL_DUP
               and abs(m[2] - c[2]) < TOL_DUP for m in mantidos):
            continue
        mantidos.append(c)
    return mantidos


def montar_palavras(chars):
    """Agrupa caracteres em palavras (por linha em Y e lacuna em X)."""
    linhas = []
    for c in sorted(chars, key=lambda t: (t[2], t[1])):
        if c[0].isspace():
            continue
        for ln in linhas:
            if abs(ln["y"] - c[2]) < TOL_LINHA:
                ln["cs"].append(c)
                break
        else:
            linhas.append({"y": c[2], "cs": [c]})
    palavras = []
    for ln in linhas:
        cs = sorted(ln["cs"], key=lambda t: t[1])
        atual = [cs[0]]
        for c in cs[1:]:
            if c[1] - atual[-1][3] > 0.18 * (c[4] - c[2]):  # lacuna proporcional à fonte
                palavras.append(atual)
                atual = []
            atual.append(c)
        palavras.append(atual)
    return [{
        "t": "".join(c[0] for c in w),
        "x0": min(c[1] for c in w), "y0": min(c[2] for c in w),
        "x1": max(c[3] for c in w), "y1": max(c[4] for c in w),
    } for w in palavras]


def linhas_da_zona(ws):
    """Agrupa palavras em linhas pelo centro em Y -> [texto, ...] de cima p/ baixo."""
    linhas = []
    for w in sorted(ws, key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"])):
        cy = (w["y0"] + w["y1"]) / 2
        for ln in linhas:
            if abs(ln["y"] - cy) < TOL_LINHA:
                ln["ws"].append(w)
                break
        else:
            linhas.append({"y": cy, "ws": [w]})
    return [" ".join(w["t"] for w in sorted(ln["ws"], key=lambda w: w["x0"]))
            for ln in sorted(linhas, key=lambda ln: ln["y"])]


def r1(t):
    return tuple(round(v, 1) for v in t)


# ================================================================ TRADUZIDO
def cards_traduzido(palavras, imgs):
    # fotos reais: ~135pt de largura. As fatias de fundo/sombra do card que o
    # fitz também lista têm >=177pt e aparecem duplicadas, então ficam de fora.
    fotos = []
    for i in imgs:
        w, h = i[2] - i[0], i[3] - i[1]
        if 100 < w < 160 and 60 < h < 140 and i[1] > CABECALHO_Y - 10:
            if not any(all(abs(a - b) < 1 for a, b in zip(i, f)) for f in fotos):
                fotos.append(i)
    skus = [w for w in palavras if SKU_RE.match(w["t"]) and w["y0"] > CABECALHO_Y]
    cards = []
    for s in skus:
        cx = (s["x0"] + s["x1"]) / 2
        cand = [f for f in fotos if f[0] - 10 <= cx <= f[2] + 10 and f[3] <= s["y0"] + 5]
        foto = max(cand, key=lambda f: f[3]) if cand else None
        if not foto:
            continue
        abaixo = [f[1] for f in fotos if f[1] > s["y1"] + 5
                  and f[0] - 10 <= cx <= f[2] + 10]
        y_fim = min(abaixo + [RODAPE_Y]) - 2
        cards.append((s, foto, (foto[0] - 25, s["y0"] - 3, foto[2] + 25, y_fim)))
    return cards


def campos_traduzido(s, zona, palavras):
    ws = [w for w in palavras
          if zona[0] <= (w["x0"] + w["x1"]) / 2 <= zona[2]
          and zona[1] <= w["y0"] <= zona[3]]
    ws.sort(key=lambda w: (round(w["y0"] / TOL_LINHA), w["x0"]))
    preco = next((float(w["t"].replace(",", ".")) for w in ws
                  if PRECO_RE.match(w["t"]) and (w["y1"] - w["y0"]) > 12), None)
    pcs = None
    for i, w in enumerate(ws):
        if w["t"].upper().startswith("PCS/CX") and i > 0 and ws[i - 1]["t"].isdigit():
            pcs = int(ws[i - 1]["t"])
    txt = " ".join(w["t"] for w in ws)
    montagem = "desmontado" if "DESMONTADO" in txt else "montado" if "MONTADO" in txt else None
    ignora = lambda t: (t == s["t"] or t == "R$" or PRECO_RE.match(t) or t.isdigit()
                        or t.upper().startswith("PCS/CX") or t in ("MONTADO", "DESMONTADO")
                        or t in ("包好", "散装"))
    pt, cn = [], []
    for w in ws:
        if not ignora(w["t"]):
            (cn if CJK_RE.search(w["t"]) else pt).append(w["t"])
    return {"sku": s["t"], "preco_brl": preco, "pcs_cx": pcs,
            "nome_pt": " ".join(pt), "nome_cn": "".join(cn),
            "status_montagem": montagem}


def debug_traduzido(s, foto, zona, palavras, imgs):
    print(f"\n=== DEBUG {s['t']} ===")
    print("sku bbox :", r1((s["x0"], s["y0"], s["x1"], s["y1"])))
    print("foto     :", r1(foto))
    print("zona     :", r1(zona))
    cx = (s["x0"] + s["x1"]) / 2
    print("imagens na coluna (x sobreposto):")
    for i in imgs:
        if i[0] - 10 <= cx <= i[2] + 10:
            print("   ", r1(i))
    print("palavras na faixa X do card (D=dentro da zona):")
    for w in sorted(palavras, key=lambda w: (w["y0"], w["x0"])):
        mx = (w["x0"] + w["x1"]) / 2
        if zona[0] <= mx <= zona[2] and s["y0"] - 5 <= w["y0"] <= s["y0"] + 140:
            d = "D" if zona[1] <= w["y0"] <= zona[3] else "-"
            print(f"   {d} {r1((w['x0'], w['y0'], w['x1'], w['y1']))} {w['t']!r}")
    print("======\n")


def processar_traduzido(n, palavras, imgs, dims, a, salvar_foto):
    regs = []
    for s, foto, zona in cards_traduzido(palavras, imgs):
        if a.debug == s["t"]:
            debug_traduzido(s, foto, zona, palavras, imgs)
        p = campos_traduzido(s, zona, palavras)
        p["pagina"] = n
        salvar_foto(foto, p["sku"])
        regs.append(p)
    return regs


# ===================================================================== GERAL
def fotos_geral(imgs):
    fotos = []
    for i in imgs:
        w, h = i[2] - i[0], i[3] - i[1]
        if FOTO_GERAL_W[0] < w < FOTO_GERAL_W[1] and FOTO_GERAL_H[0] < h < FOTO_GERAL_H[1]:
            if not any(all(abs(x - y) < 1 for x, y in zip(i, f)) for f in fotos):
                fotos.append(i)
    return fotos


def cards_geral(palavras, imgs, dims):
    """Âncora: a linha 'PCS/CX:' (uma por card). Coluna = terço da página."""
    larg = dims[0]
    fotos = fotos_geral(imgs)
    cards = []
    for p in sorted((w for w in palavras if w["t"].startswith("PCS/CX")),
                    key=lambda w: (w["y0"], w["x0"])):
        col = min(2, int(((p["x0"] + p["x1"]) / 2) / (larg / 3)))
        x0, x1 = col * larg / 3, (col + 1) * larg / 3
        cand = [f for f in fotos if x0 <= (f[0] + f[2]) / 2 <= x1
                and p["y0"] - 90 <= f[3] <= p["y0"] + 2]
        # a maior área evita pegar fatias de fundo/sombra que o fitz também lista
        foto = max(cand, key=lambda f: (f[2] - f[0]) * (f[3] - f[1])) if cand else None
        # o SKU às vezes se sobrepõe ao canto inferior da foto (~14pt) ou fica
        # abaixo dela: o topo da zona é o fundo da foto menos essa folga
        topo = max(p["y0"] - 90, foto[3] - 14) if foto else p["y0"] - 70
        cards.append((p, foto, (x0, topo, x1, p["y1"] + 3)))
    return cards


def achar_skus(texto):
    """SKUs dentro de um texto: precisam ter hífen e pelo menos um dígito
    (descarta 'USB-C', 'type-c')."""
    return [m for m in SKU_SOLTO_RE.finditer(texto) if any(c.isdigit() for c in m.group(0))]


def _num_pcs(bruto):
    if re.fullmatch(r"\d+", bruto):
        return int(bruto)
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", bruto):      # 2.000 -> 2000
        return int(re.sub(r"[.,]", "", bruto))
    return None


def _num_preco(bruto):
    limpo = bruto.lstrip(".,").replace(",", ".")
    try:
        val = float(limpo)
    except ValueError:
        return None, True
    return val, not re.fullmatch(r"\d+(\.\d{1,2})?", bruto)  # (valor, formato estranho?)


def campos_geral(zona, palavras):
    ws = [w for w in palavras
          if zona[0] <= (w["x0"] + w["x1"]) / 2 <= zona[2]
          and zona[1] <= (w["y0"] + w["y1"]) / 2 <= zona[3]]
    linhas = linhas_da_zona(ws)
    tecnico, status_vistos, skus_vistos = "", [], []
    pt_parts, cn_parts = [], []
    for txt in linhas:
        if "PCS/CX" in txt or "Unid.CX" in txt:
            tecnico += " " + txt
            continue
        if PLACEHOLDER_NOME in txt:
            continue
        # marcadores de status (Desmontado/montado/散装/包好), em qualquer posição
        for m in STATUS_TOKEN_RE.finditer(txt):
            status_vistos.append(STATUS_MAP[m.group(1).lower()])
        txt = STATUS_TOKEN_RE.sub(" ", txt)
        # SKUs
        for m in achar_skus(txt):
            skus_vistos.append(m.group(0))
        for m in reversed(achar_skus(txt)):
            txt = txt[:m.start()] + " " + txt[m.end():]
        txt = re.sub(r"\s*/\s*", " ", txt).strip(" /")
        txt = re.sub(r"\s+", " ", txt)
        if txt:
            (cn_parts if CJK_RE.search(txt) else pt_parts).append(txt)

    avisos = []
    sku = skus_vistos[-1] if skus_vistos else None
    if sku and sku != sku.upper():
        avisos.append("SKU em minúsculas (normalizado)")
        sku = sku.upper()
    if not sku:
        avisos.append("SKU não encontrado")
    nome_cn = " ".join(cn_parts) or None
    nome_pt = " ".join(pt_parts) or None
    if not nome_cn and not nome_pt and len(skus_vistos) >= 2:
        nome_cn = skus_vistos[0].upper()        # o nome impresso é o código do modelo
        avisos.append("nome = código do modelo")
    elif not nome_cn and not nome_pt:
        avisos.append("sem nome")
    if len(set(status_vistos)) > 1:
        avisos.append("status divergente (" + " x ".join(sorted(set(status_vistos))) + ")")
    status = status_vistos[0] if status_vistos else None

    pcs_m, unid_m = PCS_RE.search(tecnico), UNID_RE.search(tecnico)
    pcs = _num_pcs(pcs_m.group(1)) if pcs_m else None
    if pcs is None:
        avisos.append("PCS/CX não encontrado" if not pcs_m else f"PCS/CX estranho ({pcs_m.group(1)})")
    preco = None
    if not unid_m:
        avisos.append("Unid.CX não encontrado")
    else:
        preco, estranho = _num_preco(unid_m.group(1))
        if preco is None:
            avisos.append(f"Unid.CX ilegível ({unid_m.group(1)})")
        elif estranho:
            avisos.append(f"Unid.CX com formato estranho ({unid_m.group(1)})")
        if preco == 0:
            avisos.append("Unid.CX zerado")
    return {
        "sku": sku, "nome_cn": nome_cn, "nome_pt": nome_pt,
        "preco_cn": preco, "pcs_cx": pcs,
        "status_montagem": status, "avisos": avisos,
    }, linhas


def processar_geral(n, palavras, imgs, dims, a, salvar_foto, vistos):
    regs = []
    for pcs_w, foto, zona in cards_geral(palavras, imgs, dims):
        p, linhas = campos_geral(zona, palavras)
        if a.debug and a.debug == p["sku"]:
            print(f"\n=== DEBUG {p['sku']} (pág {n}) ===")
            print("foto:", r1(foto) if foto else None, "| zona:", r1(zona))
            for t in linhas:
                print("   ", repr(t))
            print("campos:", p, "\n======\n")
        p["pagina"] = n
        if not foto:
            p["avisos"].append("foto não encontrada")
        elif p["sku"]:
            nome_arq = p["sku"]
            if p["sku"] in vistos:
                p["avisos"].append(f"SKU repetido (1ª vez na pág {vistos[p['sku']]})")
                nome_arq = f"{p['sku']}__p{n}"
            else:
                vistos[p["sku"]] = n
            salvar_foto(foto, nome_arq)
        regs.append(p)
    return regs


# ======================================================================= main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--perfil", choices=["traduzido", "geral"], default="traduzido")
    ap.add_argument("--out", default="saida")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--paginas", help="ex.: 1-3,7 (padrão: todas)")
    ap.add_argument("--sem-imagens", action="store_true", help="só texto (varredura rápida)")
    ap.add_argument("--debug", metavar="SKU", help="imprime a zona e as palavras desse SKU")
    a = ap.parse_args()

    out = Path(a.out)
    (out / "assets" / "img").mkdir(parents=True, exist_ok=True)
    paginas, backend = carregar(a.pdf)
    sel = parse_paginas(a.paginas, paginas.total)
    produtos, vistos = [], {}
    for n, chars, imgs, dims, render, salvar in paginas(sel):
        palavras = montar_palavras(dedup_chars(chars))

        def salvar_foto(rect, nome, _r=render, _s=salvar):
            if not a.sem_imagens:
                _s(_r(rect, a.dpi), str(out / "assets" / "img" / f"{nome}.png"))

        if a.perfil == "traduzido":
            produtos += processar_traduzido(n, palavras, imgs, dims, a, salvar_foto)
        else:
            produtos += processar_geral(n, palavras, imgs, dims, a, salvar_foto, vistos)

    (out / "produtos.json").write_text(
        json.dumps(produtos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{backend}] perfil={a.perfil}: {len(produtos)} produtos -> {out / 'produtos.json'}")
    if a.perfil == "geral":
        from collections import Counter
        tipos = Counter(av.split(" (")[0] for p in produtos for av in p["avisos"])
        com_aviso = [p for p in produtos if p["avisos"]]
        print(f"cards com aviso: {len(com_aviso)} de {len(produtos)}")
        for tipo, q in tipos.most_common():
            print(f"  {q:4d}  {tipo}")
        print("(detalhes no campo 'avisos' de cada item do produtos.json)")


if __name__ == "__main__":
    main()