-- =====================================================================
--  CCT MONITOR — setup v0.17.0 (usuários: excluir; e-mail de erros críticos; ciência só a partir da data)
--  Incremental sobre v0.16.0. Idempotente. Validado em PostgreSQL 16 local em 11/09/2026.
--  O que faz:
--   1. cct_config.email_erros_criticos — endereço(s) que recebem TODO erro CRÍTICO do sistema (robô, Central de Erros
--      e vigia do banco), além dos gerentes/administradores. Vários separados por vírgula/ponto e vírgula/linha.
--   2. cct_emails_erros_criticos(tenant) — gerentes ∪ endereços configurados (usada pelo robô e pelo heartbeat).
--   3. cct_verificar_heartbeat passa a usar cct_emails_erros_criticos (só muda o destinatário; lógica idêntica à v0.16.0).
--   4. cct_salvar_config aceita email_erros_criticos (configuração operacional: administrador altera).
--   5. cct_excluir_usuario(auth_id) — administrador/superadministrador exclui um usuário: remove acessos do CCT, o vínculo
--      do Rescisões (resc_usuarios), inativa o colaborador e apaga o login (auth.users). Regras: não exclui a si mesmo,
--      não exclui o último administrador, só superadministrador exclui superadministrador.
-- =====================================================================
do $$ begin
  if not exists (select 1 from pg_proc where proname='cct_email_db') then raise exception 'Execute antes o setup_cct_v0.16.0.sql'; end if;
end $$;

do $$ begin
  raise notice 'ANTES: cct_config.email_erros_criticos existe? % · cct_excluir_usuario existe? %',
    exists (select 1 from information_schema.columns where table_name='cct_config' and column_name='email_erros_criticos'),
    exists (select 1 from pg_proc where proname='cct_excluir_usuario');
end $$;

-- ---------- 1. coluna ----------
alter table public.cct_config add column if not exists email_erros_criticos text;

-- ---------- 2. destinatários de erro crítico = gerentes ∪ configurados ----------
create or replace function public.cct_emails_erros_criticos(p_tenant uuid) returns setof text
language sql stable security definer set search_path = public as $$
  select x from public.cct_emails_gerentes(p_tenant) x
  union
  select lower(trim(e)) from cct_config c, regexp_split_to_table(coalesce(c.email_erros_criticos, ''), '[,;\s]+') e
   where c.tenant_id = p_tenant and trim(e) ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
$$;
grant execute on function public.cct_emails_erros_criticos(uuid) to authenticated;

-- ---------- 3. heartbeat: mesmo corpo da v0.16.0, destinatários = erros críticos ----------
create or replace function public.cct_verificar_heartbeat(p_ref timestamptz default now()) returns jsonb
language plpgsql security definer set search_path = public as $$
declare t record; s jsonb; v_inc uuid; v_abertos int; n_alerta int := 0; n_res int := 0; e record; v_dest text[]; v_msg text;
        abertos text[] := array['NOVO','EM_NOVA_TENTATIVA','PERSISTENTE','EM_ANALISE'];
begin
  for t in select tenant_id from cct_config loop
    s := public.cct_saude_robo(t.tenant_id, p_ref);
    select array_agg(x) into v_dest from public.cct_emails_erros_criticos(t.tenant_id) x;   -- v0.17.0
    if s ->> 'situacao' = 'ATRASADO' then
      select count(*) into v_abertos from cct_incidentes where tenant_id = t.tenant_id and fingerprint = 'APLICATIVO:execucao-diaria' and status = any(abertos);
      if v_abertos = 0 then
        v_msg := format('ROBÔ NÃO EXECUTOU – execução prevista para %s não iniciou até %s (tolerância %s min). Última realizada: %s.',
                        to_char((s ->> 'ultima_prevista')::timestamptz at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'),
                        to_char(p_ref at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'), (s ->> 'tolerancia_min')::int,
                        coalesce(to_char((s ->> 'ultima_realizada')::timestamptz at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'), 'nunca'));
        v_inc := public.cct_registrar_incidente(t.tenant_id, 'APLICATIVO:execucao-diaria', 'APLICATIVO', 'CRITICO', v_msg, null, null, s);
        perform public.cct_email_db(t.tenant_id, v_dest, 'Artecon · CCT Monitor — ROBÔ NÃO EXECUTOU', '<p>' || v_msg || '</p><p>Verifique em GitHub → Actions → CCT Monitor. Este aviso foi enviado pelo banco de dados (vigia independente do robô).</p>');
        n_alerta := n_alerta + 1;
      end if;
    elsif s ->> 'situacao' in ('NORMAL', 'COM_ALERTAS', 'EXECUTANDO') then
      n_res := n_res + coalesce(public.cct_resolver_incidente(t.tenant_id, 'APLICATIVO:execucao-diaria', 'automatico'), 0);
    end if;
    for e in select * from cct_execucoes where tenant_id = t.tenant_id and resultado in ('INTERROMPIDA','ERRO_TOTAL') and not alertado order by inicio loop
      v_msg := format('EXECUÇÃO %s – iniciada %s (%s): %s', e.resultado, to_char(e.inicio at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'),
                      coalesce(e.origem, '?'), coalesce(e.motivo, 'sem motivo registrado'));
      perform public.cct_registrar_incidente(t.tenant_id, 'APLICATIVO:execucao:' || e.id::text, 'APLICATIVO', 'CRITICO', v_msg, null, null, to_jsonb(e));
      perform public.cct_email_db(t.tenant_id, v_dest, 'Artecon · CCT Monitor — Execução ' || lower(e.resultado),
                                  '<p>' || v_msg || '</p>' || coalesce('<p><a href="' || e.run_url || '">Abrir o log no GitHub</a></p>', ''));
      update cct_execucoes set alertado = true where id = e.id;
      n_alerta := n_alerta + 1;
    end loop;
  end loop;
  return jsonb_build_object('alertas', n_alerta, 'resolvidos', n_res, 'ref', p_ref);
end $$;

-- ---------- 4. salvar_config (v0.13.0 + ciencia_inicio da v0.15.2 + email_erros_criticos) ----------
create or replace function public.cct_salvar_config(p jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_nivel text := public.cct_nivel(); v_tenant uuid; criticas text[] := array['ia_ativa','ia_limite_mes','ia_historico','horarios_consulta','intervalo_consultas_s','max_sindicatos_por_execucao'];
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
  if mudou_critica and v_nivel <> 'superadmin' then raise exception 'IA, horários/fila de consultas e limpeza são configurações críticas: somente o superadministrador altera.'; end if;
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
    updated_at = now()
  where tenant_id = v_tenant;
  select to_jsonb(c) into atual from cct_config c where tenant_id = v_tenant;
  return atual;
end $$;
grant execute on function public.cct_salvar_config(jsonb) to authenticated;

-- ---------- 5. excluir usuário ----------
create or replace function public.cct_excluir_usuario(p_auth uuid) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_nivel text := public.cct_nivel(); v_tenant uuid; v_papel text; v_admins int; n_acessos int := 0; n_usu int := 0; n_colab int := 0;
        v_email text; v_login boolean := false; v_login_erro text;
begin
  if v_nivel not in ('gerente','superadmin') then raise exception 'Somente administrador ou superadministrador exclui usuários.'; end if;
  if p_auth = auth.uid() then raise exception 'Você não pode excluir o seu próprio usuário.'; end if;
  select tenant_id into v_tenant from public.resc_usuarios where auth_id = auth.uid() limit 1;
  if v_tenant is null then select tenant_id into v_tenant from public.cct_acessos where auth_id = auth.uid() and ativo limit 1; end if;
  select papel into v_papel from public.resc_usuarios where auth_id = p_auth and tenant_id = v_tenant;
  if v_papel is null and not exists (select 1 from public.cct_acessos where auth_id = p_auth and tenant_id = v_tenant) then
    raise exception 'Usuário não pertence ao seu escritório.';
  end if;
  if (v_papel = 'superadmin' or exists (select 1 from public.cct_acessos where auth_id = p_auth and tenant_id = v_tenant and nivel = 'superadmin'))
     and v_nivel <> 'superadmin' then raise exception 'Somente o superadministrador exclui um superadministrador.'; end if;
  if v_papel in ('diretoria','superadmin','admin') then
    select count(*) into v_admins from public.resc_usuarios where tenant_id = v_tenant and papel in ('diretoria','superadmin','admin');
    if v_admins <= 1 then raise exception 'Este é o único administrador — promova outro antes de excluí-lo.'; end if;
  end if;
  select lower(c.email) into v_email from public.resc_colaboradores c where c.auth_id = p_auth and c.tenant_id = v_tenant limit 1;
  if v_email is null then select lower(email) into v_email from public.cct_acessos where auth_id = p_auth and tenant_id = v_tenant limit 1; end if;
  delete from public.cct_acessos where auth_id = p_auth and tenant_id = v_tenant; get diagnostics n_acessos = row_count;
  delete from public.resc_usuarios where auth_id = p_auth and tenant_id = v_tenant; get diagnostics n_usu = row_count;
  update public.resc_colaboradores set status = 'inativo' where auth_id = p_auth and tenant_id = v_tenant; get diagnostics n_colab = row_count;
  -- login: só quando o usuário não tem mais nenhum vínculo em outro escritório
  if not exists (select 1 from public.resc_usuarios where auth_id = p_auth) and not exists (select 1 from public.cct_acessos where auth_id = p_auth) then
    begin
      delete from auth.users where id = p_auth; v_login := found;
    exception when others then v_login := false; v_login_erro := sqlerrm; end;
  end if;
  return jsonb_build_object('email', v_email, 'acessos', n_acessos, 'usuarios', n_usu, 'colaborador_inativado', n_colab > 0,
                            'login_apagado', v_login, 'login_erro', v_login_erro);
end $$;
grant execute on function public.cct_excluir_usuario(uuid) to authenticated;

-- ---------- verificação ----------
do $$ begin
  if not exists (select 1 from information_schema.columns where table_name='cct_config' and column_name='email_erros_criticos') then raise exception 'FALHA: cct_config.email_erros_criticos'; end if;
  if not exists (select 1 from pg_proc where proname='cct_emails_erros_criticos') then raise exception 'FALHA: cct_emails_erros_criticos'; end if;
  if not exists (select 1 from pg_proc where proname='cct_excluir_usuario') then raise exception 'FALHA: cct_excluir_usuario'; end if;
  if position('cct_emails_erros_criticos' in pg_get_functiondef('public.cct_verificar_heartbeat(timestamptz)'::regprocedure)) = 0 then raise exception 'FALHA: heartbeat não usa cct_emails_erros_criticos'; end if;
  if position('email_erros_criticos' in pg_get_functiondef('public.cct_salvar_config(jsonb)'::regprocedure)) = 0 then raise exception 'FALHA: salvar_config sem email_erros_criticos'; end if;
  raise notice 'VERIFICAÇÃO v0.17.0 OK';
end $$;
