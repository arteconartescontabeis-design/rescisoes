// Testes das regras do app CCT Monitor (cct.html) — roda em Node, sem navegador.
// Executar: node tests/test_cct_app.js   (na raiz do repositório)
const fs = require("fs"), path = require("path"), assert = require("assert");
const html = fs.readFileSync(path.join(__dirname, "..", "cct.html"), "utf8");
const js = [...html.matchAll(/<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");

// ---- ambiente mínimo de navegador ----
const store = {};
const el = () => ({ value: "", innerHTML: "", textContent: "", style: {}, disabled: false, classList: { add() {}, remove() {}, toggle() {} },
  addEventListener() {}, querySelectorAll: () => [], focus() {}, setSelectionRange() {}, appendChild() {}, options: [], dataset: {} });
global.window = { location: { href: "" }, addEventListener() {} };
global.document = { getElementById: id => store[id] || (store[id] = el()), querySelectorAll: () => [], querySelector: () => null,
  addEventListener() {}, body: {}, documentElement: {}, activeElement: null, createElement: () => el() };
global.sessionStorage = global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.confirm = () => true; global.alert = () => {}; global.setInterval = () => 0; global.clearInterval = () => 0;
let fetchImpl = async () => ({ ok: true, status: 200, text: async () => "[]", json: async () => ({}) });
global.fetch = (...a) => fetchImpl(...a);

// carrega o app (as funções viram globais)
eval(js + "\n;globalThis.__cct = { dataBR, hojeBR, normCat, catsUnicas, getTodos, fmtCnpj, abrevTipo, renderComFoco, confirmarSenha, senhaConfirmar, marcarSemFunc, consultarAgora, APP, CONFIG };");
const C = globalThis.__cct;

let n = 0, falhas = 0;
async function t(nome, fn) { try { await fn(); n++; console.log("  ok  " + nome); } catch (e) { falhas++; console.log("FALHA " + nome + "\n      " + (e.message || e)); } }

(async () => {
  console.log("Regras do app — cct.html " + C.CONFIG.versao);

  // datas no fuso de Brasília (v0.18.5)
  await t("dataBR: 23:30 em Brasília continua no mesmo dia (UTC já virou)", () => {
    assert.strictEqual(C.dataBR("2026-09-26T23:30:00-03:00"), "2026-09-26");
    assert.strictEqual(new Date("2026-09-26T23:30:00-03:00").toISOString().slice(0, 10), "2026-09-27"); // o erro antigo
  });
  await t("hojeBR devolve AAAA-MM-DD", () => assert.match(C.hojeBR(), /^\d{4}-\d{2}-\d{2}$/));

  // categorias (v0.18.5)
  await t("normCat colapsa espaços", () => assert.strictEqual(C.normCat("  Empregados   no Comércio "), "Empregados no Comércio"));
  await t("catsUnicas não repete a mesma categoria com maiúsculas diferentes e ignora vazias", () => {
    const r = C.catsUnicas(["empregados no comércio varejista", "Empregados no Comércio Varejista", "", null, "Atacado"]);
    assert.deepStrictEqual(r, ["Atacado", "empregados no comércio varejista"]);
  });

  // filtro de vigência (v0.18.5): sem data final não é "vencida"
  await t("vigência: sem data final cai em 'sem_vig', não em 'vencida'", () => {
    const hoje = "2026-09-27";
    const regra = (i, fv) => !fv || (fv === "vigente" ? (i.vigencia_fim || "") >= hoje : fv === "vencida" ? (!!i.vigencia_fim && i.vigencia_fim < hoje) : !i.vigencia_fim);
    assert.strictEqual(regra({ vigencia_fim: null }, "vencida"), false);
    assert.strictEqual(regra({ vigencia_fim: null }, "sem_vig"), true);
    assert.strictEqual(regra({ vigencia_fim: "2026-01-31" }, "vencida"), true);
    assert.strictEqual(regra({ vigencia_fim: "2027-08-31" }, "vigente"), true);
  });

  // paginação (v0.19.0)
  await t("getTodos lê 2.500 linhas em 3 páginas", async () => {
    const chamadas = [];
    fetchImpl = async (url, opt) => { const ini = +((opt.headers || {}).Range || "0-").split("-")[0]; chamadas.push(ini);
      const arr = []; for (let i = ini; i < Math.min(2500, ini + 1000); i++) arr.push({ id: i });
      return { ok: true, status: 200, text: async () => JSON.stringify(arr) }; };
    C.APP.token = "x";
    const r = await C.getTodos("cct_instrumentos?select=id");
    assert.strictEqual(r.length, 2500); assert.deepStrictEqual(chamadas, [0, 1000, 2000]);
  });

  // formatação
  await t("fmtCnpj", () => assert.strictEqual(C.fmtCnpj("14646445000158"), "14.646.445/0001-58"));
  await t("abrevTipo", () => { assert.strictEqual(C.abrevTipo("Termo Aditivo a Convenção"), "TA"); assert.strictEqual(C.abrevTipo("Acordo Coletivo"), "ACT"); assert.strictEqual(C.abrevTipo("Convenção Coletiva"), "CCT"); });

  // foco da busca (v0.19.2)
  await t("renderComFoco devolve o foco e o cursor ao campo ativo", () => {
    const campo = el(); campo.id = "q_sind"; campo.selectionStart = 3; let focado = false, pos = null;
    campo.focus = () => { focado = true; }; campo.setSelectionRange = (a) => { pos = a; };
    store["q_sind"] = campo; document.activeElement = campo; global.window.renderSindFake = () => {};
    C.renderComFoco("renderSindFake");
    assert.ok(focado); assert.strictEqual(pos, 3);
  });

  // senha (v0.19.1): errada não passa, certa passa; nada gravado antes
  await t("marcarSemFunc só chama a RPC após senha correta", async () => {
    const chamadas = [];
    fetchImpl = async (url, opt) => { chamadas.push(url);
      if (url.includes("auth/v1/token")) { const b = JSON.parse(opt.body); return { ok: b.password === "certa", status: 200, json: async () => b.password === "certa" ? { access_token: "t" } : { msg: "bad" } }; }
      if (url.includes("rpc/cct_marcar_sem_funcionarios")) return { ok: true, status: 200, text: async () => JSON.stringify({ alterado: true, ciencias_canceladas: 0 }) };
      return { ok: true, status: 200, text: async () => "[]" }; };
    C.APP.email = "u@x"; C.APP.nivel = "gerente"; C.APP.dados = { emp: [{ id: "e1", razao_social: "Emp" }], sind: [], inst: [] };
    globalThis.carregarTudo = async () => {};
    const p = C.marcarSemFunc("e1", true);
    document.getElementById("mSenhaIn").value = "errada"; await C.senhaConfirmar();
    assert.ok(!chamadas.some(u => u.includes("cct_marcar_sem_funcionarios")), "não pode gravar com senha errada");
    document.getElementById("mSenhaIn").value = "certa"; await C.senhaConfirmar(); await p.catch(() => {});
    assert.ok(chamadas.some(u => u.includes("cct_marcar_sem_funcionarios")));
  });

  console.log(`\n${n} ok, ${falhas} falha(s)`);
  process.exit(falhas ? 1 : 0);
})();
