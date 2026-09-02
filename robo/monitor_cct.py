"""
monitor_cct.py — robô do CCT Monitor (fase 1), v0.2.0
Para cada sindicato monitorado: consulta o Mediador (CCT + Termo Aditivo de CCT vigentes), identifica
registros novos, baixa o extrato, importa cláusulas, grava tudo no Supabase, abre/resolve incidentes e
registra notificações (seções 22-28, 40-43, 81-100 da spec).

Variáveis de ambiente (secrets do GitHub Actions):
  SUPABASE_URL          https://fbxelwhdiisfmnwrerbl.supabase.co
  SUPABASE_SERVICE_KEY  chave service_role (NUNCA a publicável) — ignora RLS
  TENANT_CNPJ           79876769000128 (Artecon)
  MAIL_WEBHOOK_URL      (opcional) endpoint que envia e-mail: POST JSON {to:[...], subject, html}
  MAIL_WEBHOOK_KEY      (opcional) Authorization: Bearer <key> para o webhook
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

VERSAO = "0.2.0"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TENANT_CNPJ = os.environ.get("TENANT_CNPJ", "79876769000128")
ORIGEM = os.environ.get("ORIGEM", "github-actions")
INTERVALO = float(os.environ.get("INTERVALO_S", "8"))
MAIL_URL = os.environ.get("MAIL_WEBHOOK_URL")
MAIL_KEY = os.environ.get("MAIL_WEBHOOK_KEY")
BUCKET = "cct-arquivos"
TIPOS_MONITORADOS = ["Convenção Coletiva", "Termo Aditivo de Convenção Coletiva"]
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
    if not dest:
        reg.update(status="NAO_ENVIADA", erro="nenhum destinatário configurado para este tipo")
    elif not MAIL_URL:
        reg.update(status="NAO_ENVIADA", erro="MAIL_WEBHOOK_URL não configurado (envio de e-mail ainda não ligado)")
    else:
        try:
            h = {"Content-Type": "application/json"}
            if MAIL_KEY:
                h["Authorization"] = f"Bearer {MAIL_KEY}"
            r = requests.post(MAIL_URL, headers=h, data=json.dumps({"to": dest, "subject": assunto, "html": html}), timeout=40)
            reg["tentativas"] = 1
            if r.status_code < 300:
                reg.update(status="ENVIADA", enviada_em=datetime.now(timezone.utc).isoformat())
            else:
                reg.update(status="NAO_ENVIADA", erro=f"webhook HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            reg.update(status="NAO_ENVIADA", tentativas=1, erro=f"{type(e).__name__}: {e}"[:300])
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


def importar(tenant, sind, reg, page, consulta_id):
    """Baixa, extrai e grava um instrumento novo. Sempre grava a linha — com status explícito."""
    registro = reg["registro"]
    base = {"tenant_id": tenant, "sindicato_id": sind["id"], "numero_registro": registro,
            "numero_solicitacao": reg.get("solicitacao"), "tipo": reg.get("tipo"),
            "partes": [{"nome": p} for p in reg.get("partes", [])], "origem_consulta_id": consulta_id}
    vig = (reg.get("vigencia") or "").split(" - ")
    if len(vig) == 2:
        base["vigencia_inicio"], base["vigencia_fim"] = data_br(vig[0]), data_br(vig[1])

    # 1) download
    try:
        status, corpo = mediador.baixar_extrato(page, reg["solicitacao"])
        if status != 200 or not mediador.extrato_valido(corpo):
            raise RuntimeError(f"extrato inválido (HTTP {status}, {len(corpo) if corpo else 0} bytes)")
        resolver(tenant, f"DOWNLOAD:{registro}")
    except Exception as e:
        row = sb_insert("cct_instrumentos", dict(base, status_importacao="IMPORTACAO_NAO_CONCLUIDA",
                                                 observacoes=f"download falhou: {e}"), upsert_on="tenant_id,numero_registro")[0]
        incidente(tenant, f"DOWNLOAD:{registro}", "DOWNLOAD", "ALTO", f"Download do extrato {registro} falhou: {e}",
                  sind["id"], row["id"])
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
        incidente(tenant, f"ARMAZENAMENTO:{registro}", "ARMAZENAMENTO", "ALTO", f"Falha ao guardar {registro} no bucket: {e}", sind["id"])

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
        return row, dados["status_importacao"] == "IMPORTADO"
    except Exception as e:
        row = sb_insert("cct_instrumentos", dict(base, arquivo_path=arquivo_path, sha256=sha,
                                                 status_importacao="IMPORTACAO_NAO_CONCLUIDA",
                                                 observacoes=f"extração falhou: {e}"), upsert_on="tenant_id,numero_registro")[0]
        incidente(tenant, f"IMPORTACAO:{registro}", "IMPORTACAO", "ALTO", f"Importação de {registro} não concluída: {e}",
                  sind["id"], row["id"])
        return row, False


# ---------------------------------------------------------------- ciclo por sindicato
def processar_sindicato(tenant, sind, page, existentes):
    cnpj = sind["cnpj"]
    log(f"== {sind['nome']} ({cnpj})")
    novos, etapas, status_geral, erro_geral, http = [], [], "CONSULTA_CONFIRMADA", None, None
    t0 = time.time()
    regs_total = []
    for tipo in TIPOS_MONITORADOS:
        r = mediador.consultar(page, cnpj, tipo=tipo, vigencia="Vigentes")
        etapas += [f"[{tipo}] {e}" for e in r["etapas"]]
        http = r["http"] or http
        if r["status"] == "CONSULTA_NAO_CONCLUIDA":
            status_geral, erro_geral = "CONSULTA_NAO_CONCLUIDA", r["erro"]
            etapas.append(f"[{tipo}] ERRO: {r['erro']}")
            break
        if r["status"] == "CONSULTA_COM_ALERTA" and status_geral != "CONSULTA_NAO_CONCLUIDA":
            etapas.append(f"[{tipo}] {r['erro']}")
        regs_total += r["registros"]
        time.sleep(2)

    consulta = sb_insert("cct_consultas", {
        "tenant_id": tenant, "sindicato_id": sind["id"], "origem": ORIGEM, "status": status_geral, "http": http,
        "qtd_encontrados": len(regs_total), "qtd_novos": 0, "duracao_ms": int((time.time() - t0) * 1000),
        "erro": erro_geral, "etapas": etapas})[0]

    if status_geral == "CONSULTA_NAO_CONCLUIDA":
        incidente(tenant, f"MEDIADOR:consulta:{cnpj}", "MEDIADOR", "ALTO",
                  f"CONSULTA NÃO CONCLUÍDA – erro no acesso ao Mediador para {sind['nome']}: {erro_geral}", sind["id"],
                  detalhes={"etapas": etapas})
        sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"ultima_consulta": consulta["executada_em"], "ultimo_status": status_geral})
        return status_geral, 0

    resolver(tenant, f"MEDIADOR:consulta:{cnpj}")
    for reg in regs_total:
        if reg["registro"] in existentes:
            continue
        log(f"  NOVO registro {reg['registro']} ({reg['tipo']}) — {reg.get('vigencia')}")
        row, ok = importar(tenant, sind, reg, page, consulta["id"])
        existentes.add(reg["registro"])
        novos.append((reg, row, ok))
        time.sleep(1)

    if novos:
        sb_patch("cct_consultas", {"id": f"eq.{consulta['id']}"}, {"qtd_novos": len(novos)})
        for reg, row, ok in novos:
            partes = "<br>".join(p["nome"] if isinstance(p, dict) else p for p in (row.get("partes") or []))
            html = (f"<p><b>{reg['tipo']}</b> registrada no Mediador para <b>{sind['nome']}</b>.</p>"
                    f"<p><b>Registro:</b> {reg['registro']}<br><b>Solicitação:</b> {reg.get('solicitacao')}<br>"
                    f"<b>Vigência:</b> {reg.get('vigencia')}<br><b>Partes:</b><br>{partes}</p>"
                    f"<p><b>Importação:</b> {'concluída' if ok else 'NÃO CONCLUÍDA — verificar na Central de Erros'}</p>")
            st = notificar(tenant, "NOVA_CCT", f"NOVA {reg['tipo'].upper()} – {sind['nome']} – {reg['registro']}", html,
                           instrumento_id=row["id"])
            sb_patch("cct_instrumentos", {"id": f"eq.{row['id']}"},
                     {"status_ciencia": "NOTIFICADO" if st == "ENVIADA" else "PENDENTE"})
    sb_patch("cct_sindicatos", {"id": f"eq.{sind['id']}"}, {"ultima_consulta": consulta["executada_em"], "ultimo_status": status_geral})
    return status_geral, len(novos)


def main():
    log(f"CCT Monitor robô v{VERSAO} — origem {ORIGEM}")
    tenants = sb_get("tenants", {"cnpj": f"eq.{TENANT_CNPJ}", "select": "id,nome"})
    if not tenants:
        log(f"tenant {TENANT_CNPJ} não encontrado — abortando");
        sys.exit(2)
    tenant = tenants[0]["id"]
    sinds = sb_get("cct_sindicatos", {"tenant_id": f"eq.{tenant}", "monitorar": "eq.true", "ativo": "eq.true",
                                      "select": "id,cnpj,nome,tipo,uf", "order": "nome"})
    filtro = os.environ.get("APENAS_CNPJ", "").strip()
    if filtro:
        sinds = [s for s in sinds if s["cnpj"] == filtro]
    log(f"{len(sinds)} sindicato(s) a monitorar")
    existentes = {r["numero_registro"] for r in sb_get("cct_instrumentos", {"tenant_id": f"eq.{tenant}", "select": "numero_registro"})}
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
                if i < len(sinds) - 1:
                    time.sleep(INTERVALO)
        finally:
            browser.close()

    if not sinds:
        incidente(tenant, "APLICATIVO:sem-sindicatos", "APLICATIVO", "ATENCAO", "Execução sem nenhum sindicato monitorado")
    resolver(tenant, "APLICATIVO:execucao-diaria")  # heartbeat: execução chegou ao fim
    log(f"RESUMO: {json.dumps(resumo, ensure_ascii=False)}")
    with open("resumo_execucao.json", "w", encoding="utf-8") as f:
        json.dump({"versao": VERSAO, "origem": ORIGEM, "quando": datetime.now(timezone.utc).isoformat(), **resumo}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
