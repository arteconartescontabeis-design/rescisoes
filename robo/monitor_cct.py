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
from zoneinfo import ZoneInfo
BRT = ZoneInfo("America/Sao_Paulo")

import requests
from playwright.sync_api import sync_playwright

import mediador
from extrair_cct import extrair
import analisar_cct

VERSAO = "0.14.0"
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


COR_CCT = "#4a235a"
LOGO_URL = "https://arteconartescontabeis-design.github.io/rescisoes/logo_artecon.png"
APP_URL = "https://arteconartescontabeis-design.github.io/rescisoes/cct.html"
CONTATO = "dp@artecon.cnt.br"
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def _data_extenso():
    d = datetime.now(BRT)
    return f"{DIAS[d.weekday()]}, {d:%d} de {MESES[d.month - 1]} de {d.year}"


def html_padrao(titulo, corpo, faixa_sup="CCT Monitor · Aviso", botoes=None, cor=COR_CCT):
    """Cartão único da Artecon (modelo aprovado em 04/09/2026): filete + logo/data + faixa de título centralizada +
    conteúdo + botões + rodapé. `titulo` = título da faixa; `corpo` = HTML do conteúdo; `botoes` = [(texto, url), ...]."""
    botoes = botoes or [("Abrir o CCT Monitor", APP_URL)]
    b = "".join((f'<td style="padding-right:8px"><a href="{u}" style="display:inline-block;background:{cor};color:#fff;text-decoration:none;padding:12px 22px;border-radius:9px;font-size:13px;font-weight:700">{t}</a></td>' if i == 0 else
                 f'<td style="padding-right:8px"><a href="{u}" style="display:inline-block;background:#fff;color:{cor};text-decoration:none;padding:11px 20px;border-radius:9px;font-size:13px;font-weight:700;border:1.5px solid {cor}">{t}</a></td>')
                for i, (t, u) in enumerate(botoes))
    return (f'<!doctype html><html><body style="margin:0;padding:0;background:#e9edf2;font-family:Segoe UI,Arial,Helvetica,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#e9edf2;padding:24px 12px"><tr><td align="center">'
            f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 6px 24px rgba(20,40,60,.10);color:#1f2d3a">'
            f'<tr><td style="background:{cor};height:6px;font-size:0;line-height:0">&nbsp;</td></tr>'
            f'<tr><td style="padding:22px 32px 18px;border-bottom:1px solid #edf1f5"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="vertical-align:middle"><img src="{LOGO_URL}" alt="Artecon Artes Contábeis" width="170" style="display:block;max-width:170px;height:auto"></td>'
            f'<td style="vertical-align:middle;text-align:right;font-size:12px;color:#7a8894;line-height:1.5">{_data_extenso()}<br><span style="color:#9aa7b3">Palhoça/SC</span></td></tr></table></td></tr>'
            f'<tr><td style="background:{cor};padding:16px 32px;text-align:center">'
            f'<div style="font-size:11px;color:rgba(255,255,255,.75);text-transform:uppercase;letter-spacing:.8px;font-weight:700">{faixa_sup}</div>'
            f'<div style="font-size:18px;color:#ffffff;font-weight:800;line-height:1.3;margin-top:2px">{titulo}</div></td></tr>'
            f'<tr><td style="padding:24px 32px 8px;font-size:13.5px;line-height:1.55">{corpo}</td></tr>'
            f'<tr><td style="padding:12px 32px 26px"><table role="presentation" cellpadding="0" cellspacing="0"><tr>{b}</tr></table></td></tr>'
            f'<tr><td style="padding:16px 32px;background:#f7f9fb;border-top:1px solid #eceff3"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="font-size:11px;color:#9aa7b3;line-height:1.6"><b style="color:#7a8894">Artecon Artes Contábeis</b> · Palhoça/SC<br>Mensagem automática — não responda a este e-mail. Dúvidas: {CONTATO}</td>'
            f'<td style="text-align:right;font-size:11px;color:#9aa7b3">CCT Monitor v{VERSAO}</td></tr></table></td></tr>'
            f'</table></td></tr></table></body></html>')


def bloco_chave(pares, destaque=None):
    """Bloco cinza de dados-chave: pares [(rótulo, valor)] à esquerda; destaque (rótulo, valor, sub) à direita."""
    esq = "<br>".join(f'<b style="color:#1a2332">{r}:</b> {v}' for r, v in pares if v)
    dir_ = (f'<td style="padding:14px 16px;text-align:right;vertical-align:top;white-space:nowrap"><div style="font-size:11px;color:#7a8894;text-transform:uppercase;letter-spacing:.5px">{destaque[0]}</div>'
            f'<div style="font-size:22px;font-weight:800;color:#b9770e;line-height:1.2">{destaque[1]}</div><div style="font-size:11px;color:#7a8894">{destaque[2]}</div></td>') if destaque else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;border-radius:12px;margin:10px 0 6px"><tr>'
            f'<td style="padding:14px 16px;font-size:12px;color:#5b6b7a;line-height:1.7">{esq}</td>{dir_}</tr></table>')


def h2_email(t, cor=COR_CCT):
    return f'<h2 style="margin:16px 0 8px;font-size:14px;color:{cor};text-transform:uppercase;letter-spacing:.5px">{t}</h2>'


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
            notificar(tenant, "ERRO", f"Artecon · CCT Monitor — Erro {gravidade.lower()} em {modulo}: {mensagem[:70]}",
                      bloco_chave([("Módulo", modulo), ("Gravidade", gravidade), ("Ocorrências", inc["ocorrencias"]), ("Origem", f"{ORIGEM} · {datetime.now(BRT):%d/%m/%Y %H:%M}")])
                      + f'<p style="margin:10px 0 0"><b>Mensagem:</b> {mensagem}</p><p style="font-size:12px;color:#7a8894">O incidente está registrado na Central de Erros com a trilha completa; se a etapa voltar a funcionar, ele é resolvido automaticamente e você recebe o aviso.</p>',
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


MODULOS_ERRO = {"MEDIADOR", "DOWNLOAD", "IMPORTACAO", "ARMAZENAMENTO", "APLICATIVO", "EMAIL", "IA", "ERRO"}


def destinatarios(tenant, tipo):
    rows = sb_get("cct_alertas_destinatarios", {"tenant_id": f"eq.{tenant}", "ativo": "eq.true", "select": "email,tipos"})
    dest = [r["email"] for r in rows if "TODOS" in (r["tipos"] or []) or tipo in (r["tipos"] or [])]
    if tipo in MODULOS_ERRO:  # erros do aplicativo vão SEMPRE também aos gerentes/administradores do escritório
        try:
            dest += [g for g in sb_rpc("cct_emails_gerentes", {"p_tenant": tenant}) if g]
        except Exception as e:
            log(f"  !! gerentes: {e}")
    return list(dict.fromkeys(d.lower() for d in dest if d))


def notificar(tenant, tipo, assunto, html, instrumento_id=None, incidente_id=None, tipo_dest=None):
    dest = destinatarios(tenant, tipo_dest or tipo)
    reg = {"tenant_id": tenant, "tipo": tipo, "instrumento_id": instrumento_id, "incidente_id": incidente_id,
           "destinatarios": dest, "assunto": assunto, "status": "PENDENTE", "tentativas": 0}
    faixa = "CCT Monitor · Alerta de erro do sistema" if tipo == "ERRO" else "CCT Monitor · Aviso"
    st, erro, efetivos = enviar_email(dest, assunto, html_padrao(assunto, html, faixa, [("Abrir o CCT Monitor", APP_URL)] + ([("Central de Erros", APP_URL)] if tipo == "ERRO" else [])))
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


def importar(tenant, sind, reg, page, consulta_id, empresa=None, origem="monitoramento"):
    """Baixa, extrai e grava um instrumento novo. Sempre grava a linha — com status explícito."""
    registro = reg["registro"]
    base = {"tenant_id": tenant, "sindicato_id": sind.get("id"), "empresa_id": (empresa or {}).get("id"), "numero_registro": registro, "origem": origem,
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
        try:
            cadastrar_partes(tenant, sind if sind.get("id") else None, d)
        except Exception as e:
            log(f"  !! partes: {e}")
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


# ---------------------------------------------------------------- tipo do sindicato pelas partes do extrato
import re as _re
_RX_LAB = _re.compile(r"\b(EMPREGAD|TRABALHADOR|PROFISSIONA|OPERARI|OPERÁRI|MOTORIST|VIGILANT|SERVIDOR|TECNIC|TÉCNIC|BANCARI|BANCÁRI|COMERCIARI|COMERCIÁRI|CONDUTOR|ENFERM|AUXILIAR|OFICIAI)", _re.I)
_RX_PAT = _re.compile(r"\b(PATRONAL|EMPRESAS|EMPRESARI|INDUSTRIA|INDÚSTRIA|COMERCIO|COMÉRCIO|LOJIST|VAREJIST|ATACADIST|HOTEIS|HOTÉIS|HOSPITAIS|ESCOLAS|TRANSPORTADOR|CONTABILIST|AGENCIAS|AGÊNCIAS|CONCESSIONARI)", _re.I)


def inferir_tipo(nome, posicao):
    """laboral/patronal pelo nome; empate resolve pela posição no extrato (1ª parte = laboral, no padrão do Mediador)."""
    n = nome or ""
    lab, pat = bool(_RX_LAB.search(n)), bool(_RX_PAT.search(n))
    if lab and not pat:
        return "laboral"
    if pat and not lab:
        return "patronal"
    if lab and pat:  # "SINDICATO DOS EMPREGADOS NO COMÉRCIO": palavra laboral prevalece
        return "laboral"
    return "laboral" if posicao == 0 else "patronal"


def cadastrar_partes(tenant, sind, dados):
    """Define o tipo do sindicato monitorado se estiver em branco e cadastra as demais partes (sem monitorar)."""
    partes = dados.get("partes") or []
    for pos, pt in enumerate(partes):
        cnpj = _re.sub(r"\D", "", pt.get("cnpj") or "")
        if len(cnpj) != 14:
            continue
        tipo = inferir_tipo(pt.get("nome"), pos)
        if sind and sind.get("cnpj") == cnpj:
            if not sind.get("tipo"):
                sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"tipo": tipo, "tipo_origem": "cct"})
                sind["tipo"] = tipo
                log(f"  tipo do sindicato definido pela CCT: {tipo}")
            continue
        ex = sb_get("cct_sindicatos", {"tenant_id": f"eq.{tenant}", "cnpj": f"eq.{cnpj}", "select": "id,tipo"})
        if ex:
            if not ex[0].get("tipo"):
                sb_patch("cct_sindicatos", {"id": f"eq.{ex[0]['id']}"}, {"tipo": tipo, "tipo_origem": "cct"})
        else:
            try:
                sb_insert("cct_sindicatos", {"tenant_id": tenant, "cnpj": cnpj, "nome": (pt.get("nome") or "")[:200], "tipo": tipo, "tipo_origem": "cct",
                                             "monitorar": False, "observacoes": f"cadastrado automaticamente como parte da {dados['metadados'].get('numero_registro')}"})
                log(f"  parte cadastrada (sem monitorar): {pt.get('nome')} [{tipo}]")
            except Exception as e:
                log(f"  !! parte: {e}")


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
        cfg = config(tenant)
        origem_inst = (sb_get("cct_instrumentos", {"id": f"eq.{inst_id}", "select": "origem"}) or [{}])[0].get("origem")
        usar_ia = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GITHUB_TOKEN"))
        motivo_sem_ia = None
        if usar_ia and not cfg.get("ia_ativa", True):
            usar_ia, motivo_sem_ia = False, "IA desativada nos parâmetros pelo gerente"
        elif usar_ia and origem_inst == "historico" and not cfg.get("ia_historico", False):
            usar_ia, motivo_sem_ia = False, "convenção histórica: parecer por IA desligado para histórico (parâmetros)"
        elif usar_ia:
            try:
                uso = sb_rpc("cct_ia_uso_mes", {"p_tenant": tenant})
                if uso >= int(cfg.get("ia_limite_mes") or 0):
                    usar_ia, motivo_sem_ia = False, f"limite mensal de pareceres por IA atingido ({uso}/{cfg.get('ia_limite_mes')})"
            except Exception as e:
                log(f"  !! uso IA: {e}")
        r = analisar_cct.analisar(dados, anterior, usar_ia=usar_ia)
        if motivo_sem_ia:
            r["erro_ia"] = motivo_sem_ia
        versao = 1 + len(sb_get("cct_analises", {"instrumento_id": f"eq.{inst_id}", "select": "id"}))
        an = sb_insert("cct_analises", {"tenant_id": tenant, "instrumento_id": inst_id, "anterior_id": ant_id, "versao": versao, "status": r["status"],
                                        "modelo": r["modelo"], "erro_ia": r["erro_ia"], "resumo": r["resumo"], "destaques": r["destaques"],
                                        "providencias": r["providencias"], "alertas": r["alertas"], "pontos_incertos": r["pontos_incertos"],
                                        "validacao": r["validacao"], "comparacao": r["comparacao"], "duracao_ms": r["duracao_ms"], "gerado_por": ORIGEM,
                                        "comentarios": r.get("comentarios", [])})[0]
        requests.delete(f"{SB_URL}/rest/v1/cct_valores", headers=H, params={"instrumento_id": f"eq.{inst_id}"}, timeout=30)
        if r["valores"]:
            sb_insert("cct_valores", [{"tenant_id": tenant, "instrumento_id": inst_id, "analise_id": an["id"], **{k: v[k] for k in
                      ("chave", "tema", "descricao", "valor_texto", "valor_num", "unidade", "clausula_ordem", "clausula_titulo", "trecho", "confianca")}} for v in r["valores"]])
        sb_patch("cct_instrumentos", {"id": f"eq.{inst_id}"}, {"analise_status": r["status"], "analise_em": datetime.now(timezone.utc).isoformat()})
        if r["status"] == "CONCLUIDA":
            resolver(tenant, f"IA:{inst_id}")
        elif usar_ia:  # só é incidente quando a IA deveria ter rodado e falhou
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
            TDs = 'style="padding:10px 12px;border-top:1px solid #eef1f4"'
            linhas.append(f'<tr><td {TDs}>{r["tema"]}</td><td {TDs}>{r["descricao"]}</td><td align="right" {TDs}><b>{r["valor_texto"]}</b></td>'
                          f'<td align="right" {TDs} style="padding:10px 12px;border-top:1px solid #eef1f4;color:#7a8894">{ant}</td><td {TDs}>{r["clausula_ordem"]}ª</td></tr>')
        TH = "align=\"{a}\" style=\"padding:10px 12px;font-size:11px;color:#48586a;text-transform:uppercase;letter-spacing:.4px\""
        h = h2_email("Matriz de impacto — valores da convenção")
        if linhas:
            h += ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;border-spacing:0;font-size:13px;border:1px solid #e6ebf0;border-radius:12px;overflow:hidden">'
                  f'<tr style="background:#f4f6f8"><th {TH.format(a="left")}>Tema</th><th {TH.format(a="left")}>Descrição</th><th {TH.format(a="right")}>Valor</th><th {TH.format(a="right")}>Anterior</th><th {TH.format(a="left")}>Cláusula</th></tr>'
                  + "".join(linhas) + "</table>")
        if an.get("resumo"):
            h += f'<p style="margin:12px 0 4px;font-size:13px;color:#1f2d3a"><b>Resumo:</b> {an["resumo"]}</p>'
        if an.get("providencias"):
            h += h2_email("Providências para o DP") + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;line-height:1.55">' + "".join(
                f'<tr><td style="padding:6px 0;border-bottom:1px solid #eef1f4"><span style="display:inline-block;width:22px;height:22px;border-radius:50%;background:{COR_CCT};color:#fff;text-align:center;line-height:22px;font-size:11px;font-weight:700;margin-right:8px">{i + 1}</span>{p.get("acao")}'
                + (f' <i style="color:#7a8894">({p.get("prazo")})</i>' if p.get("prazo") else "") + "</td></tr>" for i, p in enumerate(an["providencias"])) + "</table>"
            h += '<p style="margin:10px 0 0;font-size:11px;color:#9aa7b3">Valores e cláusulas conferidos contra o texto registrado no Mediador. Confira sempre a cláusula antes de agir.</p>'
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
        pares = [("Convenção", f"{c['tipo_instrumento']} {c['numero_registro']}"), ("Sindicato", c.get("sindicato")), ("Empresa", c.get("empresa")),
                 ("Vigência", f"{fmt_br(c.get('vigencia_inicio'))} a {fmt_br(c.get('vigencia_fim'))}"), ("Responsável", c.get("responsavel_email"))]
        cab = bloco_chave(pares, ("Prazo para ciência", fmt_br(c.get("prazo")), c["situacao"].replace("_", " ").lower() if c.get("situacao") else ""))
        botoes = [("Registrar ciência", APP_URL), ("Ver convenção completa", APP_URL)]
        if c["status"] == "PENDENTE" and dest_resp:
            st = notificar_para(tenant, "NOVA_CCT", f"Artecon · CCT Monitor — Nova {c['tipo_instrumento']} {c['numero_registro']} · {alvo} · ciência até {fmt_br(c.get('prazo'))}",
                                html_padrao(f"Nova {c['tipo_instrumento']} registrada no Mediador", cab + html_impacto(tenant, c["instrumento_id"], c.get("empresa_id")),
                                            "CCT Monitor · Aviso ao responsável", botoes), dest_resp, ciencia_id=c["id"], instrumento_id=c["instrumento_id"], pronto=True)
            if st == "ENVIADA":
                sb_patch("cct_ciencias", {"id": f"eq.{c['id']}"}, {"status": "NOTIFICADO", "notificado_em": datetime.now(timezone.utc).isoformat()})
            continue
        if c["situacao"] == "ATRASADA" and cfg.get("escalonar_apos_prazo", True) and c["status"] != "ESCALONADO":
            st = notificar_para(tenant, "ESCALONAMENTO", f"Artecon · CCT Monitor — Ciência pendente com prazo vencido · {alvo} · {c['numero_registro']}",
                                html_padrao("Ciência pendente — prazo vencido", cab + f'<p style="color:#c0392b"><b>O responsável ainda não registrou ciência</b> ({c["dias_atraso"]} dia(s) de atraso). Este aviso foi escalonado ao gerente.</p>',
                                            "CCT Monitor · Escalonamento ao gerente", botoes), dest_ger + dest_resp, ciencia_id=c["id"], pronto=True)
            sb_patch("cct_ciencias", {"id": f"eq.{c['id']}"}, {"status": "ESCALONADO", "escalonado_em": datetime.now(timezone.utc).isoformat(),
                                                              "lembretes": c["lembretes"] + 1, "ultimo_lembrete": datetime.now(timezone.utc).isoformat()})
            continue
        if cfg.get("lembrete_diario", True) and not ja_hoje and (dest_resp or dest_ger):
            dest = dest_resp + (dest_ger if c["status"] == "ESCALONADO" else [])
            notificar_para(tenant, "LEMBRETE", f"Artecon · CCT Monitor — Lembrete: ciência pendente · {alvo} · até {fmt_br(c.get('prazo'))}",
                           html_padrao("Lembrete de ciência pendente", cab + "<p>Registre a ciência no CCT Monitor para encerrar este lembrete.</p>", "CCT Monitor · Lembrete", botoes), dest, ciencia_id=c["id"], pronto=True)
            sb_patch("cct_ciencias", {"id": f"eq.{c['id']}"}, {"lembretes": c["lembretes"] + 1, "ultimo_lembrete": datetime.now(timezone.utc).isoformat()})


def fmt_br(d):
    return f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if d and len(str(d)) >= 10 else (d or "—")


def notificar_para(tenant, tipo, assunto, html, dest, instrumento_id=None, incidente_id=None, ciencia_id=None, pronto=False):
    """Igual a notificar(), mas para destinatários explícitos (responsável/gerente). pronto=True: html já é o cartão completo."""
    dest = [d for d in dict.fromkeys(dest) if d]
    reg = {"tenant_id": tenant, "tipo": tipo, "instrumento_id": instrumento_id, "incidente_id": incidente_id, "ciencia_id": ciencia_id,
           "destinatarios": dest, "assunto": assunto, "status": "PENDENTE", "tentativas": 0}
    if not dest:
        reg.update(status="NAO_ENVIADA", erro="sem responsável/gerente configurado")
    else:
        st, erro, efetivos = enviar_email(dest, assunto, html if pronto else html_padrao(assunto, html))
        reg.update(status=st, erro=erro, tentativas=1 if MAIL_KEY else 0, destinatarios=efetivos or dest,
                   enviada_em=datetime.now(timezone.utc).isoformat() if st == "ENVIADA" else None)
    try:
        sb_insert("cct_notificacoes", reg)
    except Exception as e:
        log(f"  !! falha ao gravar notificação: {e}")
    log(f"  NOTIFICAÇÃO {tipo} → {', '.join(dest) or '-'}: {reg['status']}")
    return reg["status"]


# ---------------------------------------------------------------- histórico (últimos N anos), aos poucos
def ano_do_registro(reg):
    m = _re.search(r"/(\d{4})", reg.get("registro") or "")
    if m:
        return int(m.group(1))
    m = _re.search(r"(\d{4})", (reg.get("vigencia") or "")[:10][::-1])
    return None


def importar_historico(tenant, page, existentes, cfg):
    """Para sindicatos com historico_status PENDENTE/EM_ANDAMENTO: consulta 'Todos' (vigentes e não vigentes),
    importa registros dos últimos N anos ainda ausentes — no máximo `max` downloads por execução, com pausas."""
    anos, maximo = int(cfg.get("historico_anos") or 5), int(cfg.get("historico_max_por_execucao") or 8)
    ano_min = datetime.now().year - anos
    pend = sb_get("cct_sindicatos", {"tenant_id": f"eq.{tenant}", "ativo": "eq.true", "historico_status": "in.(PENDENTE,EM_ANDAMENTO)",
                                     "select": "id,cnpj,nome,tipo,historico_status", "order": "historico_em.nullsfirst", "limit": "3"})
    if not pend:
        return 0
    log(f"HISTÓRICO: {len(pend)} sindicato(s) pendente(s); limite {maximo} download(s) nesta execução; registros desde {ano_min}")
    feitos = 0
    for sind in pend:
        if feitos >= maximo:
            break
        sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"historico_status": "EM_ANDAMENTO", "historico_em": datetime.now(timezone.utc).isoformat()})
        faltam, erro_consulta = [], None
        for tipo in TIPOS_MONITORADOS:
            if feitos >= maximo:
                break
            pendentes_pg = []

            def on_pagina(n, regs):
                # importa os desta página AGORA (o download só funciona enquanto a página está na tela)
                nonlocal feitos
                for reg in regs:
                    ano = ano_do_registro(reg)
                    if ano is None or ano < ano_min or reg["registro"] in existentes:
                        continue
                    if feitos >= maximo:
                        pendentes_pg.append(reg["registro"]); continue
                    log(f"  HISTÓRICO {reg['registro']} ({reg['tipo']}) — {reg.get('vigencia')}")
                    row, ok = importar(tenant, sind, reg, page, None, None, origem="historico")
                    existentes.add(reg["registro"]); feitos += 1
                    time.sleep(6)

            r = None
            for tent in (1, 2):  # uma nova tentativa em falha (seção 92), sem duplicar consultas boas
                r = mediador.consultar(page, sind["cnpj"], tipo=tipo, vigencia="Todos", on_pagina=on_pagina)
                if r["status"] != "CONSULTA_NAO_CONCLUIDA":
                    break
                time.sleep(20)
            if r["status"] == "CONSULTA_NAO_CONCLUIDA":
                erro_consulta = r["erro"]
            else:
                faltam += pendentes_pg
            time.sleep(4)
        if erro_consulta:
            sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"historico_obs": f"consulta não concluída: {erro_consulta}"})
        elif not faltam and feitos < maximo:
            sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"historico_status": "CONCLUIDO", "historico_em": datetime.now(timezone.utc).isoformat(), "historico_obs": f"histórico de {anos} anos importado"})
            log(f"  HISTÓRICO concluído: {sind['nome']}")
        else:
            sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"historico_obs": f"{len(set(faltam))} registro(s) ainda por importar — continua na próxima execução"})
    return feitos


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
        st, erro, ef = enviar_email([teste], "Artecon · CCT Monitor — TESTE de envio", html_padrao("Teste de envio",
                                    f"<p>Se você recebeu esta mensagem, o CCT Monitor está conectado ao hub artecon-mail.</p><p style='color:#7a8894;font-size:12px'>{datetime.now(BRT):%d/%m/%Y %H:%M} · pode ignorar/excluir.</p>", "CCT Monitor · Teste"))
        log(f"TESTE DE E-MAIL para {ef}: {st} {erro or ''}")
        sys.exit(0 if st == "ENVIADA" else 1)
    tenants = sb_get("resc_tenants", {"cnpj": f"eq.{TENANT_CNPJ}", "select": "id,nome"})
    if not tenants:
        log(f"tenant {TENANT_CNPJ} não encontrado — abortando");
        sys.exit(2)
    tenant = tenants[0]["id"]
    cfg0 = config(tenant)
    forcar = (os.environ.get("FORCAR") or "").lower() in ("1", "true", "sim")
    if not forcar and ORIGEM == "github-actions":
        # Disparos de hora em hora (aos :05). Regra: executa no PRIMEIRO disparo após cada horário configurado (HH:MM, BRT),
        # se ainda não houve execução automática desde esse horário. Atraso máximo ≈ 1 h. Fins de semana não consultam.
        from datetime import timedelta
        agora = datetime.now(BRT)
        if agora.weekday() >= 5:
            log(f"fim de semana ({agora:%d/%m %H:%M} BRT) — sem consulta"); processar_testes_email(tenant); return
        horarios = []
        for h in (cfg0.get("horarios_consulta") or "06:00").split(","):
            m = _re.match(r"\s*(\d{1,2}):(\d{2})", h)
            if m:
                horarios.append((int(m.group(1)) % 24, int(m.group(2)) % 60))
        devido = None
        for hh, mm in horarios:
            alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if alvo > agora:
                alvo -= timedelta(days=1)
            if alvo.weekday() >= 5:
                continue
            if devido is None or alvo > devido:
                devido = alvo
        if devido is None:
            log("nenhum horário válido configurado"); processar_testes_email(tenant); return
        # devido = último horário configurado já passado. Executa enquanto houver sindicato monitorado NÃO consultado desde então
        # (assim, o que não coube em um disparo de 45 min continua no próximo, e um máximo por execução vira fila natural).
        todos = sb_get("cct_sindicatos", {"tenant_id": f"eq.{tenant}", "monitorar": "eq.true", "ativo": "eq.true", "select": "id,ultima_consulta"})
        pendentes = [x for x in todos if not x.get("ultima_consulta") or datetime.fromisoformat(x["ultima_consulta"].replace("Z", "+00:00")).astimezone(BRT) < devido]
        if todos and not pendentes:
            log(f"nada devido: todos os {len(todos)} sindicatos já consultados desde {devido:%d/%m %H:%M} BRT (horários {cfg0.get('horarios_consulta')})")
            processar_testes_email(tenant); return
        log(f"executando: horário devido {devido:%d/%m %H:%M} BRT, agora {agora:%H:%M} — {len(pendentes)} de {len(todos)} sindicato(s) ainda não consultados desde então")
    global MAIL_DESTINO_UNICO
    cfg_dest = (config(tenant).get("email_destino_teste") or "").strip().lower()
    if cfg_dest:
        MAIL_DESTINO_UNICO = cfg_dest
        log(f"MODO TESTE de e-mail ativo (configurado no app): tudo vai para {cfg_dest}")
    processar_testes_email(tenant)
    if cfg0.get("historico_automatico"):
        try:
            n = sb_rpc("cct_enfileirar_historico", {"p_tenant": tenant})
            if n:
                log(f"histórico automático: {n} sindicato(s) entraram na fila dos últimos {cfg0.get('historico_anos') or 5} anos")
        except Exception as e:
            log(f"  !! histórico automático: {e}")
    # FILA: os consultados há mais tempo primeiro; opcionalmente só N por execução; intervalo configurável
    global INTERVALO
    INTERVALO = float(cfg0.get("intervalo_consultas_s") or INTERVALO)
    sinds = sb_get("cct_sindicatos", {"tenant_id": f"eq.{tenant}", "monitorar": "eq.true", "ativo": "eq.true",
                                      "select": "id,cnpj,nome,tipo,uf,responsavel_email,gerente_email,ultima_consulta", "order": "ultima_consulta.asc.nullsfirst"})
    maximo = int(cfg0.get("max_sindicatos_por_execucao") or 0)
    if maximo and len(sinds) > maximo:
        log(f"fila: {len(sinds)} sindicatos, {maximo} por execução (os demais ficam para a próxima)")
        sinds = sinds[:maximo]
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
        inicio_exec = time.time()
        try:
            for i, s in enumerate(sinds):
                if time.time() - inicio_exec > 38 * 60:  # limite do Actions: 45 min — deixa o resto para a próxima execução
                    log(f"tempo esgotado: {len(sinds) - i} sindicato(s) ficam para a próxima execução"); break
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
    try:
        cfg = config(tenant)
        if cfg.get("historico_anos") is not None or True:
            with sync_playwright() as p2:
                b2 = p2.chromium.launch(headless=False)
                pg2 = b2.new_context(locale="pt-BR", accept_downloads=True).new_page()
                try:
                    n_hist = importar_historico(tenant, pg2, existentes, cfg)
                    resumo["historico"] = n_hist
                finally:
                    b2.close()
    except Exception as e:
        log(f"  !! histórico: {e}\n{traceback.format_exc()}")
        incidente(tenant, "APLICATIVO:historico", "APLICATIVO", "ATENCAO", f"Importação do histórico falhou nesta execução: {e}")
    resolver(tenant, "APLICATIVO:execucao-diaria")  # heartbeat: execução chegou ao fim
    log(f"RESUMO: {json.dumps(resumo, ensure_ascii=False)}")
    with open("resumo_execucao.json", "w", encoding="utf-8") as f:
        json.dump({"versao": VERSAO, "origem": ORIGEM, "quando": datetime.now(timezone.utc).isoformat(), **resumo}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
