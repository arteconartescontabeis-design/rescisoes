#!/usr/bin/env python3
"""Testes mínimos do robô CCT Monitor (ponto crítico 08) — roda no CI a cada alteração, antes de chegar em produção.
1. Compila todos os .py de robo/.
2. Procura nomes usados e nunca definidos (o NameError da v0.14.0 seria pego aqui).
3. Importa monitor_cct com ambiente falso: erros de import/módulo aparecem sem consultar nada.
4. Confere que a versão do robô e a do cct.html (badge + 1ª linha do changelog) estão coerentes.
Uso: python tests/verificar_robo.py   (a partir da raiz do repositório)
"""
import ast, builtins, os, py_compile, re, sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROBO = os.path.join(RAIZ, "robo")
falhas = []

def ok(msg): print("  OK ", msg)
def falha(msg): print("  FALHA", msg); falhas.append(msg)

print("1) compilação")
arquivos = sorted(f for f in os.listdir(ROBO) if f.endswith(".py"))
for f in arquivos:
    try:
        py_compile.compile(os.path.join(ROBO, f), doraise=True); ok(f)
    except Exception as e:
        falha(f"{f}: {e}")

print("2) nomes não definidos")
for f in arquivos:
    src = open(os.path.join(ROBO, f), encoding="utf-8").read()
    tree = ast.parse(src)
    defs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    stores = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    imports = {(a.asname or a.name.split(".")[0]) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    args = {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args + n.kwonlyargs + n.posonlyargs + ([n.vararg] if n.vararg else []) + ([n.kwarg] if n.kwarg else [])}
    exc = {n.name for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name}
    globais = {n.id for g in ast.walk(tree) if isinstance(g, ast.Global) for n in [] } | {x for g in ast.walk(tree) if isinstance(g, ast.Global) for x in g.names}
    usados = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    indef = usados - defs - stores - imports - args - exc - globais - set(dir(builtins))
    if indef: falha(f"{f}: {sorted(indef)}")
    else: ok(f"{f}: nenhum")

print("3) import do robô com ambiente falso")
os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "chave-falsa")
sys.path.insert(0, ROBO)
for mod in ("mediador", "analisar_cct", "extrair_cct", "monitor_cct"):
    try:
        __import__(mod); ok(mod)
    except ModuleNotFoundError as e:
        if e.name in ("playwright", "bs4", "lxml", "requests"):
            falha(f"{mod}: dependência ausente no CI ({e.name}) — instale no workflow")
        else:
            falha(f"{mod}: {e}")
    except Exception as e:
        falha(f"{mod}: {type(e).__name__}: {e}")

print("4) coerência de versões")
try:
    import monitor_cct
    html = open(os.path.join(RAIZ, "cct.html"), encoding="utf-8").read()
    badge = re.search(r'versao:\s*"v([\d.]+)"', html).group(1)
    topo = re.search(r"<tr><td>v([\d.]+)</td><td>[^<]*</td><td>", html).group(1)
    if badge != topo: falha(f"cct.html: badge v{badge} ≠ primeira linha do changelog v{topo}")
    else: ok(f"cct.html: badge e changelog em v{badge}")
    if badge.split(".")[:2] != monitor_cct.VERSAO.split(".")[:2]:
        print(f"  AVISO robô v{monitor_cct.VERSAO} e app v{badge} em séries diferentes (permitido, mas confira)")
    else: ok(f"robô v{monitor_cct.VERSAO} compatível com app v{badge}")
except Exception as e:
    falha(f"versões: {e}")

print()
if falhas:
    print(f"REPROVADO: {len(falhas)} falha(s)"); sys.exit(1)
print("APROVADO: robô pronto para publicar")
