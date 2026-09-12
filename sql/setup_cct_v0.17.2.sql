-- =====================================================================
--  CCT MONITOR — setup v0.17.2 (Central de Erros: contexto e retentativa; heartbeat fecha o incidente antigo; IA em lote por ano)
--  Incremental sobre v0.17.1. Idempotente. Validado em PostgreSQL 16 local em 12/09/2026.
--  O que faz:
--   1. cct_config.retentar_intervalo_h (padrão 2) e retentar_max_dia (padrão 3): o robô refaz, ao longo do dia, SÓ o que deu erro
--      (consulta/download/importação/armazenamento), a cada N horas, até M tentativas por item por dia; esgotado, o incidente vira CRÍTICO.
--   2. cct_incidentes.tentativas_dia / tentativas_data / ultima_tentativa / escalado_em — contadores da retentativa (o app mostra
--      "tentativa X de Y, próxima às HH:MM").
--   3. cct_verificar_heartbeat: ao constatar o robô em dia, fecha também os incidentes "ROBÔ NÃO EXECUTOU" abertos pelo vigia
--      ANTIGO (fingerprint diferente da atual) — era o motivo do CRÍTICO ficar aberto depois de o robô voltar.
--   4. cct_disparar_robo(tenant, inputs): dispara o workflow no GitHub (pg_net + GITHUB_DISPATCH_TOKEN no Vault); reaproveitado por
--      cct_pedir_analise_agora (comportamento idêntico à v0.17.1) e pela nova cct_pedir_analise_lote(tenant, ano), que coloca na fila
--      TODAS as convenções importadas do ano sem parecer concluído e dispara o robô — o lote é processado aos poucos (limite de tempo
--      por execução), continuando nas execuções seguintes.
--   5. cct_salvar_config aceita os dois parâmetros de retentativa (operacionais: administrador altera).
-- =====================================================================
do $$ begin
  if not exists (select 1 from pg_proc where proname='cct_pedir_analise_agora') then raise exception 'Execute antes o setup_cct_v0.17.1.sql'; end if;
end $$;

-- ---------- 1. parâmetros ----------
alter table public.cct_config add column if not exists retentar_intervalo_h int not null default 2;
alter table public.cct_config add column if not exists retentar_max_dia int not null default 3;

-- ---------- 2. contadores da retentativa ----------
alter table public.cct_incidentes add column if not exists tentativas_dia int not null default 0;
alter table public.cct_incidentes add column if not exists tentativas_data date;
alter table public.cct_incidentes add column if not exists ultima_tentativa timestamptz;
alter table public.cct_incidentes add column if not exists escalado_em timestamptz;

-- ---------- 3. heartbeat (v0.17.0 + fechamento dos incidentes do vigia antigo) ----------
create or replace function public.cct_verificar_heartbeat(p_ref timestamptz default now()) returns jsonb
language plpgsql security definer set search_path = public as $$
declare t record; s jsonb; v_inc uuid; v_abertos int; n_alerta int := 0; n_res int := 0; e record; v_dest text[]; v_msg text; f record;
        abertos text[] := array['NOVO','EM_NOVA_TENTATIVA','PERSISTENTE','EM_ANALISE'];
begin
  for t in select tenant_id from cct_config loop
    s := public.cct_saude_robo(t.tenant_id, p_ref);
    select array_agg(x) into v_dest from public.cct_emails_erros_criticos(t.tenant_id) x;
    if s ->> 'situacao' = 'ATRASADO' then
      select count(*) into v_abertos from cct_incidentes where tenant_id = t.tenant_id and fingerprint = 'APLICATIVO:execucao-diaria' and status = any(abertos);
      if v_abertos = 0 then
        v_msg := format('ROBÔ NÃO EXECUTOU – execução prevista para %s não iniciou até %s (tolerância %s min). Última realizada: %s.',
                        to_char((s ->> 'ultima_prevista')::timestamptz at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'),
                        to_char(p_ref at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'), (s ->> 'tolerancia_min')::int,
                        coalesce(to_char((s ->> 'ultima_realizada')::timestamptz at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'), 'nunca'));
        v_inc := public.cct_registrar_incidente(t.tenant_id, 'APLICATIVO:execucao-diaria', 'APLICATIVO', 'CRITICO', v_msg, null, null,
                   s || jsonb_build_object('contexto', jsonb_build_object('etapa', 'vigia do banco (heartbeat)', 'referencia', 'agenda de execução do robô')));
        perform public.cct_email_db(t.tenant_id, v_dest, 'Artecon · CCT Monitor — ROBÔ NÃO EXECUTOU', '<p>' || v_msg || '</p><p>Verifique em GitHub → Actions → CCT Monitor. Este aviso foi enviado pelo banco de dados (vigia independente do robô).</p>');
        n_alerta := n_alerta + 1;
      end if;
    elsif s ->> 'situacao' in ('NORMAL', 'COM_ALERTAS', 'EXECUTANDO') then
      n_res := n_res + coalesce(public.cct_resolver_incidente(t.tenant_id, 'APLICATIVO:execucao-diaria', 'automatico'), 0);
      -- v0.17.2: incidentes "ROBÔ NÃO EXECUTOU" abertos pelo vigia antigo (v0.3.0, fingerprint diferente) também são fechados
      for f in select distinct fingerprint from cct_incidentes
                where tenant_id = t.tenant_id and modulo = 'APLICATIVO' and status = any(abertos)
                  and fingerprint <> 'APLICATIVO:execucao-diaria' and upper(mensagem) like 'ROBÔ NÃO EXECUTOU%' loop
        n_res := n_res + coalesce(public.cct_resolver_incidente(t.tenant_id, f.fingerprint, 'automatico'), 0);
      end loop;
    end if;
    for e in select * from cct_execucoes where tenant_id = t.tenant_id and resultado in ('INTERROMPIDA','ERRO_TOTAL') and not alertado order by inicio loop
      v_msg := format('EXECUÇÃO %s – iniciada %s (%s): %s', e.resultado, to_char(e.inicio at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'),
                      coalesce(e.origem, '?'), coalesce(e.motivo, 'sem motivo registrado'));
      perform public.cct_registrar_incidente(t.tenant_id, 'APLICATIVO:execucao:' || e.id::text, 'APLICATIVO', 'CRITICO', v_msg, null, null,
                to_jsonb(e) || jsonb_build_object('contexto', jsonb_build_object('etapa', 'execução do robô no GitHub Actions', 'referencia', coalesce(e.run_url, ''))));
      perform public.cct_email_db(t.tenant_id, v_dest, 'Artecon · CCT Monitor — Execução ' || lower(e.resultado),
                                  '<p>' || v_msg || '</p>' || coalesce('<p><a href="' || e.run_url || '">Abrir o log no GitHub</a></p>', ''));
      update cct_execucoes set alertado = true where id = e.id;
      n_alerta := n_alerta + 1;
    end loop;
  end loop;
  return jsonb_build_object('alertas', n_alerta, 'resolvidos', n_res, 'ref', p_ref);
end $$;

-- ---------- 4. disparo do robô (reaproveitado) ----------
create or replace function public.cct_disparar_robo(p_tenant uuid, p_inputs jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare c record; v_key text; v_motivo text;
begin
  select * into c from cct_config where tenant_id = p_tenant;
  if c.ultimo_pedido_ia is not null and c.ultimo_pedido_ia > now() - interval '2 minutes' then
    return jsonb_build_object('disparado', true, 'agrupado', true, 'motivo', 'robô já disparado há menos de 2 min — o pedido entra na mesma rodada');
  end if;
  begin
    execute 'select decrypted_secret from vault.decrypted_secrets where name = $1 limit 1' into v_key using 'GITHUB_DISPATCH_TOKEN';
  exception when others then v_key := null; end;
  if v_key is null then return jsonb_build_object('disparado', false, 'agrupado', false, 'motivo', 'GITHUB_DISPATCH_TOKEN não está no Vault do Supabase — o pedido sai na próxima execução agendada'); end if;
  if not exists (select 1 from pg_extension where extname = 'pg_net') then return jsonb_build_object('disparado', false, 'agrupado', false, 'motivo', 'extensão pg_net não habilitada — o pedido sai na próxima execução agendada'); end if;
  begin
    execute 'select net.http_post($1, $2, ''{}''::jsonb, $3, 15000)'
      using 'https://api.github.com/repos/' || c.github_repo || '/actions/workflows/monitor-cct.yml/dispatches',
            jsonb_build_object('ref', c.github_branch, 'inputs', p_inputs),
            jsonb_build_object('Accept', 'application/vnd.github+json', 'Authorization', 'Bearer ' || v_key, 'X-GitHub-Api-Version', '2022-11-28',
                               'User-Agent', 'cct-monitor-supabase', 'Content-Type', 'application/json');
    update cct_config set ultimo_pedido_ia = now() where tenant_id = p_tenant;
    return jsonb_build_object('disparado', true, 'agrupado', false, 'motivo', null);
  exception when others then
    return jsonb_build_object('disparado', false, 'agrupado', false, 'motivo', 'falha ao disparar o robô: ' || sqlerrm || ' — o pedido sai na próxima execução agendada');
  end;
end $$;
revoke all on function public.cct_disparar_robo(uuid, jsonb) from public, authenticated;

-- pedido individual: mesmo contrato da v0.17.1 ({na_fila, disparado, agrupado, motivo, uso_mes})
create or replace function public.cct_pedir_analise_agora(p_instrumento uuid) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_nivel text := public.cct_nivel(); i record; c record; v_uso int := 0; d jsonb;
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
  d := public.cct_disparar_robo(i.tenant_id, jsonb_build_object('forcar', 'true', 'so_analise', 'true'));
  return d || jsonb_build_object('na_fila', true, 'uso_mes', v_uso);
end $$;
grant execute on function public.cct_pedir_analise_agora(uuid) to authenticated;

-- lote por ano: {total_ano, ja_concluidas, ja_na_fila, enfileiradas, uso_mes, limite_mes, cabem_no_limite, disparado, agrupado, motivo}
create or replace function public.cct_pedir_analise_lote(p_tenant uuid, p_ano int) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_nivel text := public.cct_nivel(); c record; v_uso int := 0; n_total int; n_ok int; n_fila int; n_enf int; d jsonb;
begin
  if v_nivel not in ('gerente','superadmin') then raise exception 'Somente administrador ou superadministrador pede análise em lote.'; end if;
  if p_tenant not in (select public.cct_tenants_do_usuario()) then raise exception 'Escritório inválido.'; end if;
  select * into c from cct_config where tenant_id = p_tenant;
  if coalesce(c.ia_ativa, true) = false then raise exception 'O parecer por IA está desligado em Configurações.'; end if;
  begin execute 'select public.cct_ia_uso_mes($1)' into v_uso using p_tenant; exception when others then v_uso := 0; end;
  select count(*), count(*) filter (where analise_status = 'CONCLUIDA'), count(*) filter (where analise_status is null)
    into n_total, n_ok, n_fila
    from cct_instrumentos
   where tenant_id = p_tenant and status_importacao = 'IMPORTADO'
     and extract(year from coalesce(data_registro, vigencia_inicio))::int = p_ano;
  update cct_instrumentos set analise_status = null
   where tenant_id = p_tenant and status_importacao = 'IMPORTADO' and analise_status is distinct from 'CONCLUIDA' and analise_status is not null
     and extract(year from coalesce(data_registro, vigencia_inicio))::int = p_ano;
  get diagnostics n_enf = row_count;
  if n_enf + n_fila = 0 then
    return jsonb_build_object('total_ano', n_total, 'ja_concluidas', n_ok, 'ja_na_fila', n_fila, 'enfileiradas', 0, 'uso_mes', v_uso,
                              'limite_mes', coalesce(c.ia_limite_mes, 60), 'cabem_no_limite', greatest(coalesce(c.ia_limite_mes, 60) - v_uso, 0),
                              'disparado', false, 'agrupado', false, 'motivo', 'nada a analisar: todas as convenções do ano já têm parecer concluído');
  end if;
  d := public.cct_disparar_robo(p_tenant, jsonb_build_object('forcar', 'true', 'so_analise', 'true'));
  return d || jsonb_build_object('total_ano', n_total, 'ja_concluidas', n_ok, 'ja_na_fila', n_fila, 'enfileiradas', n_enf, 'uso_mes', v_uso,
                                 'limite_mes', coalesce(c.ia_limite_mes, 60), 'cabem_no_limite', greatest(coalesce(c.ia_limite_mes, 60) - v_uso, 0));
end $$;
grant execute on function public.cct_pedir_analise_lote(uuid, int) to authenticated;

-- ---------- 5. salvar_config (v0.17.1 + retentativa) ----------
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
    retentar_intervalo_h = greatest(1, least(12, coalesce((p ->> 'retentar_intervalo_h')::int, retentar_intervalo_h))),
    retentar_max_dia = greatest(0, least(10, coalesce((p ->> 'retentar_max_dia')::int, retentar_max_dia))),
    updated_at = now()
  where tenant_id = v_tenant;
  select to_jsonb(c) into atual from cct_config c where tenant_id = v_tenant;
  return atual;
end $$;
grant execute on function public.cct_salvar_config(jsonb) to authenticated;

-- ---------- verificação ----------
do $$ begin
  if not exists (select 1 from information_schema.columns where table_name='cct_config' and column_name='retentar_max_dia') then raise exception 'FALHA: cct_config.retentar_max_dia'; end if;
  if not exists (select 1 from information_schema.columns where table_name='cct_incidentes' and column_name='tentativas_dia') then raise exception 'FALHA: cct_incidentes.tentativas_dia'; end if;
  if not exists (select 1 from pg_proc where proname='cct_disparar_robo') then raise exception 'FALHA: cct_disparar_robo'; end if;
  if not exists (select 1 from pg_proc where proname='cct_pedir_analise_lote') then raise exception 'FALHA: cct_pedir_analise_lote'; end if;
  if position('ROBÔ NÃO EXECUTOU%' in pg_get_functiondef('public.cct_verificar_heartbeat(timestamptz)'::regprocedure)) = 0 then raise exception 'FALHA: heartbeat não fecha o incidente antigo'; end if;
  if position('retentar_max_dia' in pg_get_functiondef('public.cct_salvar_config(jsonb)'::regprocedure)) = 0 then raise exception 'FALHA: salvar_config sem retentativa'; end if;
  raise notice 'VERIFICAÇÃO v0.17.2 OK';
end $$;
