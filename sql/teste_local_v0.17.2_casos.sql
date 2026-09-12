-- casos v0.17.2 — rodar após teste_local_v0.17.0.sql, setup_cct_v0.17.0.sql, setup_cct_v0.17.1.sql, setup_cct_v0.17.2.sql
alter table public.cct_instrumentos add column if not exists data_registro date;
alter table public.cct_instrumentos add column if not exists vigencia_inicio date;
alter table public.cct_incidentes add column if not exists resolvido_em timestamptz;
create or replace function public.cct_resolver_incidente(p_tenant uuid, p_fingerprint text, p_forma text) returns int language plpgsql as $$
declare n int; begin update cct_incidentes set status='RESOLVIDO_AUTOMATICAMENTE', resolvido_em=now() where tenant_id=p_tenant and fingerprint=p_fingerprint and status in ('NOVO','EM_NOVA_TENTATIVA','PERSISTENTE','EM_ANALISE'); get diagnostics n = row_count; return n; end $$;
-- incidente do vigia ANTIGO (fingerprint antigo) + um da fingerprint nova
insert into cct_incidentes(tenant_id,fingerprint,modulo,gravidade,mensagem,status,ocorrencias) values
 ('aaaaaaaa-0000-0000-0000-000000000001','APLICATIVO:heartbeat','APLICATIVO','CRITICO','ROBÔ NÃO EXECUTOU – última consulta em 04/09/2026 17:21 (limite 30 h)','PERSISTENTE',3),
 ('aaaaaaaa-0000-0000-0000-000000000001','APLICATIVO:execucao-diaria','APLICATIVO','CRITICO','ROBÔ NÃO EXECUTOU – execução prevista','NOVO',1),
 ('aaaaaaaa-0000-0000-0000-000000000001','MEDIADOR:consulta:x','MEDIADOR','ALTO','CONSULTA NÃO CONCLUÍDA','NOVO',1);
select set_config('app.situacao','NORMAL',false);
do $$ declare r jsonb; begin
  r := public.cct_verificar_heartbeat(now());
  if (r->>'resolvidos')::int <> 2 then raise exception 'FALHA heartbeat: esperado 2 resolvidos, veio %', r; end if;
  if exists (select 1 from cct_incidentes where modulo='APLICATIVO' and status<>'RESOLVIDO_AUTOMATICAMENTE') then raise exception 'FALHA: incidente antigo continua aberto'; end if;
  if not exists (select 1 from cct_incidentes where fingerprint='MEDIADOR:consulta:x' and status='NOVO') then raise exception 'FALHA: heartbeat fechou incidente que não era dele'; end if;
  raise notice 'OK 1: heartbeat fecha o ROBÔ NÃO EXECUTOU antigo (e só ele + o atual)';
end $$;
-- lote por ano
insert into cct_instrumentos(id,tenant_id,numero_registro,analise_status,data_registro) values
 ('d1000000-0000-0000-0000-000000000011','aaaaaaaa-0000-0000-0000-000000000001','SC011/2026','ANALISE_IA_NAO_CONCLUIDA','2026-03-01'),
 ('d1000000-0000-0000-0000-000000000012','aaaaaaaa-0000-0000-0000-000000000001','SC012/2026',null,'2026-04-01'),
 ('d1000000-0000-0000-0000-000000000013','aaaaaaaa-0000-0000-0000-000000000001','SC013/2025','ANALISE_IA_NAO_CONCLUIDA','2025-04-01'),
 ('d1000000-0000-0000-0000-000000000014','aaaaaaaa-0000-0000-0000-000000000001','SC014/2026','CONCLUIDA','2026-05-01');
update cct_instrumentos set data_registro='2026-01-10' where id in ('d1000000-0000-0000-0000-000000000001','d1000000-0000-0000-0000-000000000002');
select set_config('app.uid','33333333-3333-3333-3333-333333333333',false);
do $$ begin
  begin perform public.cct_pedir_analise_lote('aaaaaaaa-0000-0000-0000-000000000001', 2026); raise exception 'FALHA: operador pediu lote';
  exception when others then if sqlerrm not like 'Somente administrador%' then raise; end if; end;
  raise notice 'OK 2: operador não pede lote';
end $$;
select set_config('app.uid','22222222-2222-2222-2222-222222222222',false);
select set_config('app.uso','5',false);
do $$ declare r jsonb; begin
  r := public.cct_pedir_analise_lote('aaaaaaaa-0000-0000-0000-000000000001', 2026);
  -- 2026 importadas: SC001(CONCLUIDA), SC011(nao concluida), SC012(null), SC014(CONCLUIDA); SC002 não importada; SC013 é 2025
  if (r->>'total_ano')::int <> 4 or (r->>'ja_concluidas')::int <> 2 or (r->>'ja_na_fila')::int <> 1 or (r->>'enfileiradas')::int <> 1 then raise exception 'FALHA lote: %', r; end if;
  if (r->>'cabem_no_limite')::int <> 55 then raise exception 'FALHA limite: %', r; end if;
  if (select analise_status from cct_instrumentos where id='d1000000-0000-0000-0000-000000000011') is not null then raise exception 'FALHA: SC011 não entrou na fila'; end if;
  if (select analise_status from cct_instrumentos where id='d1000000-0000-0000-0000-000000000013') is null then raise exception 'FALHA: 2025 entrou na fila'; end if;
  if (r->>'disparado')::boolean then raise exception 'FALHA: disparou sem token'; end if;
  if r->>'motivo' not like 'GITHUB_DISPATCH_TOKEN%' then raise exception 'FALHA motivo: %', r; end if;
  raise notice 'OK 3: lote 2026 = % (sem token → fila com motivo)', r;
  r := public.cct_pedir_analise_lote('aaaaaaaa-0000-0000-0000-000000000001', 2024);
  if (r->>'enfileiradas')::int <> 0 or r->>'motivo' not like 'nada a analisar%' then raise exception 'FALHA lote vazio: %', r; end if;
  raise notice 'OK 4: ano sem convenções → nada a analisar';
  r := public.cct_pedir_analise_agora('d1000000-0000-0000-0000-000000000014');
  if not (r->>'na_fila')::boolean or (r->>'uso_mes')::int <> 5 or (r->>'disparado')::boolean then raise exception 'FALHA pedido individual: %', r; end if;
  raise notice 'OK 5: pedido individual mantém o contrato da v0.17.1';
  r := public.cct_salvar_config(jsonb_build_object('tenant_id','aaaaaaaa-0000-0000-0000-000000000001','retentar_intervalo_h',3,'retentar_max_dia',99));
  if (r->>'retentar_intervalo_h')::int <> 3 or (r->>'retentar_max_dia')::int <> 10 then raise exception 'FALHA salvar retentativa: %', r; end if;
  raise notice 'OK 6: gerente salva retentativa (máximo limitado a 10)';
end $$;
-- (cct_disparar_robo tem execute revogado de authenticated/public: só as funções security definer o chamam; o harness roda como superusuário e não testa isso)
