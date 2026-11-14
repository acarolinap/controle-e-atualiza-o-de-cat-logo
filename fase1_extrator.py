"""Fase 1 - Extrator do catálogo Lukton (SKU como âncora)."""
import argparse
import json
import re
import unicodedata
import os
from PIL import Image
import pytesseract
from pathlib import Path

SKU_RE = re.compile(r"^([A-Z]{1,5}-?\d{1,6}[A-Z]?|[A-Z0-9]+-[A-Z0-9\-]*[A-UW-Z0-9])$")
PRECO_RE = re.compile(r"^\d{1,4},\d{2}$")
CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")
TOL_DUP = 1.5      
TOL_LINHA = 3.0    
RODAPE_Y = 760     
CABECALHO_Y = 95   

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

def dedup_chars(chars):
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
            if c[1] - atual[-1][3] > 0.18 * (c[4] - c[2]):  
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

def cards_da_pagina(palavras, imgs):
    """Identifica a posição de cada card na página utilizando a foto e o SKU como âncoras para definir a zona de leitura."""
    fotos = []
    for i in imgs:
        w, h = i[2] - i[0], i[3] - i[1]
        if 40 < w < 250 and 40 < h < 250 and i[1] > CABECALHO_Y - 20:
            if not any(all(abs(a - b) < 1 for a, b in zip(i, f)) for f in fotos):
                fotos.append(i)
                
    skus = [w for w in palavras if SKU_RE.match(w["t"]) and w["y0"] > CABECALHO_Y]
    cards = []
    for s in skus:
        cx = (s["x0"] + s["x1"]) / 2
        cand = [f for f in fotos if f[0] - 25 <= cx <= f[2] + 25 and f[1] <= s["y1"] + 30]
        foto = max(cand, key=lambda f: f[3]) if cand else None
        
        if foto:
            x_min = min(foto[0], s["x0"]) - 15
            x_max = max(foto[2], s["x1"]) + 15
            y_min = foto[1] - 10
            y_max = max(foto[3], s["y1"]) + 10
        else:
            x_min = s["x0"] - 70
            x_max = s["x1"] + 70
            y_min = s["y0"] - 130
            y_max = s["y1"] + 15
            
        zona = (x_min, y_min, x_max, y_max)
        cards.append((s, foto, zona))
        
    return cards

def extrair_campos(s, zona, palavras, caminho_img=None):
    """Extrai campos do texto vetorizado e complementa com OCR na imagem do produto se necessário."""
    ws = [w for w in palavras
          if zona[0] <= (w["x0"] + w["x1"]) / 2 <= zona[2]
          and zona[1] <= w["y0"] <= zona[3]]
    ws.sort(key=lambda w: (round(w["y0"] / TOL_LINHA), w["x0"]))
    
    txt_vetor = " ".join(w["t"] for w in ws)
    
    txt_ocr = ""
    if caminho_img and os.path.exists(caminho_img):
        try:
            img = Image.open(caminho_img)
            txt_ocr = pytesseract.image_to_string(img, config='--psm 6')
        except Exception:
            pass
            
    txt_completo = txt_vetor + " " + txt_ocr

    preco = None
    match_preco = re.search(r'(?:R\$)?\s*(\d+[\.,]\d{2})', txt_completo)
    if match_preco:
        try:
            preco = float(match_preco.group(1).replace(",", "."))
        except ValueError:
            pass

    pcs = None
    match_pcs = re.search(r'(\d+)\s*(?:PCS|PC|CX|个|箱)', txt_completo, re.IGNORECASE)
    if match_pcs:
        try:
            pcs = int(match_pcs.group(1))
        except ValueError:
            pass

    if "DESMONTADO" in txt_completo.upper() or "散装" in txt_completo:
        montagem = "desmontado"
    elif "MONTADO" in txt_completo.upper() or "包好" in txt_completo:
        montagem = "montado"
    else:
        montagem = None

    def ignora(t):
        if t == s["t"] or t in ("R$", "R$"):
            return True
        if re.match(r'^(?:R\$)?\d+[\.,]\d{2}$', t):
            return True
        if t.upper() in ("PCS/CX", "PCS", "CX", "MONTADO", "DESMONTADO", "包好", "散装"):
            return True
        return False

    pt, cn = [], []
    for w in ws:
        if ignora(w["t"]):
            continue
        if CJK_RE.search(w["t"]):
            cn.append(w["t"])
        else:
            pt.append(w["t"])

    return {
        "sku": s["t"], 
        "preco_brl": preco, 
        "pcs_cx": pcs,
        "nome_pt": " ".join(pt).strip(), 
        "nome_cn": "".join(cn).strip(),
        "status_montagem": montagem,
    }

def debug_card(s, foto, zona, palavras, imgs):
    r = lambda t: tuple(round(v, 1) for v in t)
    print(f"\n=== DEBUG {s['t']} ===")
    print("sku bbox :", r((s["x0"], s["y0"], s["x1"], s["y1"])))
    print("foto     :", r(foto) if foto else "Nenhuma")
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
        
            if foto:
                salvar(render(foto, a.dpi), str(out / "assets" / "img" / f"{p['sku']}.png"))
                
            produtos.append(p)
    (out / "produtos.json").write_text(
        json.dumps(produtos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{backend}] {len(produtos)} produtos -> {out/'produtos.json'}")

if __name__ == "__main__":
    main()