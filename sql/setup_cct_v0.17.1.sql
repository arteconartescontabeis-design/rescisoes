-- =====================================================================
--  CCT MONITOR — setup v0.17.1 (parecer por IA "na hora")
--  Incremental sobre v0.17.0. Idempotente. Validado em PostgreSQL 16 local em 11/09/2026.
--  O que faz:
--   1. cct_config.github_repo / github_branch (críticas: superadmin) e ultimo_pedido_ia.
--   2. cct_pedir_analise_agora(instrumento): coloca a convenção na fila (analise_status nulo) e DISPARA o robô no GitHub
--      Actions (workflow_dispatch, inputs forcar=true e so_analise=true) via pg_net, com o token guardado no Vault
--      (GITHUB_DISPATCH_TOKEN). Disparos com menos de 2 min de intervalo são agrupados (o mesmo robô atende a fila toda).
--   3. cct_salvar_config aceita github_repo/github_branch (só superadmin).
--  Pós-instalação (uma vez): select vault.create_secret('<token fine-grained com Actions: Read and write no repositório>', 'GITHUB_DISPATCH_TOKEN');
-- =====================================================================
do $$ begin
  if not exists (select 1 from pg_proc where proname='cct_excluir_usuario') then raise exception 'Execute antes o setup_cct_v0.17.0.sql'; end if;
end $$;

alter table public.cct_config add column if not exists github_repo text not null default 'arteconartescontabeis-design/rescisoes';
alter table public.cct_config add column if not exists github_branch text not null default 'main';
alter table public.cct_config add column if not exists ultimo_pedido_ia timestamptz;

create or replace function public.cct_salvar_config(p jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_nivel text := public.cct_nivel(); v_tenant uuid; criticas text[] := array['ia_ativa','ia_limite_mes','ia_historico','horarios_consulta','intervalo_consultas_s','max_sindicatos_por_execucao','github_repo','github_branch'];
        k text; atual jsonb; mudou_critica boolean := false;
begin
  if v_nivel not in ('gerente','superadmin') then raise exception 'Somente administrador ou superadministrador altera configurações.'; end if;
  v_tenant := (p ->> 'tenant_id')::uuid;
  if v_tenant not in (select public.cct_tenants_do_usuario()) then raise exception 'Escritório inválido.'; end if;
  insert into cct_config (tenant_id) values (v_tenant) on conflict (tenant_id) do nothing;
  select to_jsonb(c) into atual from cct_config c where tenant_id = v_tenant;
  foreach k in array criticas loop
    if p ? k and (p -> k) is distinct from (atual -> k) then mudou_critica := true; end if;
  end loop;
  if mudou_critica and v_nivel <> 'superadmin' then raise exception 'IA, horários/fila de consultas, repositório do robô e limpeza são configurações críticas: somente o superadministrador altera.'; end if;
  update cct_config set
    prazo_ciencia_dias_uteis = coalesce((p ->> 'prazo_ciencia_dias_uteis')::int, prazo_ciencia_dias_uteis),
    lembrete_diario = coalesce((p ->> 'lembrete_diario')::boolean, lembrete_diario),
    escalonar_apos_prazo = coalesce((p ->> 'escalonar_apos_prazo')::boolean, escalonar_apos_prazo),
    email_destino_teste = case when p ? 'email_destino_teste' then nullif(p ->> 'email_destino_teste', '') else email_destino_teste end,
    email_erros_criticos = case when p ? 'email_erros_criticos' then (select string_agg(lower(trim(e)), ', ') from regexp_split_to_table(coalesce(p ->> 'email_erros_criticos', ''), '[,;\s]+') e where trim(e) <> '') else email_erros_criticos end,
    ciencia_inicio = case when p ? 'ciencia_inicio' then nullif(p ->> 'ciencia_inicio', '')::date else ciencia_inicio end,
    historico_anos = coalesce((p ->> 'historico_anos')::int, historico_anos),
    historico_max_por_execucao = coalesce((p ->> 'historico_max_por_execucao')::int, historico_max_por_execucao),
    historico_automatico = coalesce((p ->> 'historico_automatico')::boolean, historico_automatico),
    ia_ativa = coalesce((p ->> 'ia_ativa')::boolean, ia_ativa),
    ia_limite_mes = coalesce((p ->> 'ia_limite_mes')::int, ia_limite_mes),
    ia_historico = coalesce((p ->> 'ia_historico')::boolean, ia_historico),
    horarios_consulta = coalesce(p ->> 'horarios_consulta', horarios_consulta),
    intervalo_consultas_s = coalesce((p ->> 'intervalo_consultas_s')::int, intervalo_consultas_s),
    max_sindicatos_por_execucao = coalesce((p ->> 'max_sindicatos_por_execucao')::int, max_sindicatos_por_execucao),
    github_repo = coalesce(nullif(trim(p ->> 'github_repo'), ''), github_repo),
    github_branch = coalesce(nullif(trim(p ->> 'github_branch'), ''), github_branch),
    updated_at = now()
  where tenant_id = v_tenant;
  select to_jsonb(c) into atual from cct_config c where tenant_id = v_tenant;
  return atual;
end $$;
grant execute on function public.cct_salvar_config(jsonb) to authenticated;

-- fila + disparo do robô (só análise): retorna {na_fila, disparado, motivo, uso_mes}
create or replace function public.cct_pedir_analise_agora(p_instrumento uuid) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_nivel text := public.cct_nivel(); i record; c record; v_key text; v_disp boolean := false; v_motivo text; v_uso int := 0;
begin
  if v_nivel not in ('usuario','gerente','superadmin') then raise exception 'Somente a equipe do escritório solicita análise.'; end if;
  select id, tenant_id, numero_registro, status_importacao into i from cct_instrumentos where id = p_instrumento;
  if i.id is null or i.tenant_id not in (select public.cct_tenants_do_usuario()) then raise exception 'Convenção não encontrada.'; end if;
  if i.status_importacao <> 'IMPORTADO' then raise exception 'A convenção ainda não foi importada — não há texto para analisar.'; end if;
  select * into c from cct_config where tenant_id = i.tenant_id;
  if coalesce(c.ia_ativa, true) = false then raise exception 'O parecer por IA está desligado em Configurações.'; end if;
  begin execute 'select public.cct_ia_uso_mes($1)' into v_uso using i.tenant_id; exception when others then v_uso := 0; end;
  if v_uso >= coalesce(c.ia_limite_mes, 60) then raise exception 'Limite mensal de pareceres atingido (%/%).', v_uso, coalesce(c.ia_limite_mes, 60); end if;
  update cct_instrumentos set analise_status = null where id = i.id;   -- entra na fila
  if c.ultimo_pedido_ia is not null and c.ultimo_pedido_ia > now() - interval '2 minutes' then
    v_motivo := 'robô já disparado há menos de 2 min — esta convenção entra na mesma rodada';
    return jsonb_build_object('na_fila', true, 'disparado', true, 'agrupado', true, 'motivo', v_motivo, 'uso_mes', v_uso);
  end if;
  begin
    execute 'select decrypted_secret from vault.decrypted_secrets where name = $1 limit 1' into v_key using 'GITHUB_DISPATCH_TOKEN';
  exception when others then v_key := null; end;
  if v_key is null then v_motivo := 'GITHUB_DISPATCH_TOKEN não está no Vault do Supabase — o parecer sai na próxima execução agendada';
  elsif not exists (select 1 from pg_extension where extname = 'pg_net') then v_motivo := 'extensão pg_net não habilitada — o parecer sai na próxima execução agendada';
  else
    begin
      execute 'select net.http_post($1, $2, ''{}''::jsonb, $3, 15000)'
        using 'https://api.github.com/repos/' || c.github_repo || '/actions/workflows/monitor-cct.yml/dispatches',
              jsonb_build_object('ref', c.github_branch, 'inputs', jsonb_build_object('forcar', 'true', 'so_analise', 'true')),
              jsonb_build_object('Accept', 'application/vnd.github+json', 'Authorization', 'Bearer ' || v_key, 'X-GitHub-Api-Version', '2022-11-28',
                                 'User-Agent', 'cct-monitor-supabase', 'Content-Type', 'application/json');
      v_disp := true;
      update cct_config set ultimo_pedido_ia = now() where tenant_id = i.tenant_id;
    exception when others then v_motivo := 'falha ao disparar o robô: ' || sqlerrm || ' — o parecer sai na próxima execução agendada'; end;
  end if;
  return jsonb_build_object('na_fila', true, 'disparado', v_disp, 'agrupado', false, 'motivo', v_motivo, 'uso_mes', v_uso);
end $$;
grant execute on function public.cct_pedir_analise_agora(uuid) to authenticated;

do $$ begin
  if not exists (select 1 from information_schema.columns where table_name='cct_config' and column_name='github_repo') then raise exception 'FALHA: cct_config.github_repo'; end if;
  if not exists (select 1 from pg_proc where proname='cct_pedir_analise_agora') then raise exception 'FALHA: cct_pedir_analise_agora'; end if;
  raise notice 'VERIFICAÇÃO v0.17.1 OK';
end $$;
