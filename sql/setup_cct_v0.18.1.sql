-- ============================================================================
-- CCT Monitor — setup_cct_v0.18.1.sql  (incremental sobre a v0.18.0)
-- ----------------------------------------------------------------------------
-- O que este script faz:
--   1) cct_redefinir_senha(p_auth, p_senha): o administrador (níveis 1 e 2) define a nova senha de um usuário
--      DIRETAMENTE no app, sem e-mail de recuperação. Regras:
--        - só Administrador ou Superadministrador executam;
--        - o usuário precisa pertencer ao mesmo escritório (resc_usuarios ou cct_acessos do tenant de quem chama);
--        - a senha de um Superadministrador só é alterada por ele mesmo (para isso existe "Alterar minha senha");
--        - senha com no mínimo 8 caracteres;
--        - grava em auth.users.encrypted_password com bcrypt (mesmo formato que o GoTrue usa);
--        - encerra as sessões abertas do usuário (ele precisa entrar de novo com a senha nova);
--        - registra a ação em cct_auditoria (sem a senha).
-- Executar no SQL Editor do Supabase DEPOIS do setup_cct_v0.18.0.sql.
-- Trava: exige a coluna cct_config.receita_reconsulta_dias (criada na v0.18.0).
-- ============================================================================
do $$
begin
  if not exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = 'cct_config' and column_name = 'receita_reconsulta_dias') then
    raise exception 'Execute primeiro o setup_cct_v0.18.0.sql (coluna cct_config.receita_reconsulta_dias não existe).';
  end if;
end $$;

create extension if not exists pgcrypto with schema extensions;

create or replace function public.cct_redefinir_senha(p_auth uuid, p_senha text)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, auth
as $$
declare
  v_eu        uuid := auth.uid();
  v_nivel     text;
  v_email     text;
  v_alvo_sup  boolean := false;
  v_tenants   uuid[];
  v_do_tenant boolean := false;
  v_sessoes   int := 0;
begin
  if v_eu is null then
    raise exception 'Sessão inválida: entre de novo no aplicativo.';
  end if;
  v_nivel := public.cct_nivel();
  if v_nivel not in ('gerente', 'superadmin') then
    raise exception 'Só Administrador ou Superadministrador pode redefinir a senha de um usuário.';
  end if;
  if p_auth is null then
    raise exception 'Usuário não informado.';
  end if;
  if p_senha is null or length(p_senha) < 8 then
    raise exception 'A nova senha precisa ter pelo menos 8 caracteres.';
  end if;
  if length(p_senha) > 72 then
    raise exception 'A senha pode ter no máximo 72 caracteres.';
  end if;

  select u.email into v_email from auth.users u where u.id = p_auth;
  if v_email is null then
    raise exception 'Usuário não encontrado no login.';
  end if;

  -- escritórios de quem chama (resc_usuarios ∪ cct_acessos)
  select array_agg(distinct t) into v_tenants
    from (select tenant_id t from public.resc_usuarios where auth_id = v_eu
          union select tenant_id from public.cct_acessos where auth_id = v_eu and ativo) x;
  select exists (select 1 from public.resc_usuarios where auth_id = p_auth and tenant_id = any(v_tenants))
      or exists (select 1 from public.cct_acessos    where auth_id = p_auth and tenant_id = any(v_tenants))
    into v_do_tenant;
  if not v_do_tenant then
    raise exception 'Este usuário não pertence ao seu escritório.';
  end if;

  -- senha de Superadministrador: só ele mesmo
  select exists (select 1 from public.resc_usuarios where auth_id = p_auth and papel = 'superadmin')
      or exists (select 1 from public.cct_acessos    where auth_id = p_auth and nivel = 'superadmin')
    into v_alvo_sup;
  if v_alvo_sup and p_auth <> v_eu then
    raise exception 'A senha de um Superadministrador só pode ser alterada por ele mesmo (use "Alterar minha senha").';
  end if;

  update auth.users
     set encrypted_password = extensions.crypt(p_senha, extensions.gen_salt('bf')),
         updated_at = now()
   where id = p_auth;

  -- encerra as sessões abertas: o usuário entra de novo com a senha nova
  begin
    delete from auth.refresh_tokens where user_id = p_auth::text;
    delete from auth.sessions where user_id = p_auth;
    get diagnostics v_sessoes = row_count;
  exception when others then
    v_sessoes := -1;  -- estrutura do auth diferente: a senha já foi trocada; as sessões expiram sozinhas
  end;

  -- auditoria (sem a senha)
  begin
    insert into public.cct_auditoria (tenant_id, tabela, registro_id, acao, usuario, quando, antes, depois, campos)
    select v_tenants[1], 'cct_acessos', p_auth::text, 'UPDATE',
           (select email from auth.users where id = v_eu), now(),
           jsonb_build_object('email', v_email, 'senha', '(anterior)'),
           jsonb_build_object('email', v_email, 'senha', '(redefinida pelo administrador)'),
           array['senha'];
  exception when others then
    null;  -- auditoria com outra estrutura: não impede a redefinição
  end;

  return jsonb_build_object('ok', true, 'email', v_email, 'sessoes_encerradas', v_sessoes);
end;
$$;

revoke all on function public.cct_redefinir_senha(uuid, text) from public;
grant execute on function public.cct_redefinir_senha(uuid, text) to authenticated;

-- ----------------------------------------------------------------------------
-- Conferência
-- ----------------------------------------------------------------------------
select 'cct_redefinir_senha' as funcao, prosecdef as security_definer
  from pg_proc where proname = 'cct_redefinir_senha';
