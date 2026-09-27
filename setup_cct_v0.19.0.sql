-- =====================================================================
-- CCT Monitor — setup_cct_v0.19.0.sql — 26/09/2026
-- Pré-requisito: setup_cct_v0.18.2.sql aplicado.
-- Conteúdo:
--   1) cct_consultar_agora(p_sindicato)  — "Consultar agora" por sindicato (dispara o robô com apenas_cnpj)
--   2) cct_status_disparo(p_tenant)      — resposta do GitHub ao último disparo (HTTP 204 = aceito; 401/403/404 = token/repo)
--   3) cct_salvar_config                 — passa a gravar retentar_intervalo_h, retentar_max_dia e receita_reconsulta_dias
--   4) cct_sugerir_lote(p_tenant)        — melhor sugestão de sindicato para cada empresa ativa sem vínculo
-- Nada é apagado. Idempotente.
-- =====================================================================

do $$ begin
  if not exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = 'cct_redefinir_senha') then
    raise exception 'Execute primeiro o setup_cct_v0.18.1.sql (e o v0.18.2).';
  end if;
end $$;

-- colunas de apoio (sem efeito se já existirem)
alter table public.cct_config     add column if not exists retentar_intervalo_h int default 2;
alter table public.cct_config     add column if not exists retentar_max_dia int default 3;
alter table public.cct_config     add column if not exists receita_reconsulta_dias int default 30;
alter table public.cct_config     add column if not exists ultimo_disparo_req bigint;
alter table public.cct_config     add column if not exists ultimo_disparo_em timestamptz;
alter table public.cct_config     add column if not exists ultimo_disparo_tipo text;
alter table public.cct_sindicatos add column if not exists ultimo_pedido_consulta timestamptz;

-- ---------------------------------------------------------------------
-- 1) Consultar agora — um sindicato, fora da agenda
-- ---------------------------------------------------------------------
create or replace function public.cct_consultar_agora(p_sindicato uuid)
returns jsonb language plpgsql security definer set search_path = public as $function$
declare v_nivel text := public.cct_nivel(); s record; c record; v_key text; v_disp boolean := false; v_motivo text; v_req bigint;
begin
  if v_nivel not in ('usuario','gerente','superadmin') then raise exception 'Somente a equipe do escritório pede consulta.'; end if;
  select id, tenant_id, cnpj, nome, ativo, monitorar, ultimo_pedido_consulta into s from cct_sindicatos where id = p_sindicato;
  if s.id is null or s.tenant_id not in (select public.cct_tenants_do_usuario()) then raise exception 'Sindicato não encontrado.'; end if;
  if not s.ativo then raise exception 'Sindicato inativo — reative antes de consultar.'; end if;
  select * into c from cct_config where tenant_id = s.tenant_id;
  if coalesce(c.github_repo, '') = '' then raise exception 'Repositório do robô não configurado (Configurações → Repositório do robô).'; end if;
  if s.ultimo_pedido_consulta is not null and s.ultimo_pedido_consulta > now() - interval '3 minutes' then
    return jsonb_build_object('disparado', true, 'agrupado', true, 'motivo', 'consulta deste sindicato já pedida há menos de 3 min — aguarde o resultado em "Consulta do dia"');
  end if;
  begin
    execute 'select decrypted_secret from vault.decrypted_secrets where name = $1 limit 1' into v_key using 'GITHUB_DISPATCH_TOKEN';
  exception when others then v_key := null; end;
  if v_key is null then v_motivo := 'GITHUB_DISPATCH_TOKEN não está no Vault do Supabase — a consulta sai na próxima execução agendada';
  elsif not exists (select 1 from pg_extension where extname = 'pg_net') then v_motivo := 'extensão pg_net não habilitada — a consulta sai na próxima execução agendada';
  else
    begin
      execute 'select net.http_post($1, $2, ''{}''::jsonb, $3, 15000)' into v_req
        using 'https://api.github.com/repos/' || c.github_repo || '/actions/workflows/monitor-cct.yml/dispatches',
              jsonb_build_object('ref', coalesce(nullif(c.github_branch,''),'main'), 'inputs', jsonb_build_object('forcar', 'true', 'apenas_cnpj', s.cnpj)),
              jsonb_build_object('Accept', 'application/vnd.github+json', 'Authorization', 'Bearer ' || v_key, 'X-GitHub-Api-Version', '2022-11-28',
                                 'User-Agent', 'cct-monitor-supabase', 'Content-Type', 'application/json');
      v_disp := true;
      update cct_sindicatos set ultimo_pedido_consulta = now() where id = s.id;
      update cct_config set ultimo_disparo_req = v_req, ultimo_disparo_em = now(), ultimo_disparo_tipo = 'consulta:' || s.cnpj where tenant_id = s.tenant_id;
    exception when others then v_motivo := 'falha ao disparar o robô: ' || sqlerrm || ' — a consulta sai na próxima execução agendada'; end;
  end if;
  return jsonb_build_object('disparado', v_disp, 'agrupado', false, 'motivo', v_motivo, 'cnpj', s.cnpj);
end $function$;

-- ---------------------------------------------------------------------
-- 2) Resposta do GitHub ao último disparo (o pedido é assíncrono via pg_net; a resposta fica em net._http_response)
-- ---------------------------------------------------------------------
create or replace function public.cct_status_disparo(p_tenant uuid)
returns jsonb language plpgsql security definer set search_path = public as $function$
declare c record; r record; v_txt text;
begin
  if p_tenant not in (select public.cct_tenants_do_usuario()) then raise exception 'Escritório inválido.'; end if;
  select ultimo_disparo_req, ultimo_disparo_em, ultimo_disparo_tipo, ultimo_pedido_ia into c from cct_config where tenant_id = p_tenant;
  if c.ultimo_disparo_req is null then
    return jsonb_build_object('tem', false, 'ultimo_pedido_ia', c.ultimo_pedido_ia);
  end if;
  begin
    execute 'select status_code, left(coalesce(content,''''), 300) as content, error_msg, created from net._http_response where id = $1'
      into r using c.ultimo_disparo_req;
  exception when others then r := null; end;
  if r is null or r.created is null then
    return jsonb_build_object('tem', true, 'em', c.ultimo_disparo_em, 'tipo', c.ultimo_disparo_tipo, 'pendente', true);
  end if;
  v_txt := case when r.status_code = 204 then 'aceito pelo GitHub (workflow na fila)'
                when r.status_code = 401 then 'token inválido ou expirado (GITHUB_DISPATCH_TOKEN no Vault)'
                when r.status_code = 403 then 'token sem permissão "actions:write" no repositório'
                when r.status_code = 404 then 'repositório/branch/workflow não encontrado (Configurações → Repositório do robô)'
                when r.status_code = 422 then 'entradas do workflow inválidas (o monitor-cct.yml publicado não tem os inputs esperados)'
                when r.status_code is null then 'sem resposta: ' || coalesce(r.error_msg, 'erro de rede')
                else 'HTTP ' || r.status_code end;
  return jsonb_build_object('tem', true, 'em', c.ultimo_disparo_em, 'tipo', c.ultimo_disparo_tipo, 'pendente', false,
                            'http', r.status_code, 'texto', v_txt, 'detalhe', r.content, 'ultimo_pedido_ia', c.ultimo_pedido_ia);
end $function$;

-- o disparo de parecer também registra o pedido (para o status aparecer em Configurações)
create or replace function public.cct_pedir_analise_agora(p_instrumento uuid)
returns jsonb language plpgsql security definer set search_path = public as $function$
declare v_nivel text := public.cct_nivel(); i record; c record; v_key text; v_disp boolean := false; v_motivo text; v_uso int := 0; v_req bigint;
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
      execute 'select net.http_post($1, $2, ''{}''::jsonb, $3, 15000)' into v_req
        using 'https://api.github.com/repos/' || c.github_repo || '/actions/workflows/monitor-cct.yml/dispatches',
              jsonb_build_object('ref', c.github_branch, 'inputs', jsonb_build_object('forcar', 'true', 'so_analise', 'true')),
              jsonb_build_object('Accept', 'application/vnd.github+json', 'Authorization', 'Bearer ' || v_key, 'X-GitHub-Api-Version', '2022-11-28',
                                 'User-Agent', 'cct-monitor-supabase', 'Content-Type', 'application/json');
      v_disp := true;
      update cct_config set ultimo_pedido_ia = now(), ultimo_disparo_req = v_req, ultimo_disparo_em = now(), ultimo_disparo_tipo = 'parecer' where tenant_id = i.tenant_id;
    exception when others then v_motivo := 'falha ao disparar o robô: ' || sqlerrm || ' — o parecer sai na próxima execução agendada'; end;
  end if;
  return jsonb_build_object('na_fila', true, 'disparado', v_disp, 'agrupado', false, 'motivo', v_motivo, 'uso_mes', v_uso);
end $function$;

-- ---------------------------------------------------------------------
-- 3) cct_salvar_config — grava também retentativas e reconsulta da Receita
-- ---------------------------------------------------------------------
create or replace function public.cct_salvar_config(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $function$
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
    -- v0.19.0: estes três eram enviados pelo app desde a v0.17.2/v0.18.0 mas não eram gravados
    retentar_intervalo_h = coalesce(least(12, greatest(1, (p ->> 'retentar_intervalo_h')::int)), retentar_intervalo_h),
    retentar_max_dia = coalesce(least(10, greatest(0, (p ->> 'retentar_max_dia')::int)), retentar_max_dia),
    receita_reconsulta_dias = coalesce(least(365, greatest(0, (p ->> 'receita_reconsulta_dias')::int)), receita_reconsulta_dias),
    updated_at = now()
  where tenant_id = v_tenant;
  select to_jsonb(c) into atual from cct_config c where tenant_id = v_tenant;
  return atual;
end $function$;

-- ---------------------------------------------------------------------
-- 4) Sugestão de sindicato em lote — só empresas ATIVAS SEM vínculo; nada é vinculado aqui (a confirmação é por linha, no app)
-- ---------------------------------------------------------------------
create or replace function public.cct_sugerir_lote(p_tenant uuid)
returns table(empresa_id uuid, razao_social text, cnpj text, municipio text, uf text, cnae text,
              sindicato_id uuid, sindicato text, tipo text, pontos int, motivos text[], alternativas int)
language sql stable security definer set search_path = public as $function$
  with base as (
    select e.id, e.razao_social, e.cnpj, e.municipio, e.uf, e.cnae
      from cct_empresas e
     where e.tenant_id = p_tenant and e.ativo
       and p_tenant in (select public.cct_tenants_do_usuario())
       and not exists (select 1 from cct_empresa_sindicato es where es.empresa_id = e.id))
  select b.id, b.razao_social, b.cnpj, b.municipio, b.uf, b.cnae,
         s.sindicato_id, s.nome, s.tipo, s.pontos, s.motivos,
         (select count(*)::int from public.cct_sugerir_sindicatos(p_tenant, b.cnae, b.uf, b.municipio)) - 1
    from base b
    left join lateral (select * from public.cct_sugerir_sindicatos(p_tenant, b.cnae, b.uf, b.municipio) order by pontos desc, nome limit 1) s on true
   order by (s.sindicato_id is null), s.pontos desc nulls last, b.razao_social
$function$;

grant execute on function public.cct_consultar_agora(uuid) to authenticated;
grant execute on function public.cct_status_disparo(uuid) to authenticated;
grant execute on function public.cct_sugerir_lote(uuid) to authenticated;
grant execute on function public.cct_salvar_config(jsonb) to authenticated;
grant execute on function public.cct_pedir_analise_agora(uuid) to authenticated;

notify pgrst, 'reload schema';
-- fim setup_cct_v0.19.0.sql
