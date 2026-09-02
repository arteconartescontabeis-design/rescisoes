"""
extrair_cct.py — lê o "ICRegistrado*.doc" do Mediador (na verdade HTML) e devolve JSON estruturado.
Uso: python extrair_cct.py ICRegistrado423593973.doc  > cct.json
Requer: pip install beautifulsoup4 lxml
"""
import sys, re, json, hashlib
from bs4 import BeautifulSoup

def limpar(s):
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\s*\n\s*", "\n", s or "")).strip()

def extrair(caminho):
    raw = open(caminho, "rb").read()
    html = raw.decode("iso-8859-1", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]): t.decompose()
    texto = limpar(soup.get_text("\n"))

    def campo(rotulo):
        m = re.search(rotulo + r":\s*\n?\s*([^\n]+)", texto, re.I)
        return m.group(1).strip() if m else None

    meta = {
        "titulo": limpar(soup.title.get_text()) if soup.title else None,
        "denominacao": (re.search(r"(Conven[çc][ãa]o Coletiva De Trabalho \d{4}/\d{4}|Acordo Coletivo[^\n]*|Termo Aditivo[^\n]*)", texto, re.I) or [None, None])[1],
        "numero_registro": campo(r"N[ÚU]MERO DE REGISTRO NO MTE"),
        "data_registro": campo(r"DATA DE REGISTRO NO MTE"),
        "numero_solicitacao": campo(r"N[ÚU]MERO DA SOLICITA[ÇC][ÃA]O"),
        "numero_processo": campo(r"N[ÚU]MERO DO PROCESSO"),
        "data_protocolo": campo(r"DATA DO PROTOCOLO"),
    }
    partes = [{"nome": n.strip(), "cnpj": c, "representante": r.strip()}
              for n, c, r in re.findall(r"\n([^\n;]+?), CNPJ n\. ([\d./-]+), neste ato representado\(a\) por seu[^\n]*\n?[^\n]*?Sr\(a\)\. ([^;\n]+);", texto)]

    clausulas, anexos, grupo, subgrupo = [], [], None, None
    for el in soup.find_all(["label", "p"]):
        cls = el.get("class") or []
        if "textogrupo" in cls:
            g = limpar(el.get_text())
            if g.upper().startswith("ANEXO"): anexos.append(g); continue
            grupo = g; subgrupo = None
        elif "textosubgrupo" in cls: subgrupo = limpar(el.get_text())
        elif "tituloClausula" in cls:
            tit = limpar(el.get_text())
            desc = el.find_next(class_="descricaoClausula")
            m = re.match(r"CL[ÁA]USULA\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ ]+?)\s*[-–]\s*(.+)", tit, re.I)
            if not m and not tit.upper().startswith("CL"):  # anexos (ex.: ANEXO I - ATA ASSEMBLEIA)
                continue
            clausulas.append({
                "ordem": len(clausulas) + 1,
                "numero_extenso": m.group(1).strip() if m else None,
                "titulo": m.group(2).strip() if m else tit,
                "grupo": grupo, "subgrupo": subgrupo,
                "texto": limpar(desc.get_text("\n")) if desc else "",
            })

    vig = re.search(r"per[íi]odo de (\d{1,2}º? de \w+ de \d{4}) a (\d{1,2}º? de \w+ de \d{4})", texto, re.I)
    db = re.search(r"data-base da categoria em (\d{1,2}º? de \w+)", texto, re.I)
    plano = re.sub(r"\s+", " ", texto)
    abr = re.search(r"abrang[êe]ncia territorial em (.+?)\.(?:\s|$)", plano, re.I)
    cat = re.search(r"categoria\(s\) (.+?)\s*,\s*com abrang", plano, re.I)
    return {
        "arquivo": caminho.split("/")[-1],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "metadados": meta,
        "partes": partes,
        "vigencia": {"inicio": vig.group(1), "fim": vig.group(2)} if vig else None,
        "data_base": db.group(1) if db else None,
        "categoria": limpar(cat.group(1)) if cat else None,
        "abrangencia_territorial": abr.group(1).strip() if abr else None,
        "anexos": anexos,
        "total_clausulas": len(clausulas),
        "clausulas": clausulas,
    }

if __name__ == "__main__":
    print(json.dumps(extrair(sys.argv[1]), ensure_ascii=False, indent=2))
