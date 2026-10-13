-- =====================================================================
-- CCT Monitor — setup_cct_v0.23.0.sql — 13/10/2026
-- Pré-requisito: setup_cct_v0.19.0.sql aplicado.
-- Conteúdo:
--   1) cct_config.notificar_gerente (padrão: true = comportamento atual)
--   2) cct_definir_notificar_gerente(p_tenant, p_valor) — grava a opção "Notificar o gerente do departamento".
--      Com "Não" (false), as ciências ESCALONADAS do escritório voltam para NOTIFICADO (atrasada, sem
--      escalonamento) — ou PENDENTE, se o responsável nunca foi avisado — e o retorno informa quantas
--      (chave "escalonamentos_cancelados"). O gerente do departamento deixa de receber qualquer e-mail;
--      o responsável continua com aviso e lembretes. cct_salvar_config não muda.
-- Nada é apagado. Idempotente. (Aplicado no banco em 13/10/2026.)
-- =====================================================================

do $$ begin
  if not exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'cct_config' and column_name = 'receita_reconsulta_dias') then
    raise exception 'Execute primeiro o setup_cct_v0.19.0.sql.';
  end if;
end $$;

alter table public.cct_config add column if not exists notificar_gerente boolean not null default true;
comment on column public.cct_config.notificar_gerente is
  'v0.23.0: false = o gerente do departamento (empresa/sindicato) não recebe e-mail algum (escalonamento, lembretes, aviso de convenção sem registro); o responsável continua sendo notificado.';

create or replace function public.cct_definir_notificar_gerente(p_tenant uuid, p_valor boolean)
returns jsonb language plpgsql security definer set search_path = public as $function$
declare v_nivel text := public.cct_nivel(); v_cancelados int := 0;
begin
  if v_nivel not in ('gerente','superadmin') then raise exception 'Somente administrador ou superadministrador altera configurações.'; end if;
  if p_tenant is null or p_valor is null or p_tenant not in (select public.cct_tenants_do_usuario()) then raise exception 'Escritório inválido.'; end if;
  update cct_config set notificar_gerente = p_valor, updated_at = now() where tenant_id = p_tenant;
  if not p_valor then
    update cct_ciencias
       set status = case when notificado_em is null then 'PENDENTE' else 'NOTIFICADO' end,
           escalonado_em = null, updated_at = now()
     where tenant_id = p_tenant and status = 'ESCALONADO';
    get diagnostics v_cancelados = row_count;
  end if;
  return jsonb_build_object('notificar_gerente', p_valor, 'escalonamentos_cancelados', v_cancelados);
end $function$;
revoke all on function public.cct_definir_notificar_gerente(uuid, boolean) from public, anon;
grant execute on function public.cct_definir_notificar_gerente(uuid, boolean) to authenticated, service_role;
