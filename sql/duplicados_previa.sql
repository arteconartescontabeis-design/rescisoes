-- CCT Monitor v0.18.2 — PRÉVIA (só leitura) das empresas e sindicatos duplicados por CNPJ.
-- (Se houver duplicados nas duas tabelas ao mesmo tempo, o setup deduplica primeiro as empresas e as contagens dos sindicatos
-- podem mudar de leve — a regra é a mesma.)
-- Mostra, por CNPJ, cada cadastro, quantos vínculos/ciências/acessos apontam para ele e qual será MANTIDO
-- pelo setup_cct_v0.18.2.sql (Receita consultada > mais referências > mais antigo). Não altera nada.
with e as (
  select 'empresa' tipo, e.id, e.tenant_id, e.cnpj, e.razao_social nome, e.ativo, e.receita_em,
         (select count(*) from cct_empresa_sindicato x where x.empresa_id = e.id) vinculos,
         (select count(*) from cct_ciencias x where x.empresa_id = e.id) ciencias,
         (select count(*) from cct_acessos x where x.empresa_id = e.id) acessos,
         (select count(*) from cct_instrumentos x where x.empresa_id = e.id) convencoes
    from cct_empresas e where e.cnpj is not null
  union all
  select 'sindicato', s.id, s.tenant_id, s.cnpj, s.nome, s.ativo, s.receita_em,
         (select count(*) from cct_empresa_sindicato x where x.sindicato_id = s.id),
         (select count(*) from cct_ciencias x where x.sindicato_id = s.id),
         0,
         (select count(*) from cct_instrumentos x where x.sindicato_id = s.id)
    from cct_sindicatos s where s.cnpj is not null),
d as (select tipo, tenant_id, cnpj from e group by tipo, tenant_id, cnpj having count(*) > 1)
select e.tipo, e.cnpj, e.nome, e.ativo, e.receita_em, e.vinculos, e.ciencias, e.acessos, e.convencoes,
       case when row_number() over (partition by e.tipo, e.tenant_id, e.cnpj
                 order by (e.receita_em is not null) desc, (e.vinculos + e.ciencias + e.acessos + e.convencoes) desc, e.id) = 1
            then 'MANTIDO' else 'será excluído (referências vão para o mantido)' end decisao,
       e.id
  from e join d using (tipo, tenant_id, cnpj)
 order by e.tipo, e.cnpj, decisao;
