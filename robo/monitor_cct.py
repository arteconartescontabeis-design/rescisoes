"""
monitor_cct.py — robô do CCT Monitor (fase 1), v0.2.0
Para cada sindicato monitorado: consulta o Mediador (CCT + Termo Aditivo de CCT vigentes), identifica
registros novos, baixa o extrato, importa cláusulas, grava tudo no Supabase, abre/resolve incidentes e
registra notificações (seções 22-28, 40-43, 81-100 da spec).

Variáveis de ambiente (secrets do GitHub Actions):
  SUPABASE_URL          https://fbxelwhdiisfmnwrerbl.supabase.co
  SUPABASE_SERVICE_KEY  chave service_role (NUNCA a publicável) — ignora RLS
  TENANT_CNPJ           79876769000128 (Artecon)
  MAIL_API_KEY          chave do hub artecon-mail (a mesma da bright-task) — sem ela, notificações ficam NAO_ENVIADA
  MAIL_DESTINO_UNICO    (opcional, teste) redireciona TODOS os e-mails para este endereço
  MAIL_HUB_URL          (opcional) URL do hub; padrão: .../functions/v1/mail-send
  ORIGEM                rótulo da execução (default github-actions)
  INTERVALO_S           pausa entre sindicatos (default 8)
"""
import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

import mediador
from extrair_cct import extrair
import analisar_cct

VERSAO = "0.6.0"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TENANT_CNPJ = os.environ.get("TENANT_CNPJ", "79876769000128")
ORIGEM = os.environ.get("ORIGEM", "github-actions")
INTERVALO = float(os.environ.get("INTERVALO_S", "8"))
# Hub artecon-mail — mesmo contrato da Edge Function bright-task do Rescisões Pro
MAIL_URL = os.environ.get("MAIL_HUB_URL", "https://tjnqloycikukvvnconqn.supabase.co/functions/v1/mail-send")
MAIL_KEY = (os.environ.get("MAIL_API_KEY") or "").strip() or None
MAIL_DESTINO_UNICO = (os.environ.get("MAIL_DESTINO_UNICO") or "").strip().lower()  # modo teste: tudo vai para um endereço
MAIL_APP = "CCT Monitor"
BUCKET = "cct-arquivos"
TIPOS_MONITORADOS = ["Convenção Coletiva", "Termo Aditivo de Convenção Coletiva"]
TIPOS_ACT = ["Acordo Coletivo", "Termo Aditivo de Acordo Coletivo"]
H = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- Supabase (PostgREST)
def sb_get(tabela, params):
    r = requests.get(f"{SB_URL}/rest/v1/{tabela}", headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_insert(tabela, dados, upsert_on=None):
    h = dict(H, Prefer="return=representation" + (",resolution=merge-duplicates" if upsert_on else ""))
    params = {"on_conflict": upsert_on} if upsert_on else None
    r = requests.post(f"{SB_URL}/rest/v1/{tabela}", headers=h, params=params, data=json.dumps(dados), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def sb_patch(tabela, params, dados):
    r = requests.patch(f"{SB_URL}/rest/v1/{tabela}", headers=H, params=params, data=json.dumps(dados), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"{tabela} patch: HTTP {r.status_code} {r.text[:300]}")


def sb_rpc(fn, args):
    r = requests.post(f"{SB_URL}/rest/v1/rpc/{fn}", headers=H, data=json.dumps(args), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"rpc {fn}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def sb_upload(path, corpo: bytes, content_type="application/msword"):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": content_type, "x-upsert": "true"}
    r = requests.post(f"{SB_URL}/storage/v1/object/{BUCKET}/{path}", headers=h, data=corpo, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"storage: HTTP {r.status_code} {r.text[:300]}")
    return path


# ---------------------------------------------------------------- e-mail (hub artecon-mail)
def enviar_email(dest, assunto, html):
    """Envia via hub, um destinatário por chamada (como a bright-task). Retorna (status, erro, destinatarios_efetivos)."""
    global MAIL_DESTINO_UNICO
    dest = [d for d in dict.fromkeys(x.strip().lower() for x in dest if x) if d]
    if not dest:
        return "NAO_ENVIADA", "nenhum destinatário", []
    if not MAIL_KEY:
        return "NAO_ENVIADA", "MAIL_API_KEY não configurada (envio de e-mail ainda não ligado)", dest
    digital = f"chave enviada: {len(MAIL_KEY)} caracteres, começa '{MAIL_KEY[:3]}' termina '{MAIL_KEY[-3:]}'"
    efetivos = [MAIL_DESTINO_UNICO] if MAIL_DESTINO_UNICO else dest
    if MAIL_DESTINO_UNICO:
        html = f'<p style="font-size:12px;color:#a93226">[MODO TESTE] destinatários reais: {", ".join(dest)}</p>' + html
    falhas = []
    for d in efetivos:
        try:
            r = requests.post(MAIL_URL, headers={"Content-Type": "application/json", "x-api-key": MAIL_KEY},
                              data=json.dumps({"to": d, "assunto": assunto, "html": html, "app": MAIL_APP}), timeout=30)
            if r.status_code >= 300:
                falhas.append(f"{d}: HTTP {r.status_code} {r.text[:120]}" + (f" [{digital}]" if r.status_code in (401, 403) else ""))
        except Exception as e:
            falhas.append(f"{d}: {type(e).__name__}: {e}"[:200])
    if falhas and len(falhas) == len(efetivos):
        return "NAO_ENVIADA", "; ".join(falhas)[:400], efetivos
    return ("ENVIADA", ("parcial: " + "; ".join(falhas))[:400] if falhas else None, efetivos)


def html_padrao(titulo, corpo):
    return (f'<div style="font-family:Arial,sans-serif;max-width:640px"><div style="background:#1a5276;color:#fff;padding:12px 16px;border-radius:8px 8px 0 0">'
            f'<b>CCT Monitor</b> · Artecon Artes Contábeis</div><div style="border:1px solid #d9e1e8;border-top:0;padding:16px;border-radius:0 0 8px 8px">'
            f'<h3 style="margin:0 0 10px;color:#1a5276">{titulo}</h3>{corpo}'
            f'<p style="font-size:12px;color:#7a8894;margin-top:16px">Acesse: https://arteconartescontabeis-design.github.io/rescisoes/cct.html</p></div></div>')


def processar_testes_email(tenant):
    """Pedidos de e-mail de teste feitos no app (cct_notificacoes tipo TESTE, status PENDENTE)."""
    pend = sb_get("cct_notificacoes", {"tenant_id": f"eq.{tenant}", "tipo": "eq.TESTE", "status": "eq.PENDENTE", "select": "id,destinatarios"})
    for n in pend:
        st, erro, ef = enviar_email(n.get("destinatarios") or [], "TESTE – CCT Monitor – circuito de e-mail",
                                    html_padrao("Teste de envio", f"<p>Se você recebeu esta mensagem, o CCT Monitor está conectado ao hub artecon-mail.</p><p>{datetime.now():%d/%m/%Y %H:%M}</p>"))
        sb_patch("cct_notificacoes", {"id": f"eq.{n['id']}"}, {"status": st, "erro": erro, "tentativas": 1, "destinatarios": ef or n.get("destinatarios"),
                                                              "enviada_em": datetime.now(timezone.utc).isoformat() if st == "ENVIADA" else None})
        log(f"  E-MAIL DE TESTE (pedido no app) → {ef}: {st} {erro or ''}")


# ---------------------------------------------------------------- incidentes / notificações
def incidente(tenant, fingerprint, modulo, gravidade, mensagem, sindicato_id=None, instrumento_id=None, detalhes=None):
    try:
        inc_id = sb_rpc("cct_registrar_incidente", {
            "p_tenant": tenant, "p_fingerprint": fingerprint, "p_modulo": modulo, "p_gravidade": gravidade,
            "p_mensagem": mensagem[:1000], "p_sindicato": sindicato_id, "p_instrumento": instrumento_id,
            "p_detalhes": detalhes or {}})
        log(f"  INCIDENTE {gravidade} [{modulo}] {mensagem[:120]}")
        inc = sb_get("cct_incidentes", {"id": f"eq.{inc_id}", "select": "id,ocorrencias,gravidade"})[0]
        if inc["ocorrencias"] in (1, 3, 10) and gravidade in ("ALTO", "CRITICO"):
            notificar(tenant, "ERRO", f"ALERTA DE ERRO – {modulo} – {mensagem[:60]}",
                      f"<p><b>Módulo:</b> {modulo}<br><b>Gravidade:</b> {gravidade}<br><b>Ocorrências:</b> {inc['ocorrencias']}"
                      f"<br><b>Mensagem:</b> {mensagem}<br><b>Origem:</b> {ORIGEM} · {datetime.now():%d/%m/%Y %H:%M}</p>",
                      incidente_id=inc_id, tipo_dest=modulo)
        return inc_id
    except Exception as e:
        log(f"  !! falha ao registrar incidente: {e}")
        return None


def resolver(tenant, fingerprint):
    try:
        n = sb_rpc("cct_resolver_incidente", {"p_tenant": tenant, "p_fingerprint": fingerprint, "p_forma": "automatico"})
        if n:
            log(f"  ERRO RESOLVIDO AUTOMATICAMENTE ({fingerprint})")
    except Exception as e:
        log(f"  !! falha ao resolver incidente: {e}")


def destinatarios(tenant, tipo):
    rows = sb_get("cct_alertas_destinatarios", {"tenant_id": f"eq.{tenant}", "ativo": "eq.true", "select": "email,tipos"})
    return [r["email"] for r in rows if "TODOS" in (r["tipos"] or []) or tipo in (r["tipos"] or [])]


def notificar(tenant, tipo, assunto, html, instrumento_id=None, incidente_id=None, tipo_dest=None):
    dest = destinatarios(tenant, tipo_dest or tipo)
    reg = {"tenant_id": tenant, "tipo": tipo, "instrumento_id": instrumento_id, "incidente_id": incidente_id,
           "destinatarios": dest, "assunto": assunto, "status": "PENDENTE", "tentativas": 0}
    st, erro, efetivos = enviar_email(dest, assunto, html_padrao(assunto, html))
    reg.update(status=st, erro=erro, tentativas=1 if MAIL_KEY else 0, destinatarios=efetivos or dest,
               enviada_em=datetime.now(timezone.utc).isoformat() if st == "ENVIADA" else None)
    try:
        sb_insert("cct_notificacoes", reg)
    except Exception as e:
        log(f"  !! falha ao gravar notificação: {e}")
    log(f"  NOTIFICAÇÃO {tipo}: {reg['status']}" + (f" ({reg.get('erro')})" if reg.get("erro") else ""))
    return reg["status"]


# ---------------------------------------------------------------- importação
def data_br(s):
    """'24/08/2026' -> '2026-08-24'; '01º de agosto de 2026' -> '2026-08-01'."""
    if not s:
        return None
    import re
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    meses = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
             "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
    m = re.match(r"(\d{1,2})º?\s+de\s+(\w+)\s+de\s+(\d{4})", s, re.I)
    if m and m.group(2).lower() in meses:
        return f"{m.group(3)}-{meses[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def importar(tenant, sind, reg, page, consulta_id, empresa=None):
    """Baixa, extrai e grava um instrumento novo. Sempre grava a linha — com status explícito."""
    registro = reg["registro"]
    base = {"tenant_id": tenant, "sindicato_id": sind.get("id"), "empresa_id": (empresa or {}).get("id"), "numero_registro": registro,
            "numero_solicitacao": reg.get("solicitacao"), "tipo": reg.get("tipo"),
            "partes": [{"nome": p} for p in reg.get("partes", [])], "origem_consulta_id": consulta_id}
    vig = (reg.get("vigencia") or "").split(" - ")
    if len(vig) == 2:
        base["vigencia_inicio"], base["vigencia_fim"] = data_br(vig[0]), data_br(vig[1])

    # 1) download
    try:
        status, corpo, trecho = mediador.baixar_extrato(page, reg["solicitacao"])
        if status != 200 or not mediador.extrato_valido(corpo):
            raise RuntimeError(f"extrato inválido (HTTP {status}, {len(corpo) if corpo else 0} bytes) — {trecho}")
        resolver(tenant, f"DOWNLOAD:{registro}")
    except Exception as e:
        row = sb_insert("cct_instrumentos", dict(base, status_importacao="IMPORTACAO_NAO_CONCLUIDA",
                                                 observacoes=f"download falhou: {e}"[:1000]), upsert_on="tenant_id,numero_registro")[0]
        incidente(tenant, f"DOWNLOAD:{registro}", "DOWNLOAD", "ALTO", f"Download do extrato {registro} falhou: {e}",
                  sind.get("id"), row["id"], detalhes={"solicitacao": reg.get("solicitacao"), "resposta": str(e)[:1200]})
        return row, False

    sha = hashlib.sha256(corpo).hexdigest()
    caminho_local = f"extrato_{registro.replace('/', '-')}.doc"
    with open(caminho_local, "wb") as f:
        f.write(corpo)

    # 2) armazenamento
    arquivo_path = None
    try:
        arquivo_path = sb_upload(f"{tenant}/{registro.replace('/', '-')}.doc", corpo)
        resolver(tenant, f"ARMAZENAMENTO:{registro}")
    except Exception as e:
        incidente(tenant, f"ARMAZENAMENTO:{registro}", "ARMAZENAMENTO", "ALTO", f"Falha ao guardar {registro} no bucket: {e}", sind.get("id"))

    # 3) extração
    try:
        d = extrair(caminho_local)
        m = d["metadados"]
        if m.get("numero_registro") and m["numero_registro"] != registro:
            raise RuntimeError(f"divergência: consulta={registro}, documento={m['numero_registro']}")
        dados = dict(base, numero_processo=m.get("numero_processo"), denominacao=m.get("denominacao"),
                     data_registro=data_br(m.get("data_registro")), data_protocolo=data_br(m.get("data_protocolo")),
                     vigencia_inicio=data_br((d.get("vigencia") or {}).get("inicio")) or base.get("vigencia_inicio"),
                     vigencia_fim=data_br((d.get("vigencia") or {}).get("fim")) or base.get("vigencia_fim"),
                     data_base=d.get("data_base"), categoria=d.get("categoria"), abrangencia=d.get("abrangencia_territorial"),
                     partes=d.get("partes") or base["partes"], anexos=d.get("anexos") or [],
                     arquivo_path=arquivo_path, sha256=sha, total_clausulas=d["total_clausulas"],
                     status_importacao="IMPORTADO" if d["total_clausulas"] > 0 else "IMPORTACAO_NAO_CONCLUIDA")
        row = sb_insert("cct_instrumentos", dados, upsert_on="tenant_id,numero_registro")[0]
        if d["clausulas"]:
            sb_insert("cct_clausulas", [{"tenant_id": tenant, "instrumento_id": row["id"], "ordem": c["ordem"],
                                         "numero_extenso": c["numero_extenso"], "titulo": c["titulo"], "grupo": c["grupo"],
                                         "subgrupo": c["subgrupo"], "texto": c["texto"]} for c in d["clausulas"]],
                      upsert_on="instrumento_id,ordem")
        resolver(tenant, f"IMPORTACAO:{registro}")
        log(f"  IMPORTADO {registro}: {d['total_clausulas']} cláusulas, {len(dados['partes'])} partes")
        if dados["status_importacao"] == "IMPORTADO":
            analisar_instrumento(tenant, row["id"], d, sind.get("id"))
        return row, dados["status_importacao"] == "IMPORTADO"
    except Exception as e:
        row = sb_insert("cct_instrumentos", dict(base, arquivo_path=arquivo_path, sha256=sha,
                                                 status_importacao="IMPORTACAO_NAO_CONCLUIDA",
                                                 observacoes=f"extração falhou: {e}"), upsert_on="tenant_id,numero_registro")[0]
        incidente(tenant, f"IMPORTACAO:{registro}", "IMPORTACAO", "ALTO", f"Importação de {registro} não concluída: {e}",
                  sind.get("id"), row["id"])
        return row, False


# ---------------------------------------------------------------- análise (seções 44-61)
def carregar_dados_instrumento(inst_id):
    """Reconstrói o dict no formato do extrair_cct a partir do banco (instrumento + cláusulas)."""
    i = sb_get("cct_instrumentos", {"id": f"eq.{inst_id}", "select": "*"})[0]
    cls = sb_get("cct_clausulas", {"instrumento_id": f"eq.{inst_id}", "select": "ordem,numero_extenso,titulo,grupo,subgrupo,texto", "order": "ordem"})
    return {"metadados": {"numero_registro": i["numero_registro"], "data_registro": i.get("data_registro")}, "partes": i.get("partes") or [],
            "vigencia": {"inicio": i.get("vigencia_inicio"), "fim": i.get("vigencia_fim")}, "categoria": i.get("categoria"),
            "abrangencia_territorial": i.get("abrangencia"), "data_base": i.get("data_base"), "clausulas": cls, "total_clausulas": len(cls)}


def analisar_instrumento(tenant, inst_id, dados=None, sindicato_id=None):
    """Gera valores, comparação com a anterior e parecer (IA opcional). Regra 99: IA falhou → ANALISE_IA_NAO_CONCLUIDA."""
    try:
        dados = dados or carregar_dados_instrumento(inst_id)
        ant_id = sb_rpc("cct_anterior", {"p_instrumento": inst_id})
        anterior = carregar_dados_instrumento(ant_id) if ant_id else None
        r = analisar_cct.analisar(dados, anterior, usar_ia=bool(os.environ.get("ANTHROPIC_API_KEY")))
        versao = 1 + len(sb_get("cct_analises", {"instrumento_id": f"eq.{inst_id}", "select": "id"}))
        an = sb_insert("cct_analises", {"tenant_id": tenant, "instrumento_id": inst_id, "anterior_id": ant_id, "versao": versao, "status": r["status"],
                                        "modelo": r["modelo"], "erro_ia": r["erro_ia"], "resumo": r["resumo"], "destaques": r["destaques"],
                                        "providencias": r["providencias"], "alertas": r["alertas"], "pontos_incertos": r["pontos_incertos"],
                                        "validacao": r["validacao"], "comparacao": r["comparacao"], "duracao_ms": r["duracao_ms"], "gerado_por": ORIGEM})[0]
        requests.delete(f"{SB_URL}/rest/v1/cct_valores", headers=H, params={"instrumento_id": f"eq.{inst_id}"}, timeout=30)
        if r["valores"]:
            sb_insert("cct_valores", [{"tenant_id": tenant, "instrumento_id": inst_id, "analise_id": an["id"], **{k: v[k] for k in
                      ("chave", "tema", "descricao", "valor_texto", "valor_num", "unidade", "clausula_ordem", "clausula_titulo", "trecho", "confianca")}} for v in r["valores"]])
        sb_patch("cct_instrumentos", {"id": f"eq.{inst_id}"}, {"analise_status": r["status"], "analise_em": datetime.now(timezone.utc).isoformat()})
        if r["status"] == "CONCLUIDA":
            resolver(tenant, f"IA:{inst_id}")
        elif os.environ.get("ANTHROPIC_API_KEY"):  # sem chave configurada não é incidente, é estado
            incidente(tenant, f"IA:{inst_id}", "IA", "ATENCAO", f"ANÁLISE POR IA NÃO CONCLUÍDA – {dados['metadados']['numero_registro']}: {r['erro_ia']} (valores e comparação gravados)", sindicato_id, inst_id)
        log(f"  ANÁLISE {dados['metadados']['numero_registro']}: {r['status']} — {len(r['valores'])} valores, "
            f"{'comparada com ' + anterior['metadados']['numero_registro'] if anterior else 'sem anterior'}, {r['duracao_ms']} ms")
        return an
    except Exception as e:
        log(f"  !! análise falhou: {e}")
        incidente(tenant, f"IA:{inst_id}", "IA", "ALTO", f"Análise não concluída (erro interno): {e}", sindicato_id, inst_id)
        sb_patch("cct_instrumentos", {"id": f"eq.{inst_id}"}, {"analise_status": "ANALISE_IA_NAO_CONCLUIDA"})
        return None


def analisar_pendentes(tenant):
    """Instrumentos importados sem análise (ou reprocessamento pedido pelo app: analise_status nulo)."""
    pend = sb_get("cct_instrumentos", {"tenant_id": f"eq.{tenant}", "status_importacao": "eq.IMPORTADO", "analise_status": "is.null", "select": "id,numero_registro,sindicato_id", "limit": "20"})
    if pend:
        log(f"análises pendentes: {len(pend)}")
    for i in pend:
        analisar_instrumento(tenant, i["id"], sindicato_id=i.get("sindicato_id"))


# ---------------------------------------------------------------- ciência (seções 31-39)
def config(tenant):
    c = sb_get("cct_config", {"tenant_id": f"eq.{tenant}"})
    return c[0] if c else {"prazo_ciencia_dias_uteis": 2, "lembrete_diario": True, "escalonar_apos_prazo": True}


def prazo_ciencia(tenant):
    dias = config(tenant)["prazo_ciencia_dias_uteis"]
    try:
        return sb_rpc("cct_adicionar_dias_uteis", {"p_inicio": datetime.now().date().isoformat(), "p_dias": dias})
    except Exception:
        from datetime import timedelta
        return (datetime.now().date() + timedelta(days=dias + 2)).isoformat()


def criar_ciencias(tenant, sind, row, empresa=None):
    """Uma ciência por empresa vinculada ao sindicato (ou pela empresa do ACT); sem empresa → uma ciência do sindicato."""
    prazo = prazo_ciencia(tenant)
    alvos = []
    if empresa:
        alvos = [empresa]
    elif sind:
        vinc = sb_get("cct_empresa_sindicato", {"sindicato_id": f"eq.{sind['id']}", "select": "empresa:cct_empresas(id,razao_social,responsavel_email,gerente_email,ativo)"})
        alvos = [v["empresa"] for v in vinc if v.get("empresa") and v["empresa"].get("ativo", True)]
    linhas = []
    if alvos:
        for e in alvos:
            linhas.append({"tenant_id": tenant, "instrumento_id": row["id"], "empresa_id": e["id"], "sindicato_id": sind["id"] if sind else None,
                           "responsavel_email": e.get("responsavel_email") or (sind or {}).get("responsavel_email"),
                           "gerente_email": e.get("gerente_email") or (sind or {}).get("gerente_email"), "prazo": prazo})
    else:
        linhas.append({"tenant_id": tenant, "instrumento_id": row["id"], "empresa_id": None, "sindicato_id": sind["id"] if sind else None,
                       "responsavel_email": (sind or {}).get("responsavel_email"), "gerente_email": (sind or {}).get("gerente_email"), "prazo": prazo})
    try:
        criadas = sb_insert("cct_ciencias", linhas, upsert_on="instrumento_id,empresa_id")
    except Exception:
        criadas = []
        for l in linhas:  # empresa_id nulo não entra no on_conflict do PostgREST; insere um a um ignorando duplicidade
            try:
                criadas += sb_insert("cct_ciencias", l)
            except Exception as e:
                if "duplicate" not in str(e) and "23505" not in str(e):
                    log(f"  !! ciência: {e}")
    log(f"  {len(criadas)} ciência(s) criada(s), prazo {prazo}")
    return criadas


def html_impacto(tenant, instrumento_id, empresa_id=None):
    """Matriz de impacto (seção 53) para o e-mail do responsável: itens-chave + variação + providências."""
    try:
        params = {"instrumento_id": f"eq.{instrumento_id}", "order": "clausula_ordem"}
        if empresa_id:
            params["empresa_id"] = f"eq.{empresa_id}"
        else:
            params["limit"] = "40"
        rows = [r for r in sb_get("cct_v_impacto", params) if r.get("chave")]
        an = sb_get("cct_analises", {"instrumento_id": f"eq.{instrumento_id}", "order": "versao.desc", "limit": "1"})
        an = an[0] if an else {}
        varm = {}
        for v in ((an.get("comparacao") or {}).get("valores") or []):
            varm[f"{v.get('descricao')}|{v.get('atual')}"] = v
        if not rows and not an.get("providencias"):
            return ""
        vistos, linhas = set(), []
        for r in rows:
            k = (r["chave"], r["descricao"], r["valor_texto"])
            if k in vistos:
                continue
            vistos.add(k)
            v = varm.get(f"{r['descricao']}|{r['valor_texto']}")
            ant = f"{v['anterior']} ({v['variacao_pct']}%)" if v and v.get("variacao_pct") is not None else (v["anterior"] if v else "—")
            linhas.append(f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>{r['tema']}</td><td style='padding:4px 8px;border-bottom:1px solid #eee'>{r['descricao']}</td>"
                          f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right'><b>{r['valor_texto']}</b></td><td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right'>{ant}</td>"
                          f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>cl. {r['clausula_ordem']}</td></tr>")
        h = "<h4 style='margin:14px 0 6px;color:#1a5276'>Matriz de impacto</h4>"
        if linhas:
            h += ("<table style='border-collapse:collapse;font-size:12.5px;width:100%'><tr style='background:#eef3f7'><th style='padding:4px 8px;text-align:left'>Tema</th><th style='padding:4px 8px;text-align:left'>Descrição</th>"
                  "<th style='padding:4px 8px;text-align:right'>Valor</th><th style='padding:4px 8px;text-align:right'>Anterior</th><th style='padding:4px 8px;text-align:left'>Cláusula</th></tr>" + "".join(linhas) + "</table>")
        if an.get("resumo"):
            h += f"<p style='margin:10px 0 4px'><b>Resumo:</b> {an['resumo']}</p>"
        if an.get("providencias"):
            h += "<p style='margin:8px 0 4px'><b>Providências:</b></p><ul style='margin:0;padding-left:18px'>" + "".join(
                f"<li>{p.get('acao')}" + (f" <i>({p.get('prazo')})</i>" if p.get("prazo") else "") + "</li>" for p in an["providencias"]) + "</ul>"
        return h
    except Exception as e:
        log(f"  !! matriz de impacto: {e}")
        return ""


def processar_ciencias(tenant):
    """Rotina diária: lembretes dentro do prazo e escalonamento ao gerente após o prazo (seções 36-39)."""
    cfg = config(tenant)
    hoje = datetime.now().date().isoformat()
    pend = sb_get("cct_v_confirmacoes", {"tenant_id": f"eq.{tenant}", "status": "in.(PENDENTE,NOTIFICADO,ESCALONADO)", "select": "*"})
    log(f"ciências pendentes: {len(pend)}")
    for c in pend:
        alvo = c.get("empresa") or c.get("sindicato") or ""
        ja_hoje = (c.get("ultimo_lembrete") or "")[:10] == hoje
        dest_resp = [c["responsavel_email"]] if c.get("responsavel_email") else []
        dest_ger = [c["gerente_email"]] if c.get("gerente_email") else []
        cab = (f"<p><b>{c['tipo_instrumento']}</b> {c['numero_registro']} — {alvo}<br>Vigência {c.get('vigencia_inicio')} a {c.get('vigencia_fim')}"
               f"<br><b>Prazo para ciência:</b> {c['prazo']}</p>")
        if c["status"] == "PENDENTE" and dest_resp:
            st = notificar_para(tenant, "NOVA_CCT", f"CONVENÇÃO COLETIVA REGISTRADA – {alvo} – {c['numero_registro']}",
                                cab + html_impacto(tenant, c["instrumento_id"], c.get("empresa_id")) + "<p>Registre a ciência no CCT Monitor.</p>",
                                dest_resp, ciencia_id=c["id"], instrumento_id=c["instrumento_id"])
            if st == "ENVIADA":
                sb_patch("cct_ciencias", {"id": f"eq.{c['id']}"}, {"status": "NOTIFICADO", "notificado_em": datetime.now(timezone.utc).isoformat()})
            continue
        if c["situacao"] == "ATRASADA" and cfg.get("escalonar_apos_prazo", True) and c["status"] != "ESCALONADO":
            st = notificar_para(tenant, "ESCALONAMENTO", f"CIÊNCIA PENDENTE – PRAZO VENCIDO – {alvo} – {c['numero_registro']}",
                                cab + f"<p>Responsável ainda não registrou ciência ({c['dias_atraso']} dia(s) de atraso).</p>", dest_ger + dest_resp, ciencia_id=c["id"])
            sb_patch("cct_ciencias", {"id": f"eq.{c['id']}"}, {"status": "ESCALONADO", "escalonado_em": datetime.now(timezone.utc).isoformat(),
                                                              "lembretes": c["lembretes"] + 1, "ultimo_lembrete": datetime.now(timezone.utc).isoformat()})
            continue
        if cfg.get("lembrete_diario", True) and not ja_hoje and (dest_resp or dest_ger):
            dest = dest_resp + (dest_ger if c["status"] == "ESCALONADO" else [])
            notificar_para(tenant, "LEMBRETE", f"LEMBRETE – ciência pendente – {alvo} – {c['numero_registro']}", cab, dest, ciencia_id=c["id"])
            sb_patch("cct_ciencias", {"id": f"eq.{c['id']}"}, {"lembretes": c["lembretes"] + 1, "ultimo_lembrete": datetime.now(timezone.utc).isoformat()})


def notificar_para(tenant, tipo, assunto, html, dest, instrumento_id=None, incidente_id=None, ciencia_id=None):
    """Igual a notificar(), mas para destinatários explícitos (responsável/gerente)."""
    dest = [d for d in dict.fromkeys(dest) if d]
    reg = {"tenant_id": tenant, "tipo": tipo, "instrumento_id": instrumento_id, "incidente_id": incidente_id, "ciencia_id": ciencia_id,
           "destinatarios": dest, "assunto": assunto, "status": "PENDENTE", "tentativas": 0}
    if not dest:
        reg.update(status="NAO_ENVIADA", erro="sem responsável/gerente configurado")
    else:
        st, erro, efetivos = enviar_email(dest, assunto, html_padrao(assunto, html))
        reg.update(status=st, erro=erro, tentativas=1 if MAIL_KEY else 0, destinatarios=efetivos or dest,
                   enviada_em=datetime.now(timezone.utc).isoformat() if st == "ENVIADA" else None)
    try:
        sb_insert("cct_notificacoes", reg)
    except Exception as e:
        log(f"  !! falha ao gravar notificação: {e}")
    log(f"  NOTIFICAÇÃO {tipo} → {', '.join(dest) or '-'}: {reg['status']}")
    return reg["status"]


# ---------------------------------------------------------------- ciclo por alvo (sindicato ou empresa/ACT)
def processar_sindicato(tenant, sind, page, existentes):
    return processar_alvo(tenant, sind, None, TIPOS_MONITORADOS, page, existentes)


def processar_empresa_act(tenant, emp, page, existentes):
    alvo = {"id": None, "cnpj": emp["cnpj"], "nome": emp["razao_social"], "responsavel_email": emp.get("responsavel_email"), "gerente_email": emp.get("gerente_email")}
    st, n = processar_alvo(tenant, alvo, emp, TIPOS_ACT, page, existentes)
    sb_patch("cct_empresas", {"id": f"eq.{emp['id']}"}, {"ultima_consulta_act": datetime.now(timezone.utc).isoformat()})
    return st, n


def processar_alvo(tenant, sind, empresa, tipos, page, existentes):
    cnpj = sind["cnpj"]
    log(f"== {sind['nome']} ({cnpj}) — {'ACT' if empresa else 'CCT'}")
    t0 = time.time()
    # registro da consulta criado ANTES (status provisório) para vincular os instrumentos; atualizado ao final
    consulta = sb_insert("cct_consultas", {"tenant_id": tenant, "sindicato_id": sind["id"], "origem": ORIGEM,
                                           "status": "CONSULTA_NAO_CONCLUIDA", "erro": "em execução",
                                           "etapas": [f"ACT da empresa {sind['nome']}"] if empresa else []})[0]
    novos, etapas, http, por_tipo, total_encontrados = [], [], None, {}, 0
    for tipo in tipos:
        r = mediador.consultar_com_retry(page, cnpj, tipo=tipo, vigencia="Vigentes")
        etapas += [f"[{tipo}] {e}" for e in r["etapas"]]
        http = r["http"] or http
        por_tipo[tipo] = (r["status"], r["erro"])
        if r["status"] == "CONSULTA_NAO_CONCLUIDA":
            etapas.append(f"[{tipo}] ERRO: {r['erro']}")
            incidente(tenant, f"MEDIADOR:consulta:{cnpj}:{tipo}", "MEDIADOR", "ALTO",
                      f"CONSULTA NÃO CONCLUÍDA – {tipo} – {sind['nome']}: {r['erro']}", sind["id"],
                      detalhes={"tipo": tipo, "http": r["http"], "trecho_erro": r.get("trecho_erro"), "etapas": r["etapas"]})
            time.sleep(2)
            continue
        resolver(tenant, f"MEDIADOR:consulta:{cnpj}:{tipo}")
        resolver(tenant, f"MEDIADOR:consulta:{cnpj}")  # fingerprint da v0.2.0 (compatibilidade)
        if r["status"] == "CONSULTA_COM_ALERTA":
            etapas.append(f"[{tipo}] {r['erro']}")
        total_encontrados += len(r["registros"])
        # importar AGORA, enquanto a sessão do Mediador ainda tem este resultado (o extrato é negado — 403 — fora dele)
        for reg in r["registros"]:
            if reg["registro"] in existentes:
                continue
            log(f"  NOVO registro {reg['registro']} ({reg['tipo']}) — {reg.get('vigencia')}")
            row, ok = importar(tenant, sind, reg, page, consulta["id"], empresa)
            existentes.add(reg["registro"])
            novos.append((reg, row, ok))
            criar_ciencias(tenant, sind if sind.get("id") else None, row, empresa)
            etapas.append(f"[{tipo}] {reg['registro']}: {'importado' if ok else 'IMPORTAÇÃO NÃO CONCLUÍDA'}")
            time.sleep(1)
        time.sleep(2)

    falhas = [t for t, (st, _) in por_tipo.items() if st == "CONSULTA_NAO_CONCLUIDA"]
    if len(falhas) == len(tipos):
        status_geral = "CONSULTA_NAO_CONCLUIDA"
        erro_geral = "; ".join(f"{t}: {e}" for t, (_, e) in por_tipo.items())
    elif falhas or any(st == "CONSULTA_COM_ALERTA" for st, _ in por_tipo.values()):
        status_geral = "CONSULTA_COM_ALERTA"
        erro_geral = "; ".join(f"{t}: {e}" for t, (st, e) in por_tipo.items() if e) or None
    else:
        status_geral, erro_geral = "CONSULTA_CONFIRMADA", None

    sb_patch("cct_consultas", {"id": f"eq.{consulta['id']}"}, {
        "status": status_geral, "http": http, "qtd_encontrados": total_encontrados, "qtd_novos": len(novos),
        "duracao_ms": int((time.time() - t0) * 1000), "erro": erro_geral, "etapas": etapas})

    for reg, row, ok in novos:
        partes = "<br>".join(p["nome"] if isinstance(p, dict) else p for p in (row.get("partes") or []))
        html = (f"<p><b>{reg['tipo']}</b> registrada no Mediador para <b>{sind['nome']}</b>.</p>"
                f"<p><b>Registro:</b> {reg['registro']}<br><b>Solicitação:</b> {reg.get('solicitacao')}<br>"
                f"<b>Vigência:</b> {reg.get('vigencia')}<br><b>Partes:</b><br>{partes}</p>"
                f"<p><b>Importação:</b> {'concluída' if ok else 'NÃO CONCLUÍDA — verificar na Central de Erros'}</p>")
        st = notificar(tenant, "NOVA_CCT", f"NOVA {reg['tipo'].upper()} – {sind['nome']} – {reg['registro']}", html,
                       instrumento_id=row["id"])
        sb_patch("cct_instrumentos", {"id": f"eq.{row['id']}"}, {"status_ciencia": "NOTIFICADO" if st == "ENVIADA" else "PENDENTE"})
    if sind.get("id"):
        sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"ultima_consulta": consulta["executada_em"], "ultimo_status": status_geral})
    return status_geral, len(novos)


def main():
    log(f"CCT Monitor robô v{VERSAO} — origem {ORIGEM}")
    teste = (os.environ.get("TESTE_EMAIL") or "").strip()
    if teste:
        st, erro, ef = enviar_email([teste], "TESTE – CCT Monitor – circuito de e-mail", html_padrao("Teste de envio",
                                    f"<p>Se você recebeu esta mensagem, o CCT Monitor está conectado ao hub artecon-mail.</p><p>{datetime.now():%d/%m/%Y %H:%M}</p>"))
        log(f"TESTE DE E-MAIL para {ef}: {st} {erro or ''}")
        sys.exit(0 if st == "ENVIADA" else 1)
    tenants = sb_get("resc_tenants", {"cnpj": f"eq.{TENANT_CNPJ}", "select": "id,nome"})
    if not tenants:
        log(f"tenant {TENANT_CNPJ} não encontrado — abortando");
        sys.exit(2)
    tenant = tenants[0]["id"]
    global MAIL_DESTINO_UNICO
    cfg_dest = (config(tenant).get("email_destino_teste") or "").strip().lower()
    if cfg_dest:
        MAIL_DESTINO_UNICO = cfg_dest
        log(f"MODO TESTE de e-mail ativo (configurado no app): tudo vai para {cfg_dest}")
    processar_testes_email(tenant)
    sinds = sb_get("cct_sindicatos", {"tenant_id": f"eq.{tenant}", "monitorar": "eq.true", "ativo": "eq.true",
                                      "select": "id,cnpj,nome,tipo,uf,responsavel_email,gerente_email", "order": "nome"})
    emps_act = sb_get("cct_empresas", {"tenant_id": f"eq.{tenant}", "monitorar_act": "eq.true", "ativo": "eq.true",
                                       "select": "id,cnpj,razao_social,responsavel_email,gerente_email", "order": "razao_social"})
    filtro = os.environ.get("APENAS_CNPJ", "").strip()
    if filtro:
        sinds = [s for s in sinds if s["cnpj"] == filtro]
        emps_act = [e for e in emps_act if e["cnpj"] == filtro]
    log(f"{len(sinds)} sindicato(s) e {len(emps_act)} empresa(s) com ACT a monitorar")
    # já importados de fato; os com IMPORTACAO_NAO_CONCLUIDA voltam a ser tentados (seção 92)
    existentes = {r["numero_registro"] for r in sb_get("cct_instrumentos",
                  {"tenant_id": f"eq.{tenant}", "status_importacao": "eq.IMPORTADO", "select": "numero_registro"})}
    resumo = {"CONSULTA_CONFIRMADA": 0, "CONSULTA_COM_ALERTA": 0, "CONSULTA_NAO_CONCLUIDA": 0, "novos": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # headed sob xvfb: melhor pontuação no reCAPTCHA
        ctx = browser.new_context(locale="pt-BR", accept_downloads=True)
        page = ctx.new_page()
        try:
            for i, s in enumerate(sinds):
                try:
                    st, n = processar_sindicato(tenant, s, page, existentes)
                    resumo[st] += 1
                    resumo["novos"] += n
                except Exception as e:
                    resumo["CONSULTA_NAO_CONCLUIDA"] += 1
                    log(f"  !! exceção não tratada: {e}\n{traceback.format_exc()}")
                    incidente(tenant, f"APLICATIVO:excecao:{s['cnpj']}", "APLICATIVO", "CRITICO",
                              f"Exceção não tratada ao processar {s['nome']}: {e}", s["id"])
                if i < len(sinds) - 1 or emps_act:
                    time.sleep(INTERVALO)
            for i, e in enumerate(emps_act):
                try:
                    st, n = processar_empresa_act(tenant, e, page, existentes)
                    resumo[st] += 1
                    resumo["novos"] += n
                except Exception as ex:
                    resumo["CONSULTA_NAO_CONCLUIDA"] += 1
                    log(f"  !! exceção não tratada (ACT): {ex}\n{traceback.format_exc()}")
                    incidente(tenant, f"APLICATIVO:excecao:{e['cnpj']}", "APLICATIVO", "CRITICO", f"Exceção não tratada ao processar ACT de {e['razao_social']}: {ex}")
                if i < len(emps_act) - 1:
                    time.sleep(INTERVALO)
        finally:
            browser.close()

    if not sinds and not emps_act:
        incidente(tenant, "APLICATIVO:sem-sindicatos", "APLICATIVO", "ATENCAO", "Execução sem nenhum sindicato monitorado")
    else:
        resolver(tenant, "APLICATIVO:sem-sindicatos")
    try:
        analisar_pendentes(tenant)
    except Exception as e:
        log(f"  !! análises pendentes falharam: {e}")
    try:
        processar_ciencias(tenant)
    except Exception as e:
        log(f"  !! rotina de ciências falhou: {e}\n{traceback.format_exc()}")
        incidente(tenant, "APLICATIVO:ciencias", "APLICATIVO", "ALTO", f"Rotina de lembretes/escalonamento falhou: {e}")
    resolver(tenant, "APLICATIVO:execucao-diaria")  # heartbeat: execução chegou ao fim
    log(f"RESUMO: {json.dumps(resumo, ensure_ascii=False)}")
    with open("resumo_execucao.json", "w", encoding="utf-8") as f:
        json.dump({"versao": VERSAO, "origem": ORIGEM, "quando": datetime.now(timezone.utc).isoformat(), **resumo}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
