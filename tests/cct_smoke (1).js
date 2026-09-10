// Testes mínimos do cct.html (ponto crítico 08): abre, sintaxe, funções essenciais existem, telas renderizam com dados vazios
// e com dados de exemplo sem lançar exceção. Uso: node tests/cct_smoke.js  (precisa de `npm i jsdom` no CI)
const fs = require("fs"), path = require("path");
const { JSDOM } = require("jsdom");
const html = fs.readFileSync(path.join(__dirname, "..", "cct.html"), "utf8");
let falhas = 0;
const ok = m => console.log("  OK ", m), falha = m => { console.log("  FALHA", m); falhas++; };

console.log("1) sintaxe do script");
try { new Function(html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"))); ok("JS válido"); } catch (e) { falha("JS: " + e.message); process.exit(1); }

console.log("2) carregar no DOM");
const dom = new JSDOM(html, { runScripts: "dangerously", url: "https://localhost/cct.html", beforeParse(w) { w.fetch = () => Promise.reject(new Error("sem rede no teste")); } });
const w = dom.window, d = w.document;
const T = id => (d.getElementById(id) || { textContent: "" }).textContent.replace(/\s+/g, " ").trim();
ok("página carregada");

console.log("3) elementos e funções essenciais");
for (const id of ["login", "versao", "tabSind", "tabEmp", "tabInst", "tabConf", "tabExec", "tabCons", "tabInc", "cfgPrazo", "cfgCienciaIni", "eBusca", "btnMonTodos"]) (d.getElementById(id) ? ok : falha)("#" + id);
for (const fn of ["iniciar", "carregarTudo", "renderPainel", "renderSind", "renderEmp", "renderInst", "renderConf", "renderExec", "renderCons", "renderInc", "renderDest", "renderNotif", "abrirEmp", "salvarEmp", "vincSind", "renderBuscaSind", "salvarConfig", "dispensarAnteriores", "monitorarTodos", "pedirHistoricoTodos"])
  (typeof w[fn] === "function" ? ok : falha)(fn + "()");

console.log("4) renderizações com dados vazios");
w.eval('APP.nivel="superadmin";APP.email="t@x";APP.dados={sind:[],inst:[],cons:[],inc:[],dest:[],notif:[],emp:[],conf:[],cfg:{},exec:[],saude:null,aud:[],usu:[],acessos:[]}');
for (const fn of ["renderPainel", "renderSind", "renderEmp", "renderInst", "renderConf", "renderExec", "renderCons", "renderInc", "renderDest", "renderNotif"]) {
  try { w.eval(fn + "()"); ok(fn); } catch (e) { falha(fn + ": " + e.message); }
}

console.log("5) renderizações com dados de exemplo");
w.eval(`APP.dados={sind:[{id:"s1",nome:"Sind Comércio",cnpj:"84307370000166",tipo:"laboral",monitorar:true,ativo:true}],
 inst:[{id:"i1",numero_registro:"SC1/2026",tipo:"Convenção Coletiva",sindicato_id:"s1",sindicato:{nome:"Sind Comércio"},vigencia_inicio:"2026-01-01",vigencia_fim:"2026-12-31",data_registro:"2026-09-01",detectado_em:"2026-09-02T10:00:00Z",status_importacao:"IMPORTADO",status_ciencia:"DISPENSADA",total_clausulas:30}],
 cons:[{id:"c1",executada_em:"2026-09-10T09:00:00Z",origem:"github-actions",status:"CONSULTA_CONFIRMADA",qtd_encontrados:1,qtd_novos:0,duracao_ms:800,etapas:[],sindicato:{nome:"Sind",cnpj:"84307370000166"}}],
 inc:[{id:"n1",modulo:"APLICATIVO",gravidade:"CRITICO",status:"NOVO",ocorrencias:1,mensagem:"x",ultima_ocorrencia:"2026-09-10T09:00:00Z",primeira_ocorrencia:"2026-09-10T09:00:00Z"}],
 dest:[],notif:[{created_at:"2026-09-10T09:00:00Z",tipo:"ERRO",assunto:"a",destinatarios:["g@x"],status:"ENVIADA"}],
 emp:[{id:"e1",cnpj:"11222333000144",razao_social:"Empresa X",ativo:true,vinculos:[{id:"v1",sindicato_id:"s1",papel:"patronal"}]}],
 conf:[{id:"cf1",instrumento_id:"i1",status:"PENDENTE",prazo:"2026-09-12",situacao:"NO_PRAZO",empresa:"Empresa X",sindicato:"Sind",numero_registro:"SC1/2026",tipo_instrumento:"CCT"}],
 cfg:{prazo_ciencia_dias_uteis:2,lembrete_diario:true,escalonar_apos_prazo:true,ciencia_inicio:"2026-09-01",horarios_consulta:"06:00"},
 exec:[{prevista_em:"2026-09-10T09:00:00Z",inicio:"2026-09-10T09:05:00Z",fim:"2026-09-10T09:12:00Z",duracao_s:420,sindicatos_previstos:1,sindicatos_processados:1,novos:0,alertas:0,erros:0,resultado:"SUCESSO"}],
 saude:{situacao:"NORMAL",ultima_prevista:"2026-09-10T09:00:00Z",ultima_realizada:"2026-09-10T09:05:00Z",proxima_prevista:"2026-09-11T09:00:00Z"},aud:[],usu:[],acessos:[]}`);
for (const fn of ["renderPainel", "renderSind", "renderEmp", "renderInst", "renderConf", "renderExec", "renderCons", "renderInc", "renderDest", "renderNotif"]) {
  try { w.eval(fn + "()"); ok(fn); } catch (e) { falha(fn + ": " + e.message); }
}
(T("tabEmp").includes("Sind Comércio (Patronal)") ? ok : falha)("Empresas mostra sindicato com papel");
(T("saude").includes("normal") ? ok : falha)("Painel mostra situação do robô");
(T("tabExec").includes("sucesso") ? ok : falha)("Histórico do Robô lista a execução");
try { w.eval('abrirEmp("e1");vincSind("s1")'); ok("modal da empresa abre"); } catch (e) { falha("abrirEmp: " + e.message); }

console.log("6) versão");
const badge = (html.match(/versao:\s*"v([\d.]+)"/) || [])[1], topo = (html.match(/<tr><td>v([\d.]+)<\/td><td>[^<]*<\/td><td>/) || [])[1];
(badge && badge === topo ? ok : falha)(`badge v${badge} = changelog v${topo}`);

console.log(); if (falhas) { console.log(`REPROVADO: ${falhas} falha(s)`); process.exit(1); } console.log("APROVADO: cct.html pronto para publicar");
