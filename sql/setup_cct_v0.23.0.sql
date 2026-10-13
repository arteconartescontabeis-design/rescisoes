-- =====================================================================
-- CCT Monitor — setup_cct_v0.23.0.sql — 13/10/2026
-- Pré-requisito: setup_cct_v0.19.0.sql aplicado.
-- Conteúdo:
--   1) cct_config.notificar_gerente (padrão: true = comportamento atual)
--   2) cct_salvar_config grava notificar_gerente; ao gravar "Não", as ciências ESCALONADAS do escritório
--      voltam para NOTIFICADO (atrasada, sem escalonamento) — ou PENDENTE, se o responsável nunca foi avisado —
--      e o retorno informa quantas (chave "escalonamentos_cancelados").
--      O gerente do departamento deixa de receber qualquer e-mail; o responsável continua com aviso e lembretes.
-- Nada é apagado. Idempotente.
-- =====================================================================

do $$ begin
  if not exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'cct_config' and column_name = 'receita_reconsulta_dias') then
    raise exception 'Execute primeiro o setup_cct_v0.19.0.sql.';
  end if;
end $$;

alter table public.cct_config add column if not exists notificar_gerente boolean not null default true;
comment on column public.cct_config.notificar_gerente is
  'v0.23.0: false = o gerente do departamento (empresa/sindicato) não recebe e-mail algum (escalonamento, lembretes, aviso de convenção sem registro); o responsável continua sendo notificado.';

create or replace function public.cct_salvar_config(p jsonb)
returns jsonb language plpgsql security definer set search_path = public as $function$
declare v_nivel text := public.cct_nivel(); v_tenant uuid; criticas text[] := array['ia_ativa','ia_limite_mes','ia_historico','horarios_consulta','intervalo_consultas_s','max_sindicatos_por_execucao','github_repo','github_branch'];
        k text; atual jsonb; mudou_critica boolean := false; v_cancelados int := 0;
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
    notificar_gerente = coalesce((p ->> 'notificar_gerente')::boolean, notificar_gerente),   -- v0.23.0
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
    retentar_intervalo_h = coalesce(least(12, greatest(1, (p ->> 'retentar_intervalo_h')::int)), retentar_intervalo_h),
    retentar_max_dia = coalesce(least(10, greatest(0, (p ->> 'retentar_max_dia')::int)), retentar_max_dia),
    receita_reconsulta_dias = coalesce(least(365, greatest(0, (p ->> 'receita_reconsulta_dias')::int)), receita_reconsulta_dias),
    updated_at = now()
  where tenant_id = v_tenant;
  -- v0.23.0: gerente desligado → os escalonamentos em aberto são desfeitos; a ciência continua atrasada e o
  -- responsável segue recebendo o lembrete diário (o robô só tira o gerente dos destinatários)
  if not (select notificar_gerente from cct_config where tenant_id = v_tenant) then
    update cct_ciencias
       set status = case when notificado_em is null then 'PENDENTE' else 'NOTIFICADO' end,
           escalonado_em = null, updated_at = now()
     where tenant_id = v_tenant and status = 'ESCALONADO';
    get diagnostics v_cancelados = row_count;
  end if;
  select to_jsonb(c) into atual from cct_config c where tenant_id = v_tenant;
  return atual || jsonb_build_object('escalonamentos_cancelados', v_cancelados);
end $function$;
