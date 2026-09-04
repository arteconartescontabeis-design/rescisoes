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
MODELO_GITHUB = os.environ.get("GITHUB_MODEL", "openai/gpt-4o")   # GitHub Models (grátis via GITHUB_TOKEN + permissions: models: read)
LIMITE_CHARS_GITHUB = 22000   # nível gratuito tem janela menor: compactamos o material enviado

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
PROMPT_SISTEMA = """Você é analista de Departamento Pessoal de um escritório de contabilidade brasileiro e vai produzir um parecer
sobre uma Convenção Coletiva de Trabalho (CCT) registrada no Mediador/MTE, para uso interno do DP.

REGRA ABSOLUTA: o parecer só pode conter o que está ESCRITO nas cláusulas fornecidas. Nada de interpretação, inferência,
conhecimento externo, legislação não citada no texto, estimativas ou suposições. Se algo não estiver no texto, não mencione.

Para garantir isso, CADA destaque, providência e alerta deve trazer:
- "clausulas": números de ordem [n] das cláusulas de origem (obrigatório);
- "trecho": cópia LITERAL (caractere por caractere, sem reticências, sem resumir) de um trecho contínuo de 30 a 300
  caracteres da cláusula citada, que sustenta a afirmação. Itens cujo trecho não for encontrado no texto serão descartados.
Valores, percentuais, datas e prazos devem ser reproduzidos exatamente como aparecem no texto (mesma grafia).

Seja DETALHADO: percorra todos os grupos de cláusulas (salários, gratificações/auxílios, contrato, relações de trabalho,
jornada, férias/licenças, saúde/segurança, relações sindicais, disposições gerais) e registre um destaque para cada
obrigação, valor, prazo ou condição relevante para o DP. As providências são apenas as ações que decorrem de obrigação
EXPRESSA no texto (ex.: "recolher a contribuição até dia X" quando a cláusula fixa a data).

Responda SOMENTE com JSON válido, sem markdown:
{"resumo": "parágrafo só com fatos presentes no texto, com os números exatamente como no texto",
 "destaques": [{"tema": "...", "texto": "...", "clausulas": [n], "trecho": "..."}],
 "providencias": [{"acao": "...", "prazo": "...", "clausulas": [n], "trecho": "..."}],
 "alertas": [{"texto": "...", "clausulas": [n], "trecho": "..."}],
 "pontos_incertos": ["o que o texto deixa em aberto — sem completar com suposições"]}"""


def _norm_txt(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def _numeros(t):
    return re.findall(r"R\$\s*[\d\.]+,\d{2}|\d{1,3}(?:[.,]\d{1,2})?\s*%|\d{1,2}/\d{1,2}/\d{2,4}|\b\d{1,2}º?\s+de\s+[a-zç]+\b", t or "", re.I)


def _validar_refs(parecer, dados, valores):
    """Regra absoluta: só sobrevive o que está no texto. Cada item precisa de cláusula existente + trecho literal
    localizado nela + todos os números/percentuais/datas do item presentes nas cláusulas citadas. O resto é DESCARTADO."""
    ordens = {c["ordem"]: c for c in dados.get("clausulas", [])}
    texto_total = _norm_txt(" ".join(c["texto"] for c in ordens.values()))
    descartados = []

    def valida(item, grupo):
        refs = [o for o in (item.get("clausulas") or []) if isinstance(o, int) and o in ordens]
        if not refs:
            return "sem cláusula de origem válida"
        item["clausulas"] = refs
        base = _norm_txt(" ".join(ordens[o]["texto"] + " " + (ordens[o].get("titulo") or "") for o in refs))
        trecho = _norm_txt(item.get("trecho") or "")
        if len(trecho) < 20:
            return "sem trecho literal"
        if trecho not in base:
            return "trecho não encontrado literalmente na(s) cláusula(s) citada(s)"
        corpo = (item.get("texto") or "") + " " + (item.get("acao") or "") + " " + (item.get("prazo") or "")
        faltando = [n for n in _numeros(corpo) if _norm_txt(n).replace(" ", "") not in base.replace(" ", "")]
        if faltando:
            return f"valor(es) {faltando} não constam na(s) cláusula(s) citada(s)"
        item["confianca"] = "ALTA"
        return None

    for grupo in ("destaques", "providencias", "alertas"):
        mantidos = []
        for item in parecer.get(grupo, []) or []:
            if isinstance(item, str):
                item = {"texto": item, "clausulas": [], "trecho": ""}
            motivo = valida(item, grupo)
            if motivo:
                descartados.append({"grupo": grupo, "item": (item.get("texto") or item.get("acao") or "")[:160], "motivo": motivo})
            else:
                mantidos.append(item)
        parecer[grupo] = mantidos
    # resumo: frase com número que não existe no texto da CCT é removida
    frases, resumo_ok = re.split(r"(?<=[.;])\s+", parecer.get("resumo") or ""), []
    for f in frases:
        nums = _numeros(f)
        if all(_norm_txt(n).replace(" ", "") in texto_total.replace(" ", "") for n in nums):
            resumo_ok.append(f)
        else:
            descartados.append({"grupo": "resumo", "item": f[:160], "motivo": "número não localizado no texto da CCT"})
    parecer["resumo"] = " ".join(resumo_ok).strip()
    parecer["pontos_incertos"] = [p for p in (parecer.get("pontos_incertos") or []) if isinstance(p, str)][:10]
    parecer["descartados"] = descartados
    parecer["validacao"] = [f"{d['grupo']}: {d['motivo']}" for d in descartados]
    return parecer


def _material(dados, valores, comparacao, limite_chars=None):
    """Monta o texto enviado ao modelo; com limite, encurta as cláusulas priorizando as com valores extraídos."""
    meta = dados.get("metadados", {})
    cab = (f"CCT {meta.get('numero_registro')} — vigência {dados.get('vigencia')} — categoria {dados.get('categoria')} — "
           f"abrangência {dados.get('abrangencia_territorial')}\nPartes: {[p['nome'] for p in dados.get('partes', [])]}\n\n"
           f"VALORES EXTRAÍDOS (determinísticos):\n{json.dumps(valores, ensure_ascii=False)[:6000 if not limite_chars else 3000]}\n\n")
    if comparacao:
        comp = {k: comparacao[k] for k in ("novas", "excluidas", "alteradas", "valores")}
        cab += f"COMPARAÇÃO COM A ANTERIOR ({comparacao.get('anterior')}):\n{json.dumps(comp, ensure_ascii=False)[:8000 if not limite_chars else 2500]}\n\n"
    com_valor = {v["clausula_ordem"] for v in valores}
    cls = dados["clausulas"]
    if not limite_chars:
        corpo = "\n\n".join(f"[{c['ordem']}] {c['grupo']} > {c['subgrupo']} > {c['titulo']}\n{c['texto'][:2500]}" for c in cls)
        return cab + "CLÁUSULAS:\n" + corpo
    orcamento = max(4000, limite_chars - len(cab))
    por_clausula = max(160, orcamento // max(1, len(cls)))
    partes = []
    for c in cls:
        lim = por_clausula * 2 if c["ordem"] in com_valor else por_clausula
        t = c["texto"] if len(c["texto"]) <= lim else c["texto"][:lim] + " (...)"
        partes.append(f"[{c['ordem']}] {c['grupo']} > {c['titulo']}\n{t}")
    return (cab + "CLÁUSULAS (resumidas por limite de tamanho):\n" + "\n\n".join(partes))[:limite_chars]


def _parse_json(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    i, j = txt.find("{"), txt.rfind("}")
    return json.loads(txt[i:j + 1] if i >= 0 and j > i else txt)


def parecer_ia(dados, valores, comparacao=None, api_key=None, timeout=120):
    """Anthropic (ANTHROPIC_API_KEY) ou, na ausência, GitHub Models grátis (GITHUB_TOKEN). Retorna (parecer, erro)."""
    import requests
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    gh = os.environ.get("GITHUB_TOKEN")
    t0 = time.time()
    if key:
        corpo = _material(dados, valores, comparacao)
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": MODELO_IA, "max_tokens": 6000, "system": PROMPT_SISTEMA,
                                "messages": [{"role": "user", "content": corpo[:180000]}]})
        if r.status_code != 200:
            return None, f"Anthropic HTTP {r.status_code}: {r.text[:300]}"
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        modelo = MODELO_IA
    elif gh:
        corpo = _material(dados, valores, comparacao, LIMITE_CHARS_GITHUB)
        r = requests.post("https://models.github.ai/inference/chat/completions", timeout=timeout,
                          headers={"Authorization": f"Bearer {gh}", "Content-Type": "application/json", "Accept": "application/vnd.github+json"},
                          json={"model": MODELO_GITHUB, "max_tokens": 2500, "temperature": 0.2,
                                "messages": [{"role": "system", "content": PROMPT_SISTEMA}, {"role": "user", "content": corpo}]})
        if r.status_code != 200:
            return None, f"GitHub Models HTTP {r.status_code}: {r.text[:300]}"
        txt = r.json()["choices"][0]["message"]["content"]
        modelo = "github:" + MODELO_GITHUB
    else:
        return None, "nenhum provedor de IA configurado (ANTHROPIC_API_KEY ou GITHUB_TOKEN)"
    try:
        parecer = _parse_json(txt)
    except Exception as e:
        return None, f"resposta da IA não é JSON válido: {e}"
    parecer = _validar_refs(parecer, dados, valores)
    parecer["modelo"] = modelo
    parecer["duracao_ms"] = int((time.time() - t0) * 1000)
    return parecer, None


# ----------------------------------------------------------------------------- 4) orquestração
def analisar(dados, anterior=None, usar_ia=True):
    t0 = time.time()
    valores = extrair_valores(dados)
    comparacao = comparar(anterior, dados) if anterior else None
    parecer, erro = (parecer_ia(dados, valores, comparacao) if usar_ia else (None, "IA desligada"))
    if parecer is None and usar_ia:
        erro = erro or "IA não respondeu"
    return {
        "status": "CONCLUIDA" if parecer else "ANALISE_IA_NAO_CONCLUIDA",
        "erro_ia": erro, "modelo": (parecer or {}).get("modelo"),
        "valores": valores, "comparacao": comparacao,
        "resumo": (parecer or {}).get("resumo"), "destaques": (parecer or {}).get("destaques", []),
        "providencias": (parecer or {}).get("providencias", []), "alertas": (parecer or {}).get("alertas", []),
        "pontos_incertos": (parecer or {}).get("pontos_incertos", []), "validacao": (parecer or {}).get("validacao", []),
        "descartados": (parecer or {}).get("descartados", []),
        "duracao_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    import sys
    atual = json.load(open(sys.argv[1], encoding="utf-8"))
    ant = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None
    print(json.dumps(analisar(atual, ant, usar_ia=bool(os.environ.get("ANTHROPIC_API_KEY"))), ensure_ascii=False, indent=2))
