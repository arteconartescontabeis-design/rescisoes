"""Ambiente mínimo para importar o robô sem Supabase, Playwright ou rede."""
import os, sys, types, importlib.util
os.environ.setdefault("SUPABASE_URL", "http://supabase.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "chave-de-teste")
os.environ.setdefault("TENANT_CNPJ", "79876769000128")
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "robo"))
for m in ("mediador", "extrair_cct", "analisar_cct", "playwright", "playwright.sync_api", "bs4"):
    sys.modules.setdefault(m, types.ModuleType(m))
sys.modules["extrair_cct"].extrair = lambda p: None
sys.modules["playwright.sync_api"].sync_playwright = lambda: None

import pytest

@pytest.fixture(scope="session")
def mc():
    spec = importlib.util.spec_from_file_location("monitor_cct", os.path.join(RAIZ, "robo", "monitor_cct.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
