import re
import fitz  # PyMuPDF
from typing import List
import pandas as pd
import argparse
import json
import os

from flask import Flask, request, jsonify

# ====== Regras (iguais às do seu script) ======
PATTERNS = [
    r"(?i)^\d{2,3}A\s*[-/]\s*\d{1,2}kA\s*[-/]\s*\d{1,2}[HKT]$",
    r"(?i)^\d{2,3}\s*-\s*\d{1,2}kA\d{1,2}[HKT]$",
    r"^\d{2,3}\s*[- ]\s*\d{2,4}$",
    r"(?i)^[A-Z]{2,4}-\d+\s*\(\s*\d+/\d+\s*(?:\"|''|\u2033)?\s*\)$",
    r"(?i)^(?:AM|BM|CM)-\d+\s*\(\s*\d+/\d+\s*(?:\"|''|\u2033)?\s*\)\s+ABN-\d+\(\d+\)$",
    r"(?i)^ABCN-\s*\d+(?:/\d+)?\s*(?:CA)?\s*\(\s*\d+(?:/\d+)?\s*(?:CA)?\s*\)$",
    r"(?i)^ABN(?:-\s*\d+)?\s*(?:CA)?\s*\(\s*\d+(?:/\d+)?\s*(?:CA)?\s*\)(?:\s*\(\s*\d+\s*\))?$",
    r"^ABN-\d+$",
    r"(?i)^AN-?\s*\d+(?:/\d+)?\s*(?:CA{1,2})?\s*\(\s*\d+(?:/\d+)?\s*(?:CA{1,2})?\s*\)$",
    r"^AN\d+\(\d+\)$",
    r"(?i)^BN-\s*\d+\s*CA{1,2}\s*\(\s*\d+\s*CA{1,2}\s*\)$",
    r"(?i)^B-\s*\d+\s*CA{1,2}$",
    r"^B\d\(\d+\)$",
    r"^B-\([A-Z0-9]+\)$",
    r"^CE\d(?:\.\d+)?(?:\(\d+\))?$",
    r"^(?:CE\d(?:\(\d+\))?)(?:[.\-]CE\d(?:\(\d+\))?)+$",
    r"^CE(?:BE|BS|J|M)\d(?:\(\d+\))?$",
    r"^(?:CEN\d|CM\d)(?:\(\d+\))?(?:[.\-](?:CEN\d|CM\d)(?:\(\d+\))?)+$",
    r"^CM\d(?:\(\d+\))?$",
    r"^CM2\($",
    r"^CN(?:-\s*\d+)?\s*\(\s*\d+\s*\)$",
    r"^I\d\(\d+\)$",
    r"^M\d(?:\.\d+)?(?:\(\d+\))?$",
    r"^(?:[A-Z]{1,3}(?:\d+(?:\.\d+)?)?(?:\(\d+\))?)(?:[ .-]{1,2}[A-Z]{1,3}(?:\d+(?:\.\d+)?)?(?:\(\d+\))?)+$",
    r"^S[A-Z0-9]+(?:\([A-Z0-9]+\))?$",
    r"^S(?:[A-Z0-9]+(?:\([A-Z0-9]+\))?)+(?:[ .-]S(?:[A-Z0-9]+(?:\([A-Z0-9]+\))?)+)*$",
    r"^T(?:E|\d)(?:\(\d+\))?$",
    r"^U\d(?:\.\d+)?(?:\(\d+\))?$",
    r"^N(?:\d+(?:\.\d+)?)?(?:\(\d+\))?$"
]
COMPILED = [re.compile(p) for p in PATTERNS]


def looks_like_code(text: str) -> bool:
    """Verifica se o texto parece um código válido."""
    t = (text or "").strip()
    if not t:
        return False
    return any(rx.search(t) for rx in COMPILED)


def to_rgb(color_value):
    """Converte o valor de cor do span para (R, G, B)."""
    if isinstance(color_value, int):
        r = (color_value >> 16) & 255
        g = (color_value >> 8) & 255
        b = color_value & 255
        return (r, g, b)
    if isinstance(color_value, (list, tuple)) and len(color_value) >= 3:
        r, g, b = color_value[:3]
        if max(r, g, b) <= 1.0:  # caso venha normalizado (0–1)
            return (int(r * 255), int(g * 255), int(b * 255))
        return (int(r), int(g), int(b))
    return (0, 0, 0)


def is_green(rgb, g_min=110, delta=20):
    """Verifica se a cor RGB é verde."""
    r, g, b = rgb
    return (g > g_min) and (g > r + delta) and (g > b + delta)


def is_blue(rgb, b_min=80, delta=10):
    """Verifica se a cor RGB é azul (para pegar os P1..P50)."""
    r, g, b = rgb
    return (b > b_min) and (b > r + delta) and (b > g + delta)


def _extract_codes_from_doc(doc) -> pd.DataFrame:
    """
    Lógica comum de extração a partir de um objeto doc do PyMuPDF.
    """
    rows = []
    # regex para P1..P50 (P + número de 1 a 50)
    ponto_regex = re.compile(r"^P(?:[1-9]|[1-4]\d|50)$", re.IGNORECASE)

    for pno, page in enumerate(doc, start=1):
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    rgb = to_rgb(span.get("color", 0))
                    text = (span.get("text", "") or "").strip()
                    if not text:
                        continue

                    # Guardar todas as infos do span
                    span_raw = dict(span)
                    span_raw["rgb"] = rgb
                    span_raw["page"] = pno

                    # ================== VERDE: estruturas ==================
                    if is_green(rgb):
                        tokens_base = re.findall(r"[A-Z0-9()/.\\\"''\u2033-]+", text)

                        tokens_split: List[str] = []
                        for t in tokens_base:
                            sub_tokens = re.split(r"(?<=\))\s*(?=[A-Z])", t)
                            tokens_split.extend(sub_tokens)

                        all_possible_tokens = set(tokens_base + tokens_split)

                        raw_candidates: List[str] = []
                        if looks_like_code(text):
                            raw_candidates.append(text)

                        for tok in all_possible_tokens:
                            if not tok:
                                continue

                            base_tok = re.sub(r"\([\s\d/\"'CA]+\)$", "", tok).strip()

                            if tok not in raw_candidates and looks_like_code(tok):
                                raw_candidates.append(tok)

                            if base_tok and base_tok != tok and base_tok not in raw_candidates and looks_like_code(base_tok):
                                raw_candidates.append(base_tok)

                        bases_found = set()
                        for cand in raw_candidates:
                            base_match = re.sub(r"\([\s\d/\"'CA]+\)$", "", cand).strip()
                            if base_match == cand and looks_like_code(cand):
                                bases_found.add(cand)

                        final_candidates = []
                        for cand in raw_candidates:
                            base_tok = re.sub(r"\([\s\d/\"'CA]+\)$", "", cand).strip()

                            if base_tok != cand:
                                if base_tok in bases_found:
                                    continue
                                else:
                                    final_candidates.append(cand)
                            else:
                                final_candidates.append(cand)

                        candidates = sorted(list(set(final_candidates)))

                        for tok in candidates:
                            rows.append({
                                "page": pno,
                                "code": tok,
                                "span_text": text,
                                "bbox": span.get("bbox", None),
                                "rgb": rgb,
                                "method": "vector",
                                "span_raw": span_raw,
                                "span_type": "code"  # estrutura verde
                            })

                        continue  # já tratou o span verde, vai para o próximo

                    # ================== AZUL: P1..P50 ==================
                    if is_blue(rgb) and ponto_regex.match(text):
                        tok = text.upper()  # padronizar como P1, P2...
                        rows.append({
                            "page": pno,
                            "code": tok,
                            "span_text": text,
                            "bbox": span.get("bbox", None),
                            "rgb": rgb,
                            "method": "vector",
                            "span_raw": span_raw,
                            "span_type": "point"  # ponto ordenado azul
                        })

    # Deduplicação (page + code + bbox arredondado)
    uniq, seen = [], set()
    for r in rows:
        bbox = r["bbox"] or (0, 0, 0, 0)
        key = (r["page"], r["code"], tuple(round(float(x), 1) for x in bbox))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    return pd.DataFrame(uniq)


def extract_codes_from_path(pdf_path: str) -> pd.DataFrame:
    """
    Versão para usar com caminho em disco (CLI).
    """
    doc = fitz.open(pdf_path)
    try:
        return _extract_codes_from_doc(doc)
    finally:
        doc.close()


def extract_codes_from_bytes(pdf_bytes: bytes) -> pd.DataFrame:
    """
    Versão para usar com bytes (upload do n8n/Render).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return _extract_codes_from_doc(doc)
    finally:
        doc.close()


# ================== MODO LINHA DE COMANDO (opcional, para testes) ==================

def main():
    parser = argparse.ArgumentParser(
        description="Extrai spans VERDES (estruturas) e AZUIS (P1..P50) de um PDF."
    )
    parser.add_argument("pdf", help="Caminho do arquivo PDF de entrada")
    parser.add_argument(
        "--csv",
        help="Caminho para salvar o resultado em CSV (opcional)",
        default=None
    )
    parser.add_argument(
        "--json",
        help="Caminho para salvar o resultado em JSON (opcional)",
        default=None
    )

    args = parser.parse_args()

    pdf_path = args.pdf

    if not os.path.isfile(pdf_path):
        print(f"Arquivo não encontrado: {pdf_path}")
        return

    print(f"Lendo PDF: {pdf_path}")
    df = extract_codes_from_path(pdf_path)

    if df.empty:
        print("Nenhum span verde/azul foi encontrado.")
        return

    # Mostra um resumo no console
    print(f"\nTotal de spans encontrados: {len(df)}\n")
    print(df[["page", "code", "span_text", "bbox", "rgb"]].to_string(index=False))

    # Salvar em CSV
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\nResultado salvo em CSV: {args.csv}")

    # Salvar em JSON (com todos os campos, inclusive span_raw)
    if args.json:
        data = df.to_dict(orient="records")
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Resultado salvo em JSON: {args.json}")


# ================== FLASK APP PARA O N8N/RENDER ==================

app = Flask(__name__)


@app.post("/extract")
def extract_endpoint():
    """
    Espera um POST multipart/form-data com:
      - campo 'file': PDF vindo do n8n

    Retorna JSON com:
      - codes: lista única de códigos de estrutura (VERDES)
      - points: lista única de pontos P1..P50 (AZUIS)
      - rows: lista completa de spans encontrados
    """
    if "file" not in request.files:
        return jsonify({"error": "campo 'file' não encontrado no upload"}), 400

    file_storage = request.files["file"]
    pdf_bytes = file_storage.read()

    if not pdf_bytes:
        return jsonify({"error": "arquivo vazio"}), 400

    df = extract_codes_from_bytes(pdf_bytes)

    if df.empty:
        return jsonify({
            "codes": [],
            "points": [],
            "rows": []
        })

    # Estruturas VERDES
    codes = (
        df[df["span_type"] == "code"]["code"]
        .dropna()
        .map(str)
        .str.strip()
        .unique()
        .tolist()
    )
    codes = sorted(set(codes))

    # Pontos AZUIS P1..P50
    points = (
        df[df["span_type"] == "point"]["code"]
        .dropna()
        .map(str.upper)
        .str.strip()
        .unique()
        .tolist()
    )

    def point_key(p):
        try:
            return int(p[1:])
        except Exception:
            return 9999

    points = sorted(points, key=point_key)

    rows = df.to_dict(orient="records")

    return jsonify({
        "codes": codes,
        "points": points,
        "rows": rows,
    })


if __name__ == "__main__":
    # Rodando localmente: python app.py arquivo.pdf  (modo CLI)
    # ou: python app.py  (subindo o servidor Flask na porta 8000)
    import sys

    # Se for chamado com argumentos, usa o modo CLI
    if len(sys.argv) > 1 and sys.argv[1].endswith(".pdf"):
        main()
    else:
        port = int(os.getenv("PORT", "8000"))
        app.run(host="0.0.0.0", port=port)
