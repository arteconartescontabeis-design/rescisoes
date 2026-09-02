"""
mediador.py — acesso ao Sistema Mediador (MTE) via navegador real (Playwright).
Funções puras, sem banco: consultar(), parse_registros(), baixar_extrato().
Regra fundamental: falha nunca vira "não existe CCT" — todo resultado carrega um status explícito.
Validado em 02/09/2026 (GitHub Actions, reCAPTCHA aceito, 2 CCTs do CNPJ 84307370000166).
"""
import html as _h
import json
import re
import time

BASE = "https://mediador.trabalho.gov.br"
URL_CONSULTA = BASE + "/sistemas/mediador/ConsultarInstColetivo"
URL_EXTRATO = BASE + "/sistemas/mediador/Resumo/resumoVisualizarSalvarMsWordDoc?NrSolicitacao={solicitacao}"
ENDPOINT = "getConsultaAvancada"
TIMEOUT_MS = 60_000

# valores reais do <select id="cboTPRequerimento"> (inspeção de 02/09/2026)
TIPOS = {
    "Convenção Coletiva": "convencao",
    "Termo Aditivo de Convenção Coletiva": "termoAditivoConvecao",
    "Acordo Coletivo": "acordo",
    "Termo Aditivo de Acordo Coletivo": "termoAditivoAcordo",
    "Todos": "",
}
VIGENCIAS = {"Vigentes": "1", "Todos": "2", "Não Vigentes": "0"}


def parse_registros(html_resp: str):
    """Extrai os blocos de resultado da resposta HTML de getConsultaAvancada."""
    txt = re.sub(r"<script.*?</script>", "", html_resp, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", "\n", txt)
    txt = _h.unescape(txt)
    txt = re.sub(r"[ \t\xa0]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    regs = []
    for b in re.split(r"Nº do Registro\n", txt)[1:]:
        linhas = [l.strip() for l in b.split("\n") if l.strip()]

        def apos(rotulo, n=1):
            if rotulo in linhas:
                i = linhas.index(rotulo)
                return " ".join(linhas[i + 1:i + 1 + n])
            return None

        partes = []
        if "Partes" in linhas:
            ip = linhas.index("Partes")
            fim = linhas.index("Download") if "Download" in linhas else len(linhas)
            partes = [x.strip() for x in re.split(r"(?<=[A-ZÇ]) (?=SIND)", " ".join(linhas[ip + 1:fim]))]
        regs.append({
            "registro": linhas[0], "solicitacao": apos("Nº da Solicitação"),
            "tipo": apos("Tipo do Instrumento"), "vigencia": apos("Vigência", 2), "partes": partes,
        })
    return regs


def total_e_paginas(html_resp: str):
    m = re.search(r"Resultado:\s*(\d+)\s*Instrumento.*?P&#225;gina\s*(\d+)\s*de\s*(\d+)", html_resp, re.S)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (None, None, None)


def consultar(page, cnpj: str, tipo="Convenção Coletiva", vigencia="Vigentes", uf=""):
    """Preenche o formulário real e captura a resposta AJAX. Retorna dict com status explícito."""
    r = {"cnpj": cnpj, "tipo": tipo, "vigencia": vigencia, "uf": uf,
         "status": "CONSULTA_NAO_CONCLUIDA", "erro": None, "http": None,
         "registros": [], "total_site": None, "paginas": None, "etapas": [], "html": None}
    et = r["etapas"]
    capturas = []
    handler = lambda resp: capturas.append(resp) if ENDPOINT in resp.url else None
    page.on("response", handler)
    t0 = time.time()
    try:
        page.goto(URL_CONSULTA, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle")
        html_pg = page.content().lower()
        if "sua conexão não é" in html_pg or "access denied" in html_pg:
            r["erro"] = "Página de bloqueio/erro ao abrir o Mediador"
            return r
        et.append("página carregada")
        page.check("#chkNRCNPJ")
        page.fill("#txtNRCNPJ", cnpj)
        page.select_option("#cboTPRequerimento", TIPOS.get(tipo, tipo))
        page.select_option("#cboSTVigencia", VIGENCIAS.get(vigencia, vigencia))
        if uf:
            page.select_option("#cboUFRegistro", uf)
        et.append(f"filtros: tipo={tipo}, vigência={vigencia}, uf={uf or '-'}")
        page.click("#btnPesquisar")
        deadline = time.time() + 60
        while not capturas and time.time() < deadline:
            page.wait_for_timeout(400)
        if not capturas:
            r["erro"] = "Pesquisar não gerou a chamada getConsultaAvancada em 60 s (reCAPTCHA reprovado? layout alterado?)"
            return r
        resp = capturas[-1]
        r["http"] = resp.status
        corpo = resp.text()
        r["html"] = corpo
        et.append(f"{ENDPOINT} HTTP {resp.status}, {len(corpo)} bytes")
        if resp.status != 200:
            texto_erro = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style).*?</\1>", "", corpo, flags=re.S | re.I))
            texto_erro = re.sub(r"\s+", " ", _h.unescape(texto_erro)).strip()[:300]
            r["erro"] = f"Mediador respondeu HTTP {resp.status} na pesquisa"
            r["trecho_erro"] = texto_erro
            et.append(f"corpo do erro: {texto_erro[:200]}")
            return r
        r["registros"] = parse_registros(corpo)
        r["total_site"], _, r["paginas"] = total_e_paginas(corpo)
        if r["registros"]:
            r["status"] = "CONSULTA_CONFIRMADA"
            et.append(f"{len(r['registros'])} registro(s); site informa {r['total_site']} em {r['paginas']} página(s)")
            if r["paginas"] and r["paginas"] > 1:
                et.append("AVISO: mais de uma página — paginação ainda não implementada")
        elif r["total_site"] == 0 or re.search(r"nenhum (registro|instrumento)|n[ãa]o foram encontrados", corpo, re.I):
            r["status"] = "CONSULTA_COM_ALERTA"
            r["erro"] = "Site respondeu zero instrumentos para os filtros — não é prova de inexistência"
        else:
            r["erro"] = "Resposta 200 sem registros e sem mensagem de vazio (layout alterado?)"
    except Exception as e:
        r["erro"] = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
    finally:
        try:
            page.remove_listener("response", handler)
        except Exception:
            pass
        r["duracao_ms"] = int((time.time() - t0) * 1000)
    return r


def consultar_com_retry(page, cnpj, tipo="Convenção Coletiva", vigencia="Vigentes", uf="", tentativas=3, pausa_s=20):
    """Seção 92 da spec: nova tentativa automática em falha (HTTP 5xx, timeout, sem resposta)."""
    ultimo = None
    for n in range(1, tentativas + 1):
        r = consultar(page, cnpj, tipo, vigencia, uf)
        r["tentativa"] = n
        if r["status"] != "CONSULTA_NAO_CONCLUIDA":
            if ultimo is not None:
                r["etapas"].insert(0, f"tentativa {n} concluída após falha(s) anterior(es): {ultimo['erro']}")
            return r
        ultimo = r
        if n < tentativas:
            r["etapas"].append(f"tentativa {n} falhou ({r['erro']}); aguardando {pausa_s}s")
            time.sleep(pausa_s)
    ultimo["etapas"].append(f"{tentativas} tentativas sem sucesso")
    return ultimo


def baixar_extrato(page, solicitacao: str):
    """GET direto do extrato (.doc = HTML) na mesma sessão do navegador. Retorna (status, bytes)."""
    resp = page.request.get(URL_EXTRATO.format(solicitacao=solicitacao), timeout=TIMEOUT_MS)
    return resp.status, resp.body()


def extrato_valido(corpo: bytes) -> bool:
    """Extrato bom = HTML do Mediador com o bloco de registro. Vazio/curto/erro = inválido."""
    if not corpo or len(corpo) < 5000:
        return False
    cab = corpo[:4000].decode("latin-1", errors="replace").lower()
    return "<html" in cab and "mediador" in cab


if __name__ == "__main__":  # uso rápido: python mediador.py 84307370000166
    import sys
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        pg = b.new_context(locale="pt-BR").new_page()
        res = consultar(pg, sys.argv[1])
        res.pop("html", None)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        b.close()
