"""Regras de negócio do robô CCT Monitor — cada teste é uma regra; se falhar, a versão não pode subir.
Executar: pytest -q tests/  (na raiz do repositório)"""
import time
import pytest


# ---------- categoria (v0.18.5) ----------
@pytest.mark.parametrize("entrada,esperado", [
    ("TRABALHADORES NO COMÉRCIO DE ITAJAÍ  ", "Trabalhadores no Comércio de Itajaí"),
    ("Trabalhadores no Comércio de Itajaí.", "Trabalhadores no Comércio de Itajaí"),
    ("empregados no comércio varejista", "empregados no comércio varejista"),   # só maiúsculas totais são reescritas
    ("COMERCIÁRIOS E EMPREGADOS EM EMPRESAS DO RAMO", "Comerciários e Empregados em Empresas do Ramo"),
    ("", None), (None, None), ("   ", None),
])
def test_norm_categoria(mc, entrada, esperado):
    assert mc._norm_categoria(entrada) == esperado


# ---------- IA sem crédito (v0.18.5) ----------
@pytest.mark.parametrize("msg,sem_credito", [
    ("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing", True),
    ("HTTP 400: insufficient_quota", True),
    ("HTTP 402 Payment Required", True),
    ("timeout", False), ("cláusula não encontrada", False), ("", False),
])
def test_deteccao_sem_credito(mc, msg, sem_credito):
    assert bool(mc._RX_SEM_CREDITO.search(msg)) is sem_credito


# ---------- datas do extrato ----------
@pytest.mark.parametrize("entrada,esperado", [
    ("24/08/2026", "2026-08-24"),
    ("01º de agosto de 2026", "2026-08-01"),
    ("1 de setembro de 2026", "2026-09-01"),
    ("31 de Março de 2027", "2027-03-31"),
    ("data inválida", None), ("", None), (None, None),
])
def test_data_br(mc, entrada, esperado):
    assert mc.data_br(entrada) == esperado


# ---------- tipo laboral × patronal ----------
@pytest.mark.parametrize("nome,pos,esperado", [
    ("SINDICATO DOS EMPREGADOS NO COMÉRCIO DE PALHOÇA E REGIÃO", 1, "laboral"),
    ("SINDICATO DO COMÉRCIO ATACADISTA DA REGIÃO DA GRANDE FLORIANÓPOLIS", 0, "patronal"),
    ("SINDICATO DOS TRABALHADORES NAS INDÚSTRIAS DA CONSTRUÇÃO", 1, "laboral"),
    ("FEDERAÇÃO DAS INDÚSTRIAS DO ESTADO DE SANTA CATARINA", 1, "patronal"),
    ("SINDICATO XYZ", 0, "laboral"),     # sem pista: 1ª parte do extrato = laboral
    ("SINDICATO XYZ", 1, "patronal"),
])
def test_inferir_tipo(mc, nome, pos, esperado):
    assert mc.inferir_tipo(nome, pos) == esperado


# ---------- ano do registro (histórico) — corrigido na v0.19.4 ----------
@pytest.mark.parametrize("reg,esperado", [
    ({"registro": "SC002473/2026"}, 2026),
    ({"registro": "MR061922/2026", "vigencia": "01/09/2026 a 31/08/2027"}, 2026),
    ({"registro": "", "vigencia": "01/09/2025 a 31/08/2026"}, 2025),
    ({"registro": None, "vigencia": None}, None),
])
def test_ano_do_registro(mc, reg, esperado):
    assert mc.ano_do_registro(reg) == esperado


# ---------- orçamento de tempo (v0.18.5) ----------
def test_restante_respeita_orcamento_e_reserva(mc, monkeypatch):
    monkeypatch.setattr(mc, "ORCAMENTO_MIN", 100)
    monkeypatch.setattr(mc, "_INICIO_GLOBAL", time.time() - 40 * 60)   # 40 min decorridos
    assert abs(mc.restante(0) - 60 * 60) < 5
    assert abs(mc.restante(12) - 48 * 60) < 5
    monkeypatch.setattr(mc, "_INICIO_GLOBAL", time.time() - 99 * 60)
    assert mc.restante(7) < 0   # sem tempo: a etapa não começa


# ---------- paginação (v0.19.0) ----------
class _Resp:
    def __init__(self, status, dados): self.status_code, self._d = status, dados
    def raise_for_status(self): pass
    def json(self): return self._d

def test_sb_get_all_le_todas_as_paginas(mc, monkeypatch):
    total = 2500
    chamadas = []
    def fake_get(url, headers=None, params=None, timeout=None):
        ini = int(headers["Range"].split("-")[0]); chamadas.append(ini)
        return _Resp(200, [{"id": i} for i in range(ini, min(total, ini + mc.PAGINA))])
    monkeypatch.setattr(mc.requests, "get", fake_get)
    rows = mc.sb_get_all("cct_instrumentos", {"select": "id"})
    assert len(rows) == total and chamadas == [0, 1000, 2000]

def test_sb_get_all_para_no_416(mc, monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        ini = int(headers["Range"].split("-")[0])
        return _Resp(200, [{"id": i} for i in range(1000)]) if ini == 0 else _Resp(416, None)
    monkeypatch.setattr(mc.requests, "get", fake_get)
    assert len(mc.sb_get_all("t", {})) == 1000


# ---------- empresa sem funcionários (v0.19.1) ----------
def test_criar_ciencias_ignora_empresa_sem_funcionarios(mc, monkeypatch):
    monkeypatch.setattr(mc, "prazo_ciencia", lambda t: "2026-10-01")
    monkeypatch.setattr(mc, "sb_get", lambda tab, p: [
        {"empresa": {"id": "e1", "razao_social": "A", "responsavel_email": "a@x", "ativo": True, "sem_funcionarios": False}},
        {"empresa": {"id": "e2", "razao_social": "B", "responsavel_email": "b@x", "ativo": True, "sem_funcionarios": True}},
        {"empresa": {"id": "e3", "razao_social": "C", "responsavel_email": "c@x", "ativo": False}},
    ])
    inseridas = []
    def fake_insert(tab, linhas, upsert_on=None):
        ls = linhas if isinstance(linhas, list) else [linhas]; inseridas.extend(ls); return ls   # como o PostgREST: devolve as linhas criadas
    monkeypatch.setattr(mc, "sb_insert", fake_insert)
    monkeypatch.setattr(mc, "log", lambda *a, **k: None)
    mc.criar_ciencias("t", {"id": "s1", "responsavel_email": None}, {"id": "i1"})
    assert [l["empresa_id"] for l in inseridas] == ["e1"]

def test_criar_ciencias_act_de_empresa_sem_funcionarios_nao_gera(mc, monkeypatch):
    monkeypatch.setattr(mc, "prazo_ciencia", lambda t: "2026-10-01")
    inseridas = []
    def fake_insert(tab, linhas, upsert_on=None):
        ls = linhas if isinstance(linhas, list) else [linhas]; inseridas.extend(ls); return ls
    monkeypatch.setattr(mc, "sb_insert", fake_insert)
    monkeypatch.setattr(mc, "log", lambda *a, **k: None)
    mc.criar_ciencias("t", None, {"id": "i1"}, empresa={"id": "e9", "sem_funcionarios": True, "responsavel_email": "x@x"})
    # sem alvo e sem sindicato: cai na ciência "do sindicato" (empresa_id nulo), nunca na empresa marcada
    assert all(l.get("empresa_id") is None for l in inseridas)


# ---------- retentativa tolerante a falha (v0.19.3) ----------
def test_fila_retentativa_nao_derruba_execucao(mc, monkeypatch):
    def quebra(*a, **k): raise RuntimeError("400 Client Error: Bad Request")
    monkeypatch.setattr(mc, "sb_get", quebra)
    incidentes = []
    monkeypatch.setattr(mc, "incidente", lambda *a, **k: incidentes.append(a[1]))
    monkeypatch.setattr(mc, "log", lambda *a, **k: None)
    devidos, intervalo, maximo = mc.fila_retentativa("t", {"retentar_intervalo_h": 2, "retentar_max_dia": 3})
    assert devidos == [] and maximo == 0 and incidentes == ["APLICATIVO:retentativa"]

def test_fila_retentativa_desligada_quando_maximo_zero(mc, monkeypatch):
    monkeypatch.setattr(mc, "sb_get", lambda *a, **k: pytest.fail("não deve consultar"))
    assert mc.fila_retentativa("t", {"retentar_max_dia": 0}) == ([], 2, 0)


# ---------- fila de IA pausada por falta de crédito ----------
def test_fila_ia_pausada_respeita_6h(mc, monkeypatch):
    from datetime import datetime, timezone, timedelta
    recente = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    antigo = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    monkeypatch.setattr(mc, "log", lambda *a, **k: None)
    monkeypatch.setattr(mc, "sb_get", lambda tab, p: [{"ultima_ocorrencia": recente}])
    assert mc._fila_ia_pausada("t")
    monkeypatch.setattr(mc, "sb_get", lambda tab, p: [{"ultima_ocorrencia": antigo}])
    assert mc._fila_ia_pausada("t") is None
    monkeypatch.setattr(mc, "sb_get", lambda tab, p: [])
    assert mc._fila_ia_pausada("t") is None
