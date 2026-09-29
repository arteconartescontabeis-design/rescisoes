"""
analisar_cct.py — Bloco 3 (seções 44-61): extração de valores por cláusula, comparação entre versões e parecer.
1) extrair_valores(dados)      — determinístico (regex + taxonomia grupo/subgrupo do Mediador); confiança ALTA = valor localizado no texto
2) comparar(anterior, atual)   — cláusulas novas / excluídas / alteradas (com diff) + variação dos valores
3) parecer_ia(...)             — opcional, via IA Central do Portal Artecon (IA_GATEWAY_TOKEN); JSON validado contra as cláusulas
4) analisar(dados, anterior)   — orquestra e devolve o registro para cct_analises
Regra 99: se a IA falhar, a análise sai com status ANALISE_IA_NAO_CONCLUIDA e os itens determinísticos permanecem.

v0.21.1 (29/09/2026): a resposta da IA vinha CORTADA pelo limite de saída (6.000 tokens ≈ 18 mil caracteres; erro
"resposta da IA não é JSON válido: Expecting ',' delimiter … char 17936") e o parecer inteiro — já pago — era descartado.
Correções: (a) limite de saída 16.000 tokens (IA_MAX_TOKENS); (b) leitura tolerante: se ainda assim a resposta vier
cortada (stop_reason max_tokens) ou com JSON quebrado, aproveita-se a maior parte válida (itens completos) e fecha-se
o JSON, marcando "parecer_cortado" na validação; (c) o prompt pede trechos de 30 a 160 caracteres (antes 300) para
caber mais itens no mesmo limite.
"""
import difflib
import json
import os
import re
import time
import unicodedata

MODELO_IA = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
IA_MAX_TOKENS = int(os.environ.get("IA_MAX_TOKENS", "16000"))   # v0.21.1 (antes 6000 — cortava o parecer)
# v0.15.2 (09/09/2026): GitHub Models retirado — o serviço foi desativado pelo GitHub (HTTP 410 "retirement brownout").
# v0.20.0: parecer por IA pela IA Central (ia-gateway do Portal Artecon) com o token do CCT (IA_GATEWAY_TOKEN).
# O gateway recebe e devolve exatamente o formato da API da Anthropic; ele controla limites e registra o consumo.
IA_GATEWAY_URL = os.environ.get("IA_GATEWAY_URL", "https://fbxelwhdiisfmnwrerbl.supabase.co/functions/v1/ia-gateway")

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
- "trecho": cópia LITERAL (caractere por caractere, sem reticências, sem resumir) de um trecho contínuo de 30 a 160
  caracteres da cláusula citada, que sustenta a afirmação — escolha o trecho MAIS CURTO que contenha o valor/prazo citado.
  Itens cujo trecho não for encontrado no texto serão descartados.
Valores, percentuais, datas e prazos devem ser reproduzidos exatamente como aparecem no texto (mesma grafia).

Seja DETALHADO, mas OBJETIVO: percorra todos os grupos de cláusulas (salários, gratificações/auxílios, contrato, relações de
trabalho, jornada, férias/licenças, saúde/segurança, relações sindicais, disposições gerais) e registre um destaque para cada
obrigação, valor, prazo ou condição relevante para o DP, com o campo "texto" em uma ou duas frases. As providências são
apenas as ações que decorrem de obrigação EXPRESSA no texto (ex.: "recolher a contribuição até dia X" quando a cláusula fixa a data).

Além disso, em "comentarios" você PODE registrar observações do analista (interpretação, orientação prática, atenção do DP),
sempre ligadas a uma cláusula. Esses comentários são apresentados separadamente, rotulados como COMENTÁRIO e com aviso
de que podem conter erro — por isso, mesmo neles, não invente números: qualquer valor citado deve estar no texto.

Responda SOMENTE com JSON válido, sem markdown, compacto (sem quebras de linha desnecessárias):
{"resumo": "parágrafo só com fatos presentes no texto, com os números exatamente como no texto",
 "destaques": [{"tema": "...", "texto": "...", "clausulas": [n], "trecho": "..."}],
 "providencias": [{"acao": "...", "prazo": "...", "clausulas": [n], "trecho": "..."}],
 "alertas": [{"texto": "...", "clausulas": [n], "trecho": "..."}],
 "comentarios": [{"texto": "observação do analista", "clausulas": [n]}],
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
    descartados = list(parecer.get("descartados") or [])   # v0.21.1: preserva o aviso de parecer cortado

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
            if not isinstance(item, dict):
                continue
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
    # comentários: interpretação permitida, mas cláusula tem de existir e números têm de estar no texto da CCT
    coment = []
    for c in parecer.get("comentarios", []) or []:
        if isinstance(c, str):
            c = {"texto": c, "clausulas": []}
        if not isinstance(c, dict):
            continue
        refs = [o for o in (c.get("clausulas") or []) if isinstance(o, int) and o in ordens]
        nums = _numeros(c.get("texto") or "")
        if not refs:
            descartados.append({"grupo": "comentarios", "item": (c.get("texto") or "")[:160], "motivo": "comentário sem cláusula de referência"}); continue
        if any(_norm_txt(n).replace(" ", "") not in texto_total.replace(" ", "") for n in nums):
            descartados.append({"grupo": "comentarios", "item": (c.get("texto") or "")[:160], "motivo": "comentário cita número inexistente na CCT"}); continue
        coment.append({"texto": c["texto"], "clausulas": refs, "tipo": "COMENTARIO"})
    parecer["comentarios"] = coment[:15]
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


def _reparar_json(txt):
    """v0.21.1: recupera a maior parte válida de um JSON cortado. Percorre o texto controlando strings/escapes e a pilha
    de chaves/colchetes; guarda a última posição em que um ITEM COMPLETO terminou (fechou um objeto dentro de uma lista,
    ou fechou uma lista/valor do objeto raiz) e, ao final, corta ali e fecha o que ficou aberto. Retorna (json_str, cortado)."""
    i = txt.find("{")
    if i < 0:
        raise ValueError("resposta sem objeto JSON")
    txt = txt[i:]
    pilha, em_str, esc, seguro = [], False, False, None
    for k, ch in enumerate(txt):
        if em_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                em_str = False
            continue
        if ch == '"':
            em_str = True
        elif ch in "{[":
            pilha.append(ch)
        elif ch in "}]":
            if pilha:
                pilha.pop()
            if not pilha:
                return txt[:k + 1], False          # JSON raiz fechado normalmente
            if len(pilha) <= 2:                     # fechou um item de lista (nível 2) ou uma lista/valor do raiz (nível 1)
                seguro = (k + 1, list(pilha))
    if seguro is None:
        raise ValueError("resposta cortada antes do primeiro item completo")
    corte, aberta = seguro
    base = txt[:corte].rstrip().rstrip(",")
    return base + "".join("]" if c == "[" else "}" for c in reversed(aberta)), True


def _parse_json(txt):
    """Retorna (dict, cortado). Primeiro tenta o JSON inteiro; se falhar, repara (v0.21.1)."""
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    i, j = txt.find("{"), txt.rfind("}")
    try:
        return json.loads(txt[i:j + 1] if i >= 0 and j > i else txt), False
    except Exception:
        rep, cortado = _reparar_json(txt)
        return json.loads(rep), cortado


def parecer_ia(dados, valores, comparacao=None, api_key=None, timeout=240):
    """Parecer pela IA Central (IA_GATEWAY_TOKEN). Retorna (parecer, erro)."""
    import requests
    key = (api_key or os.environ.get("IA_GATEWAY_TOKEN") or "").strip()
    t0 = time.time()
    if key:
        corpo = _material(dados, valores, comparacao)
        r = requests.post(IA_GATEWAY_URL, timeout=timeout,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
                                   "x-ia-usuario": "CCT Monitor (robô, Analisar com IA)"},
                          json={"model": MODELO_IA, "max_tokens": IA_MAX_TOKENS, "system": PROMPT_SISTEMA,
                                "messages": [{"role": "user", "content": corpo[:180000]}]})
        if r.status_code != 200:
            try:
                motivo = (r.json().get("error") or {}).get("message") or r.text
            except Exception:
                motivo = r.text
            return None, f"IA Central HTTP {r.status_code}: {str(motivo)[:300]}"
        resp = r.json()
        txt = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        stop = resp.get("stop_reason")
        modelo = resp.get("model") or MODELO_IA
    else:
        return None, "IA_GATEWAY_TOKEN não configurado no GitHub (Settings → Secrets) — parecer por IA não gerado"
    try:
        parecer, cortado = _parse_json(txt)
    except Exception as e:
        return None, f"resposta da IA não é JSON válido: {e} (stop_reason={stop}, {len(txt)} caracteres)"
    if not isinstance(parecer, dict):
        return None, "resposta da IA não é um objeto JSON"
    if cortado or stop == "max_tokens":
        parecer.setdefault("descartados", []).append({"grupo": "parecer_cortado", "item": f"resposta com {len(txt)} caracteres (limite {IA_MAX_TOKENS} tokens)",
                                                      "motivo": "a IA atingiu o limite de saída; a parte final do parecer foi descartada e os itens completos foram aproveitados"})
    parecer = _validar_refs(parecer, dados, valores)
    parecer["modelo"] = modelo
    parecer["parecer_cortado"] = bool(cortado or stop == "max_tokens")
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
        "comentarios": (parecer or {}).get("comentarios", []),
        "duracao_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    import sys
    atual = json.load(open(sys.argv[1], encoding="utf-8"))
    ant = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None
    print(json.dumps(analisar(atual, ant, usar_ia=bool(os.environ.get("IA_GATEWAY_TOKEN"))), ensure_ascii=False, indent=2))
