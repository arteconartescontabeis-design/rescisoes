"""
analisar_cct.py — Bloco 3 (seções 44-61): extração de valores por cláusula, comparação entre versões e parecer.
1) extrair_valores(dados)      — determinístico (regex + taxonomia grupo/subgrupo do Mediador); confiança ALTA = valor localizado no texto
2) comparar(anterior, atual)   — cláusulas novas / excluídas / alteradas (com diff) + variação dos valores
3) parecer_ia(...)             — opcional, via API Anthropic (ANTHROPIC_API_KEY); JSON validado contra as cláusulas
4) analisar(dados, anterior)   — orquestra e devolve o registro para cct_analises
Regra 99: se a IA falhar, a análise sai com status ANALISE_IA_NAO_CONCLUIDA e os itens determinísticos permanecem.
"""
import difflib
import json
import os
import re
import time
import unicodedata

MODELO_IA = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ----------------------------------------------------------------------------- utilidades
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def brl(txt):
    m = re.match(r"R\$\s*([\d\.]+),(\d{2})", txt)
    return float(m.group(1).replace(".", "") + "." + m.group(2)) if m else None


RE_BRL = re.compile(r"R\$\s*[\d\.]+,\d{2}")
RE_PCT = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
RE_HORAS = re.compile(r"(\d{1,3})\s*(?:\(\w+\)\s*)?horas?\s*(diárias|semanais|mensais)", re.I)
RE_DIAS = re.compile(r"(\d{1,3})\s*(?:\([^)]*\)\s*)?dias?", re.I)
RE_MIN = re.compile(r"(\d{2,3})\s*min", re.I)

# chave  → (regex no subgrupo|título, unidade principal, rótulo)
TEMAS = [
    ("PISO_SALARIAL",        r"piso salarial|salario normativo|salarios normativos", "BRL", "Piso salarial"),
    ("REAJUSTE",             r"reajuste|correc(a|o)es salariais|negociacao salarial", "%", "Reajuste salarial"),
    ("DATA_BASE",            r"data base|vigencia e data base", None, "Data-base"),
    ("AUXILIO_ALIMENTACAO",  r"auxilio alimentacao|vale alimentacao|vale refeicao|ticket|cesta basica", "BRL", "Auxílio-alimentação"),
    ("HORA_EXTRA",           r"hora extra|horas extras|adicional de hora", "%", "Horas extras"),
    ("ADICIONAL_NOTURNO",    r"adicional noturno", "%", "Adicional noturno"),
    ("BANCO_HORAS",          r"banco de horas|compensacao de jornada|compensacao de horarios", "dias", "Banco de horas / compensação"),
    ("JORNADA",              r"jornada de trabalho|duracao|jornada normal", "h", "Jornada"),
    ("INTERVALO",            r"intervalo intrajornada|intervalos para descanso", "min", "Intervalo intrajornada"),
    ("FERIADOS",             r"feriado", "BRL", "Trabalho em feriados"),
    ("CONTRIBUICAO_PATRONAL",r"patronal", "BRL", "Contribuição patronal"),
    ("CONTRIBUICAO_LABORAL", r"contribuicao negocial|contribuicao assistencial|contribuic(a|o)es sindicais|mensalidade", "%", "Contribuição negocial/assistencial (empregados)"),
    ("MULTA",                r"multa|penalidade", "BRL", "Multas"),
    ("ESTABILIDADE",         r"estabilidade|garantia de emprego", "dias", "Estabilidades"),
    ("AUXILIO_CRECHE",       r"creche|auxilio babá", "BRL", "Auxílio-creche"),
    ("SEGURO",               r"seguro de vida|seguro", "BRL", "Seguro"),
    ("QUEBRA_CAIXA",         r"quebra de caixa", "%", "Quebra de caixa"),
    ("PLR",                  r"participacao nos lucros|plr", "BRL", "PLR"),
    ("HOMOLOGACAO",          r"homologac|rescisao|desligamento", None, "Rescisão/homologação"),
]


def tema_da_clausula(c):
    alvo = norm((c.get("subgrupo") or "") + " " + (c.get("titulo") or ""))
    for chave, rx, unidade, rotulo in TEMAS:
        if re.search(rx, alvo):
            return chave, unidade, rotulo
    return None, None, None


def rotulo_anterior(texto, pos):
    """Rótulo curto antes do valor (ex.: 'Na admissão (experiência)'); vazio quando o valor está no meio de prosa."""
    ini = max(0, pos - 120)
    trecho = re.split(r"[\n;]", texto[ini:pos])[-1]
    trecho = re.sub(r"^\s*(\d+\s*[-–]|[a-z]\))\s*", "", trecho).strip(" :–-,")
    if ":" in trecho:
        trecho = trecho.rsplit(":", 1)[0].strip()
    if len(trecho) > 60 or (len(trecho.split()) > 8 and ":" not in texto[ini:pos]):
        return ""
    return trecho


# ----------------------------------------------------------------------------- 1) valores
def extrair_valores(dados):
    itens = []
    for c in dados.get("clausulas", []):
        chave, unidade, rotulo = tema_da_clausula(c)
        if not chave:
            continue
        texto = c.get("texto") or ""
        vistos = set()

        def add(desc, vtxt, vnum, un, pos):
            desc = desc or rotulo
            k = (desc, vtxt)
            if k in vistos or (desc == rotulo and (rotulo, vtxt) in vistos):
                return
            vistos.add(k)
            itens.append({"chave": chave, "tema": rotulo, "descricao": desc[:120], "valor_texto": vtxt, "valor_num": vnum, "unidade": un,
                          "clausula_ordem": c["ordem"], "clausula_numero": c.get("numero_extenso"), "clausula_titulo": c.get("titulo"),
                          "trecho": texto[max(0, pos - 60):pos + len(vtxt) + 60].replace("\n", " ").strip(), "confianca": "ALTA"})

        for m in RE_BRL.finditer(texto):
            add(rotulo_anterior(texto, m.start()) or rotulo, m.group(0), brl(m.group(0)), "BRL", m.start())
        if unidade == "%" or chave in ("REAJUSTE", "CONTRIBUICAO_LABORAL", "CONTRIBUICAO_PATRONAL", "HORA_EXTRA", "ADICIONAL_NOTURNO", "QUEBRA_CAIXA", "FERIADOS"):
            for m in RE_PCT.finditer(texto):
                add(rotulo_anterior(texto, m.start()) or rotulo, m.group(0), float(m.group(1).replace(",", ".")), "%", m.start())
        if chave == "JORNADA":
            for m in RE_HORAS.finditer(texto):
                add(f"jornada {m.group(2).lower()}", m.group(0), float(m.group(1)), "h", m.start())
        if chave == "INTERVALO":
            for m in RE_MIN.finditer(texto):
                add("intervalo mínimo", m.group(0), float(m.group(1)), "min", m.start())
        if chave in ("BANCO_HORAS", "ESTABILIDADE"):
            for m in RE_DIAS.finditer(texto):
                add(rotulo_anterior(texto, m.start()) or rotulo, m.group(0), float(m.group(1)), "dias", m.start())
        if chave == "DATA_BASE":
            m = re.search(r"data-?base[^\n]*?em\s+(\d{1,2}º?\s+de\s+\w+)", texto, re.I)
            if m:
                add("data-base", m.group(1), None, None, m.start())
    return itens


# ----------------------------------------------------------------------------- 2) comparação
def _mapa(dados):
    return {norm(c.get("titulo")): c for c in dados.get("clausulas", [])}


def comparar(anterior, atual):
    """Compara duas CCTs (mesmo par de sindicatos). Casa por título normalizado; fallback por similaridade."""
    ma, mb = _mapa(anterior), _mapa(atual)
    usados = set()
    res = {"anterior": anterior.get("metadados", {}).get("numero_registro"), "atual": atual.get("metadados", {}).get("numero_registro"),
           "novas": [], "excluidas": [], "alteradas": [], "inalteradas": 0, "valores": []}
    for kb, cb in mb.items():
        ca = ma.get(kb)
        if not ca:
            cand = difflib.get_close_matches(kb, [k for k in ma if k not in usados], n=1, cutoff=0.82)
            ca = ma.get(cand[0]) if cand else None
        if not ca:
            res["novas"].append({"ordem": cb["ordem"], "titulo": cb["titulo"], "grupo": cb.get("grupo"), "texto": cb["texto"][:600]})
            continue
        usados.add(norm(ca["titulo"]))
        ta, tb = norm(ca["texto"]), norm(cb["texto"])
        ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
        if ratio >= 0.985:
            res["inalteradas"] += 1
            continue
        la, lb = [l.strip() for l in ca["texto"].splitlines() if l.strip()], [l.strip() for l in cb["texto"].splitlines() if l.strip()]
        diff = [l for l in difflib.unified_diff(la, lb, lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        res["alteradas"].append({"ordem_atual": cb["ordem"], "ordem_anterior": ca["ordem"], "titulo": cb["titulo"], "grupo": cb.get("grupo"),
                                 "similaridade": round(ratio, 3), "diff": diff[:40]})
    for ka, ca in ma.items():
        if ka not in usados and ka not in mb:
            res["excluidas"].append({"ordem": ca["ordem"], "titulo": ca["titulo"], "grupo": ca.get("grupo"), "texto": ca["texto"][:600]})
    # valores: mesma chave+descrição normalizada
    va, vb = extrair_valores(anterior), extrair_valores(atual)
    idx = {}
    for v in va:
        idx.setdefault((v["chave"], norm(v["descricao"]), v["unidade"]), v)
    for v in vb:
        k = (v["chave"], norm(v["descricao"]), v["unidade"])
        a = idx.get(k)
        if a and a["valor_num"] is not None and v["valor_num"] is not None and a["valor_texto"] != v["valor_texto"]:
            var = (v["valor_num"] - a["valor_num"]) / a["valor_num"] * 100 if a["valor_num"] else None
            res["valores"].append({"tema": v["tema"], "descricao": v["descricao"], "anterior": a["valor_texto"], "atual": v["valor_texto"],
                                   "variacao_pct": round(var, 2) if var is not None else None, "clausula_ordem": v["clausula_ordem"]})
    return res


# ----------------------------------------------------------------------------- 3) parecer (IA opcional)
PROMPT_SISTEMA = """Você é analista de Departamento Pessoal de um escritório de contabilidade brasileiro.
Receberá as cláusulas de uma Convenção Coletiva de Trabalho (CCT) registrada no Mediador/MTE, os valores já extraídos
e, se houver, a comparação com a versão anterior. Produza um parecer objetivo para o DP.
Responda SOMENTE com JSON válido, sem markdown, no formato:
{"resumo": "3 a 6 frases", 
 "destaques": [{"tema": "...", "texto": "...", "clausulas": [ordem, ...]}],
 "providencias": [{"acao": "...", "prazo": "...", "clausulas": [ordem, ...]}],
 "alertas": ["..."],
 "pontos_incertos": ["..."]}
Regras: cite SEMPRE o número de ordem da cláusula em "clausulas"; não invente valores — use apenas os que constam no texto;
se algo não estiver claro, coloque em pontos_incertos em vez de afirmar."""


def _validar_refs(parecer, dados, valores):
    ordens = {c["ordem"]: c for c in dados.get("clausulas", [])}
    problemas = []
    for grupo in ("destaques", "providencias"):
        for item in parecer.get(grupo, []) or []:
            refs = [o for o in (item.get("clausulas") or []) if isinstance(o, int)]
            item["clausulas"] = [o for o in refs if o in ordens]
            if len(item["clausulas"]) != len(refs):
                problemas.append(f"{grupo}: referência a cláusula inexistente removida")
            texto = " ".join(ordens[o]["texto"] for o in item["clausulas"])
            nums = re.findall(r"R\$\s*[\d\.]+,\d{2}|\d{1,3}(?:[.,]\d{1,2})?\s*%", item.get("texto") or item.get("acao") or "")
            faltando = [n for n in nums if n.replace(" ", "") not in texto.replace(" ", "")]
            item["confianca"] = "ALTA" if item["clausulas"] and not faltando else "NECESSITA_VALIDACAO"
            if faltando:
                problemas.append(f"{grupo}: valor(es) {faltando} não localizados na(s) cláusula(s) citada(s)")
    parecer["validacao"] = problemas
    return parecer


def parecer_ia(dados, valores, comparacao=None, api_key=None, timeout=120):
    import requests
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "ANTHROPIC_API_KEY não configurada"
    cls = "\n\n".join(f"[{c['ordem']}] {c['grupo']} > {c['subgrupo']} > {c['titulo']}\n{c['texto'][:2500]}" for c in dados["clausulas"])
    meta = dados.get("metadados", {})
    corpo = (f"CCT {meta.get('numero_registro')} — vigência {dados.get('vigencia')} — categoria {dados.get('categoria')} — "
             f"abrangência {dados.get('abrangencia_territorial')}\nPartes: {[p['nome'] for p in dados.get('partes', [])]}\n\n"
             f"VALORES EXTRAÍDOS (determinísticos):\n{json.dumps(valores, ensure_ascii=False)[:6000]}\n\n"
             + (f"COMPARAÇÃO COM A ANTERIOR ({comparacao.get('anterior')}):\n{json.dumps({k: comparacao[k] for k in ('novas','excluidas','alteradas','valores')}, ensure_ascii=False)[:8000]}\n\n" if comparacao else "")
             + f"CLÁUSULAS:\n{cls}")
    t0 = time.time()
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": MODELO_IA, "max_tokens": 3000, "system": PROMPT_SISTEMA,
                            "messages": [{"role": "user", "content": corpo[:180000]}]})
    if r.status_code != 200:
        return None, f"API HTTP {r.status_code}: {r.text[:300]}"
    txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    txt = re.sub(r"^```json|```$", "", txt.strip(), flags=re.M).strip()
    try:
        parecer = json.loads(txt)
    except Exception as e:
        return None, f"resposta da IA não é JSON válido: {e}"
    parecer = _validar_refs(parecer, dados, valores)
    parecer["modelo"] = MODELO_IA
    parecer["duracao_ms"] = int((time.time() - t0) * 1000)
    return parecer, None


# ----------------------------------------------------------------------------- 4) orquestração
def analisar(dados, anterior=None, usar_ia=True):
    t0 = time.time()
    valores = extrair_valores(dados)
    comparacao = comparar(anterior, dados) if anterior else None
    parecer, erro = (parecer_ia(dados, valores, comparacao) if usar_ia else (None, "IA desligada"))
    return {
        "status": "CONCLUIDA" if parecer else "ANALISE_IA_NAO_CONCLUIDA",
        "erro_ia": erro, "modelo": (parecer or {}).get("modelo"),
        "valores": valores, "comparacao": comparacao,
        "resumo": (parecer or {}).get("resumo"), "destaques": (parecer or {}).get("destaques", []),
        "providencias": (parecer or {}).get("providencias", []), "alertas": (parecer or {}).get("alertas", []),
        "pontos_incertos": (parecer or {}).get("pontos_incertos", []), "validacao": (parecer or {}).get("validacao", []),
        "duracao_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    import sys
    atual = json.load(open(sys.argv[1], encoding="utf-8"))
    ant = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None
    print(json.dumps(analisar(atual, ant, usar_ia=bool(os.environ.get("ANTHROPIC_API_KEY"))), ensure_ascii=False, indent=2))
