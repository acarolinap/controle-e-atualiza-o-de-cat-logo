"""Fase 1 - Extrator do catálogo Lukton (SKU como âncora).

Usa PyMuPDF (pip install pymupdf).

Uso:  python fase1_extrator.py "Catálogo_Traduzido_13_11.pdf" --out saida/
Saída: saida/produtos.json + saida/assets/img/{SKU}.png
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

SKU_RE = re.compile(r"^[A-Z]{1,5}-\d{1,5}[A-Z]?$")
PRECO_RE = re.compile(r"^\d{1,4},\d{2}$")
CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")
TOL_DUP = 1.5      # pt: tolerância p/ considerar caractere "sombra"
TOL_LINHA = 3.0    # pt: tolerância em Y p/ agrupar caracteres numa linha
RODAPE_Y = 760     # pt: acima disso é conteúdo de cards (A4 595x842)
CABECALHO_Y = 95   # pt: abaixo disso começam os cards


# ---------------------------------------------------------------- PyMuPDF
def _load_fitz(pdf_path):
    import pymupdf as fitz
    doc = fitz.open(pdf_path)

    def paginas():
        for page in doc:
            chars = []
            raw = page.get_text("rawdict")
            for b in raw["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        for c in s["chars"]:
                            x0, y0, x1, y1 = c["bbox"]
                            chars.append((c["c"], x0, y0, x1, y1))
            imgs = [tuple(i["bbox"]) for i in page.get_image_info()]

            def render(rect, dpi=300, _p=page):
                return _p.get_pixmap(clip=fitz.Rect(*rect), dpi=dpi)

            def salvar(pix, caminho):
                pix.save(caminho)

            yield chars, imgs, render, salvar
    return paginas


def carregar(pdf_path):
    return _load_fitz(pdf_path), "pymupdf"


# ------------------------------------------------------------ texto/sombras
def dedup_chars(chars):
    """Remove caracteres-sombra: mesmo glifo, mesma posição (±TOL_DUP).
    Opera por coordenada, então 'SS' legítimo (separado ~5pt) é preservado."""
    # descarta invisíveis/controle (ex.: U+200C, U+200B) que o fitz devolve
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
    out = []
    for w in palavras:
        out.append({
            "t": "".join(c[0] for c in w),
            "x0": min(c[1] for c in w), "y0": min(c[2] for c in w),
            "x1": max(c[3] for c in w), "y1": max(c[4] for c in w),
        })
    return out


# ------------------------------------------------------------------ cards
def cards_da_pagina(palavras, imgs):
    # imagens de card: grandes (as demais são ícones/rótulos pequenos)
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
        # imagem do card: contém cx em X e termina logo acima do SKU
        cand = [f for f in fotos if f[0] - 10 <= cx <= f[2] + 10 and f[3] <= s["y0"] + 5]
        foto = max(cand, key=lambda f: f[3]) if cand else None
        if not foto:
            continue
        # fim da zona: topo da próxima imagem na mesma coluna ou o rodapé
        abaixo = [f[1] for f in fotos if f[1] > s["y1"] + 5
                  and f[0] - 10 <= cx <= f[2] + 10]
        y_fim = min(abaixo + [RODAPE_Y]) - 2
        zona = (foto[0] - 25, s["y0"] - 3, foto[2] + 25, y_fim)
        cards.append((s, foto, zona))
    return cards


def extrair_campos(s, zona, palavras):
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
    if "DESMONTADO" in txt:
        montagem = "desmontado"
    elif "MONTADO" in txt:
        montagem = "montado"
    else:
        montagem = None
    ignora = lambda t: (t == s["t"] or t == "R$" or PRECO_RE.match(t) or t.isdigit()
                        or t.upper().startswith("PCS/CX") or t in ("MONTADO", "DESMONTADO")
                        or t in ("包好", "散装"))
    pt, cn = [], []
    for w in ws:
        if ignora(w["t"]):
            continue
        (cn if CJK_RE.search(w["t"]) else pt).append(w["t"])
    return {
        "sku": s["t"], "preco_brl": preco, "pcs_cx": pcs,
        "nome_pt": " ".join(pt), "nome_cn": "".join(cn),
        "status_montagem": montagem,
    }


def debug_card(s, foto, zona, palavras, imgs):
    r = lambda t: tuple(round(v, 1) for v in t)
    print(f"\n=== DEBUG {s['t']} ===")
    print("sku bbox :", r((s["x0"], s["y0"], s["x1"], s["y1"])))
    print("foto     :", r(foto))
    print("zona     :", r(zona))
    cx = (s["x0"] + s["x1"]) / 2
    print("imagens na coluna (x sobreposto):")
    for i in imgs:
        if i[0] - 10 <= cx <= i[2] + 10:
            print("   ", r(i))
    print("palavras na faixa X do card, de y_sku até y+140 (D=dentro da zona):")
    for w in sorted(palavras, key=lambda w: (w["y0"], w["x0"])):
        mx = (w["x0"] + w["x1"]) / 2
        if zona[0] <= mx <= zona[2] and s["y0"] - 5 <= w["y0"] <= s["y0"] + 140:
            dentro = "D" if zona[1] <= w["y0"] <= zona[3] else "-"
            print(f"   {dentro} {r((w['x0'], w['y0'], w['x1'], w['y1']))} {w['t']!r}")
    print("======\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default="saida")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--debug", metavar="SKU", help="imprime a zona e as palavras desse SKU")
    a = ap.parse_args()

    out = Path(a.out)
    (out / "assets" / "img").mkdir(parents=True, exist_ok=True)
    paginas, backend = carregar(a.pdf)
    produtos = []
    for n, (chars, imgs, render, salvar) in enumerate(paginas(), 1):
        palavras = montar_palavras(dedup_chars(chars))
        for s, foto, zona in cards_da_pagina(palavras, imgs):
            if a.debug and s["t"] == a.debug:
                debug_card(s, foto, zona, palavras, imgs)
            p = extrair_campos(s, zona, palavras)
            p["pagina"] = n
            salvar(render(foto, a.dpi), str(out / "assets" / "img" / f"{p['sku']}.png"))
            produtos.append(p)
    (out / "produtos.json").write_text(
        json.dumps(produtos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{backend}] {len(produtos)} produtos -> {out/'produtos.json'}")


if __name__ == "__main__":
    main()