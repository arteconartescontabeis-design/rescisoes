"""
Protótipo de teste v0.3 — consulta ao Sistema Mediador (MTE) via navegador real
Captura a resposta AJAX getConsultaAvancada (inclui reCAPTCHA gerado pela própria página)
Uso:
  pip install playwright && playwright install chromium
  python teste_mediador.py --inspecionar                 # lista campos/botões da página
  python teste_mediador.py --cnpj 00000000000191         # consulta por CNPJ do sindicato
  python teste_mediador.py --cnpj ... --tipo "Termo Aditivo" --headless

Regra fundamental: nenhum erro é convertido em "não existe CCT".
Resultado sempre é um dos: CONSULTA_CONFIRMADA | CONSULTA_COM_ALERTA | CONSULTA_NAO_CONCLUIDA
"""
import argparse, json, os, re, sys, time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://mediador.trabalho.gov.br/sistemas/mediador/ConsultarInstColetivo"
ENDPOINT = "getConsultaAvancada"
BASE = "https://mediador.trabalho.gov.br"
TIMEOUT_MS = 60_000


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def inspecionar(page):
    """Dump de todos os controles do formulário para validar seletores."""
    page.goto(URL, timeout=TIMEOUT_MS)
    page.wait_for_load_state("networkidle")
    log(f"Título: {page.title()}  |  URL final: {page.url}")
    itens = page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('input,select,textarea,button,a[href*="Pesquisar"],img[alt]')) {
            const lab = el.labels && el.labels[0] ? el.labels[0].innerText.trim() : '';
            const item = {tag: el.tagName, type: el.type||'', id: el.id, name: el.name,
                          label: lab, text: (el.innerText||el.value||'').trim().slice(0,60)};
            if (el.tagName === 'SELECT') item.opcoes = [...el.options].map(o => o.text.trim());
            out.push(item);
        }
        return out;
    }""")
    for i in itens:
        print(json.dumps(i, ensure_ascii=False))
    # sinais de CAPTCHA / bloqueio
    html = page.content().lower()
    for sinal in ("captcha", "recaptcha", "hcaptcha", "cloudflare", "access denied"):
        if sinal in html:
            log(f"ATENÇÃO: sinal encontrado na página: {sinal}")
    page.screenshot(path="mediador_inspecao.png", full_page=True)
    log("Screenshot salvo em mediador_inspecao.png")


def _select_por_texto(page, rotulo_regex, texto):
    """Seleciona opção em um <select> localizado pelo label (case-insensitive)."""
    sel = page.get_by_label(re.compile(rotulo_regex, re.I))
    sel.select_option(label=texto)


def parse_registros(html_resp):
    """Extrai os blocos de resultado da resposta HTML de getConsultaAvancada."""
    import html as _h
    txt = re.sub(r"<script.*?</script>", "", html_resp, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", "\n", txt); txt = _h.unescape(txt)
    txt = re.sub(r"[ \t\xa0]+", " ", txt); txt = re.sub(r"\n\s*\n+", "\n", txt)
    regs = []
    blocos = re.split(r"Nº do Registro\n", txt)[1:]
    for b in blocos:
        linhas = [l.strip() for l in b.split("\n") if l.strip()]
        def apos(rotulo, n=1):
            if rotulo in linhas:
                i = linhas.index(rotulo); return " ".join(linhas[i + 1:i + 1 + n])
            return None
        try:
            ip = linhas.index("Partes"); fim = linhas.index("Download") if "Download" in linhas else len(linhas)
            partes = " ".join(linhas[ip + 1:fim]).replace(" e Outros", " e Outros")
            partes = [x.strip() for x in re.split(r"(?<=[A-ZÇ]) (?=SIND)", partes)]
        except ValueError:
            partes = []
        regs.append({"registro": linhas[0], "solicitacao": apos("Nº da Solicitação"),
                     "tipo": apos("Tipo do Instrumento"), "vigencia": apos("Vigência", 2),
                     "partes": partes})
    return regs


def consultar(page, cnpj, tipo, vigencia, uf):
    """Preenche o formulário real do Mediador e captura a resposta de getConsultaAvancada."""
    resultado = {"cnpj": cnpj, "tipo": tipo, "vigencia": vigencia, "uf": uf,
                 "status": "CONSULTA_NAO_CONCLUIDA", "erro": None, "http": None, "registros": [], "etapas": []}
    et = resultado["etapas"]
    capturas = []
    page.on("response", lambda r: capturas.append(r) if ENDPOINT in r.url else None)
    try:
        page.goto(URL, timeout=TIMEOUT_MS)
        page.wait_for_load_state("networkidle")
        et.append("página carregada")
        # checkbox habilita o campo CNPJ
        page.check("#chkNRCNPJ")
        page.fill("#txtNRCNPJ", cnpj)
        et.append("CNPJ preenchido")
        # tipo: valores reais do select (Convenção Coletiva = 'convencao')
        mapa_tipo = {"Convenção Coletiva": "convencao", "Termo Aditivo": "termoAditivoConvecao",
                     "Acordo Coletivo": "acordo", "Todos": ""}
        page.select_option("#cboTPRequerimento", mapa_tipo.get(tipo, tipo))
        mapa_vig = {"Vigentes": "1", "Todos": "2", "Não Vigentes": "0"}
        page.select_option("#cboSTVigencia", mapa_vig.get(vigencia, vigencia))
        if uf:
            page.select_option("#cboUFRegistro", uf)
        et.append("filtros aplicados")
        page.click("#btnPesquisar")
        # espera a chamada AJAX da pesquisa
        deadline = time.time() + 60
        while not capturas and time.time() < deadline:
            page.wait_for_timeout(500)
        if not capturas:
            resultado["erro"] = "Pesquisar não gerou chamada getConsultaAvancada em 60 s"
            page.screenshot(path=f"mediador_erro_{cnpj}.png", full_page=True)
            return resultado
        resp = capturas[-1]
        resultado["http"] = resp.status
        corpo = resp.text()
        et.append(f"{ENDPOINT} HTTP {resp.status}, {len(corpo)} bytes")
        with open(f"resposta_{cnpj}.txt", "w", encoding="utf-8") as f:
            f.write(corpo)
        if resp.status != 200:
            resultado["erro"] = f"HTTP {resp.status} — ver resposta_{cnpj}.txt"
            resultado["trecho"] = corpo[:1500]
            return resultado
        # tenta JSON; senão guarda o HTML/trecho para análise
        try:
            dados = json.loads(corpo)
            resultado["registros"] = dados if isinstance(dados, list) else dados.get("registros", dados)
            resultado["status"] = "CONSULTA_CONFIRMADA" if resultado["registros"] else "CONSULTA_COM_ALERTA"
            if not resultado["registros"]:
                resultado["erro"] = "resposta 200 sem registros — validar critérios antes de concluir"
        except Exception:
            resultado["trecho"] = corpo[:1500]
            page.wait_for_timeout(2000)
            page.screenshot(path=f"mediador_resultado_{cnpj}.png", full_page=True)
            resultado["status"] = "CONSULTA_CONFIRMADA" if re.search(r"registro|vig[êe]ncia", corpo, re.I) else "CONSULTA_COM_ALERTA"
            resultado["erro"] = None if resultado["status"] == "CONSULTA_CONFIRMADA" else "resposta não reconhecida — ver arquivo e screenshot"
        # ---- registros a partir do HTML da resposta ----
        resultado["registros"] = parse_registros(corpo)
        if resultado["registros"]:
            resultado["status"] = "CONSULTA_CONFIRMADA"; resultado["erro"] = None
            resultado.pop("trecho", None)
        et.append(f"{len(resultado['registros'])} registro(s) identificados no HTML")

        # ---- download direto do extrato (.doc = HTML), sem pop-up, mesma sessão ----
        for r in resultado["registros"]:
            try:
                url_doc = f"{BASE}/sistemas/mediador/Resumo/resumoVisualizarSalvarMsWordDoc?NrSolicitacao={r['solicitacao']}"
                resp = page.request.get(url_doc, timeout=60000)
                corpo_doc = resp.body()
                nome = "extrato_" + r["registro"].replace("/", "-") + ".doc"
                with open(nome, "wb") as f: f.write(corpo_doc)
                r["arquivo"] = nome; r["download_http"] = resp.status; r["bytes"] = len(corpo_doc)
                et.append(f"download {r['registro']}: HTTP {resp.status}, {len(corpo_doc)} bytes")
                if resp.status == 200 and len(corpo_doc) > 5000:
                    try:
                        from extrair_cct import extrair
                        dados = extrair(nome)
                        with open(nome.replace(".doc", ".json"), "w", encoding="utf-8") as f:
                            json.dump(dados, f, ensure_ascii=False, indent=2)
                        r["extrato"] = {k: v for k, v in dados.items() if k not in ("clausulas",)}
                        et.append(f"extração {r['registro']}: {dados['total_clausulas']} cláusulas, "
                                  f"registro no doc = {dados['metadados']['numero_registro']}")
                    except Exception as e:
                        et.append(f"extração {r['registro']} falhou: {type(e).__name__}: {e}")
                else:
                    et.append(f"AVISO: download {r['registro']} suspeito (status/tamanho) — conferir arquivo")
            except Exception as e:
                et.append(f"download {r.get('registro')} falhou: {type(e).__name__}: {e}")
    except PWTimeout as e:
        resultado["erro"] = f"TIMEOUT: {e}"
    except Exception as e:
        resultado["erro"] = f"{type(e).__name__}: {e}"
        try: page.screenshot(path=f"mediador_erro_{cnpj}.png", full_page=True)
        except Exception: pass
    return resultado


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspecionar", action="store_true")
    ap.add_argument("--cnpj", help="CNPJ do sindicato (só dígitos)")
    ap.add_argument("--tipo", default="Convenção Coletiva")
    ap.add_argument("--vigencia", default="Vigentes")
    ap.add_argument("--uf", default="", help="Deixe vazio para não filtrar (recomendado)")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=a.headless, slow_mo=0 if a.headless else 150)
        ctx = browser.new_context(locale="pt-BR", accept_downloads=True,
                                  user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36")
        page = ctx.new_page()
        t0 = time.time()
        if a.inspecionar:
            inspecionar(page)
        elif a.cnpj:
            r = consultar(page, re.sub(r"\D", "", a.cnpj), a.tipo, a.vigencia, a.uf)
            r["duracao_s"] = round(time.time() - t0, 1)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            with open(f"resultado_{r['cnpj']}.json", "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
        else:
            ap.print_help(); sys.exit(1)
        browser.close()


if __name__ == "__main__":
    main()
