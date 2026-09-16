-- ============================================================================
-- CCT Monitor — setup_cct_v0.18.2.sql  (incremental sobre a v0.18.1)
-- ----------------------------------------------------------------------------
-- 1) Exclui EMPRESAS (e sindicatos, se houver) duplicadas por CNPJ dentro do escritório:
--    - de cada grupo com o mesmo CNPJ fica UM cadastro (o "mantido"): o que passou pela Receita, depois o que tem mais
--      vínculos/ciências/acessos apontando para ele, depois o mais antigo;
--    - tudo o que apontava para os duplicados (vínculos empresa×sindicato, ciências, acessos de cliente, convenções por
--      ACT, alterações cadastrais…) é REAPONTADO para o mantido — descoberto pelas chaves estrangeiras do próprio banco,
--      então nada fica órfão; se o mantido já tinha o mesmo vínculo/ciência, a linha repetida é removida;
--    - os duplicados são então excluídos. Nada de convenção ou ciência se perde.
-- 2) Proíbe daqui em diante dois cadastros com o mesmo CNPJ (empresas e sindicatos):
--    índice único por (tenant_id, cnpj) + gatilho com mensagem em português ("CNPJ já cadastrado: …") que o app exibe.
-- Antes de executar, rode a PRÉVIA (sql/duplicados_previa.sql) para ver o que será mantido e o que será excluído.
-- Executar no SQL Editor do Supabase DEPOIS do setup_cct_v0.18.1.sql.
-- ============================================================================
do $$
begin
  if not exists (select 1 from pg_proc where proname = 'cct_redefinir_senha') then
    raise exception 'Execute primeiro o setup_cct_v0.18.1.sql.';
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- 1) Deduplicação (genérica: cct_empresas e cct_sindicatos)
-- ----------------------------------------------------------------------------
create or replace function public.cct_deduplicar_por_cnpj(p_tabela text)
returns table (cnpj text, mantido uuid, excluidos int, reapontados int, repetidos_removidos int)
language plpgsql
security definer
set search_path = public
as $$
declare
  g          record;
  d          record;
  fk         record;
  r          record;
  v_nome_col text := case when p_tabela = 'cct_empresas' then 'razao_social' else 'nome' end;
  v_tem_ts   boolean;
  v_keep     uuid;
  v_ids      uuid[];
  v_ex       int; v_re int; v_rm int; v_tmp int;
begin
  if p_tabela not in ('cct_empresas', 'cct_sindicatos') then
    raise exception 'tabela inválida: %', p_tabela;
  end if;
  select exists (select 1 from information_schema.columns where table_schema = 'public' and table_name = p_tabela and column_name = 'created_at') into v_tem_ts;

  for g in execute format('select tenant_id, cnpj from %I where cnpj is not null group by tenant_id, cnpj having count(*) > 1', p_tabela) loop
    -- referências a cada candidato (contadas por todas as FKs que apontam para a tabela)
    execute format($q$
      select t.id from %I t
       where t.tenant_id = $1 and t.cnpj = $2
       order by (t.receita_em is not null) desc, (%s) desc%s, t.id
       limit 1
    $q$, p_tabela,
         case when p_tabela = 'cct_empresas'
              then '(select count(*) from cct_empresa_sindicato x where x.empresa_id = t.id) + (select count(*) from cct_ciencias x where x.empresa_id = t.id) + (select count(*) from cct_acessos x where x.empresa_id = t.id)'
              else '(select count(*) from cct_empresa_sindicato x where x.sindicato_id = t.id) + (select count(*) from cct_instrumentos x where x.sindicato_id = t.id)' end,
         case when v_tem_ts then ', t.created_at asc' else '' end)
    into v_keep using g.tenant_id, g.cnpj;
    execute format('select array_agg(id) from %I where tenant_id = $1 and cnpj = $2 and id <> $3', p_tabela) into v_ids using g.tenant_id, g.cnpj, v_keep;
    v_ex := 0; v_re := 0; v_rm := 0;

    -- reaponta cada FK que referencia p_tabela(id)
    for fk in
      select c.conrelid::regclass as tab, a.attname as col
        from pg_constraint c
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
       where c.contype = 'f' and c.confrelid = p_tabela::regclass and c.conrelid <> p_tabela::regclass
    loop
      for r in execute format('select ctid from %s where %I = any($1)', fk.tab, fk.col) using v_ids loop
        begin
          execute format('update %s set %I = $1 where ctid = $2', fk.tab, fk.col) using v_keep, r.ctid;
          v_re := v_re + 1;
        exception when unique_violation then
          execute format('delete from %s where ctid = $1', fk.tab) using r.ctid;  -- o mantido já tinha a linha equivalente
          v_rm := v_rm + 1;
        end;
      end loop;
    end loop;
    -- alterações cadastrais (entidade_id sem FK)
    begin
      execute 'update cct_alteracoes_cadastrais set entidade_id = $1 where entidade_id = any($2)' using v_keep, v_ids;
    exception when others then null;
    end;
    -- ciências duplicadas do mesmo instrumento no mantido (se a tabela não tiver unique): remove as repetidas
    if p_tabela = 'cct_empresas' then
      delete from cct_ciencias a using cct_ciencias b
       where a.empresa_id = v_keep and b.empresa_id = v_keep and a.instrumento_id = b.instrumento_id and a.ctid > b.ctid;
      get diagnostics v_tmp = row_count; v_rm := v_rm + v_tmp;
    end if;

    execute format('delete from %I where id = any($1)', p_tabela) using v_ids;
    get diagnostics v_ex = row_count;

    cnpj := g.cnpj; mantido := v_keep; excluidos := v_ex; reapontados := v_re; repetidos_removidos := v_rm;
    return next;
  end loop;
end;
$$;
revoke all on function public.cct_deduplicar_por_cnpj(text) from public;

-- executa (o resultado aparece na saída do SQL Editor)
select 'cct_empresas' as tabela, * from public.cct_deduplicar_por_cnpj('cct_empresas')
union all
select 'cct_sindicatos', * from public.cct_deduplicar_por_cnpj('cct_sindicatos');

-- ----------------------------------------------------------------------------
-- 2) Proibição de CNPJ repetido (índice único + gatilho com mensagem clara)
-- ----------------------------------------------------------------------------
create unique index if not exists cct_empresas_tenant_cnpj_uk  on public.cct_empresas  (tenant_id, cnpj) where cnpj is not null;
create unique index if not exists cct_sindicatos_tenant_cnpj_uk on public.cct_sindicatos (tenant_id, cnpj) where cnpj is not null;

create or replace function public.cct_bloquear_cnpj_repetido()
returns trigger
language plpgsql
as $$
declare
  v_nome text;
begin
  if new.cnpj is null then
    return new;
  end if;
  new.cnpj := regexp_replace(new.cnpj, '\D', '', 'g');
  if tg_table_name = 'cct_empresas' then
    select razao_social into v_nome from cct_empresas where tenant_id = new.tenant_id and cnpj = new.cnpj and id <> new.id limit 1;
    if found then
      raise exception 'CNPJ já cadastrado: empresa "%" (%). Abra o cadastro existente em vez de criar outro.', v_nome, new.cnpj
        using errcode = 'unique_violation';
    end if;
  else
    select nome into v_nome from cct_sindicatos where tenant_id = new.tenant_id and cnpj = new.cnpj and id <> new.id limit 1;
    if found then
      raise exception 'CNPJ já cadastrado: sindicato "%" (%). Abra o cadastro existente em vez de criar outro.', v_nome, new.cnpj
        using errcode = 'unique_violation';
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_cct_empresas_cnpj_unico on public.cct_empresas;
create trigger trg_cct_empresas_cnpj_unico before insert or update of cnpj, tenant_id on public.cct_empresas
  for each row execute function public.cct_bloquear_cnpj_repetido();
drop trigger if exists trg_cct_sindicatos_cnpj_unico on public.cct_sindicatos;
create trigger trg_cct_sindicatos_cnpj_unico before insert or update of cnpj, tenant_id on public.cct_sindicatos
  for each row execute function public.cct_bloquear_cnpj_repetido();

-- ----------------------------------------------------------------------------
-- Conferência: deve devolver 0 linhas
-- ----------------------------------------------------------------------------
select 'cct_empresas' tabela, cnpj, count(*) from public.cct_empresas where cnpj is not null group by cnpj having count(*) > 1
union all
select 'cct_sindicatos', cnpj, count(*) from public.cct_sindicatos where cnpj is not null group by cnpj having count(*) > 1;
