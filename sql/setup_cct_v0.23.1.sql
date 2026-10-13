-- =====================================================================
-- CCT Monitor — setup_cct_v0.23.1.sql — 13/10/2026 (aplicado no banco em 13/10/2026)
-- Pré-requisito: setup_cct_v0.23.0.sql aplicado.
--
-- Problemas encontrados na varredura de 13/10/2026:
--   1) "Robô ATRASADO": o agendamento do GitHub ("5 * * * *") dispara só 3–4 vezes por dia, com atrasos
--      de 4–7 h; a consulta das 05:00 começava entre 05:33 e 14:29.
--   2) O vigia do banco (pg_cron "cct-heartbeat") falhava desde 10/09/2026 (3.214 vezes):
--      "function public.cct_verificar_heartbeat() is not unique" — havia duas versões da função
--      (p_horas integer) e (p_ref timestamptz) e o job chamava sem argumento.
--   3) cct_email_db nunca enviava: procurava o segredo MAIL_API_KEY (no Vault ele se chama mail_api_key)
--      e gravava destinatarios text[] numa coluna jsonb (erro que derrubava o vigia inteiro).
--   4) Funções internas (registrar/resolver incidente, encerrar execução, vigia) executáveis por
--      qualquer usuário logado — inclusive cliente.
--
-- Correções (funções novas; as antigas ficam sem uso e sem permissão):
--   A) cct_vigia_robo(p_ref, p_disparar, p_alertar): a cada 15 min, se o horário configurado já passou
--      (dia útil, até 8 h depois), há sindicato monitorado não consultado desde então, o robô não está
--      executando, houve menos de 3 execuções desde o horário e nenhum disparo nos últimos 40 min,
--      aciona o workflow monitor-cct.yml (workflow_dispatch, forcar=false) com o GITHUB_DISPATCH_TOKEN
--      do Vault — o mesmo mecanismo do "Consultar agora".
--   B) cct_alertas_robo(p_ref): o mesmo vigia de cct_verificar_heartbeat(timestamptz), usando
--      cct_email_db2; o ciclo é pulado nos 30 min seguintes a um disparo (tempo do robô abrir a execução).
--      cct_email_db2: segredo MAIL_API_KEY ou mail_api_key; destinatarios gravados como jsonb.
--      As 27 execuções INTERROMPIDA/ERRO_TOTAL antigas (10–28/09) foram marcadas como alertadas para
--      não gerar uma rajada de e-mails ao religar o vigia.
--   C) EXECUTE só para service_role (robô) e postgres (pg_cron) nas funções internas.
-- Nada é apagado.
-- =====================================================================

-- execuções antigas nunca alertadas (o vigia estava quebrado): não alertar agora
update public.cct_execucoes set alertado = true
 where resultado in ('INTERROMPIDA','ERRO_TOTAL') and not alertado and inicio < now() - interval '1 day';

create or replace function public.cct_email_db2(p_tenant uuid, p_para text[], p_assunto text, p_html text, p_tipo text default 'ERRO')
returns text language plpgsql security definer set search_path = public as $function$
declare v_key text; v_teste text; v_status text := 'ENVIADA'; v_erro text; d text; v_html text := p_html; v_para text[] := p_para;
        v_url text := 'https://tjnqloycikukvvnconqn.supabase.co/functions/v1/mail-send';
begin
  -- v0.23.1: corrige cct_email_db (segredo procurado como MAIL_API_KEY, no Vault esta mail_api_key; destinatarios text[] em coluna jsonb)
  select email_destino_teste into v_teste from cct_config where tenant_id = p_tenant;
  if nullif(trim(v_teste), '') is not null then
    v_html := '<p style="font-size:12px;color:#a93226">[MODO TESTE] destinatarios reais: ' || array_to_string(p_para, ', ') || '</p>' || p_html;
    v_para := array[lower(trim(v_teste))];
  end if;
  v_para := array(select distinct lower(trim(x)) from unnest(v_para) x where nullif(trim(x), '') is not null);
  begin
    select decrypted_secret into v_key from vault.decrypted_secrets where name in ('MAIL_API_KEY', 'mail_api_key') order by (name = 'MAIL_API_KEY') desc limit 1;
  exception when others then v_key := null; end;
  if v_key is null then v_status := 'NAO_ENVIADA'; v_erro := 'chave do e-mail (MAIL_API_KEY ou mail_api_key) ausente no Vault';
  elsif coalesce(array_length(v_para, 1), 0) = 0 then v_status := 'NAO_ENVIADA'; v_erro := 'nenhum destinatario';
  else
    foreach d in array v_para loop
      begin
        perform net.http_post(v_url, jsonb_build_object('to', d, 'assunto', p_assunto, 'html', v_html, 'app', 'CCT Monitor'), '{}'::jsonb,
                              jsonb_build_object('Content-Type', 'application/json', 'x-api-key', v_key), 15000);
      exception when others then v_status := 'NAO_ENVIADA'; v_erro := sqlerrm; end;
    end loop;
  end if;
  insert into cct_notificacoes (tenant_id, tipo, destinatarios, assunto, status, erro, tentativas, enviada_em)
       values (p_tenant, p_tipo, to_jsonb(v_para), p_assunto, v_status, coalesce(v_erro, 'enviado pelo banco (vigia/pg_net)'), 1,
               case when v_status = 'ENVIADA' then now() end);
  return v_status;
end $function$;

create or replace function public.cct_alertas_robo(p_ref timestamptz default now())
returns jsonb language plpgsql security definer set search_path = public as $function$
declare t record; s jsonb; v_abertos int; n_alerta int := 0; n_res int := 0; e record; v_dest text[]; v_msg text;
        abertos text[] := array['NOVO','EM_NOVA_TENTATIVA','PERSISTENTE','EM_ANALISE'];
begin
  -- v0.23.1: mesmo vigia de cct_verificar_heartbeat(timestamptz), com o e-mail corrigido (cct_email_db2)
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
        perform public.cct_registrar_incidente(t.tenant_id, 'APLICATIVO:execucao-diaria', 'APLICATIVO', 'CRITICO', v_msg, null, null, s);
        perform public.cct_email_db2(t.tenant_id, v_dest, 'Artecon · CCT Monitor — ROBÔ NÃO EXECUTOU',
                                     '<p>' || v_msg || '</p><p>O banco tentou acionar o robô no horário e ele não iniciou. Verifique em GitHub → Actions → CCT Monitor. Aviso enviado pelo vigia do banco de dados.</p>');
        n_alerta := n_alerta + 1;
      end if;
    elsif s ->> 'situacao' in ('NORMAL', 'COM_ALERTAS', 'EXECUTANDO') then
      n_res := n_res + coalesce(public.cct_resolver_incidente(t.tenant_id, 'APLICATIVO:execucao-diaria', 'automatico'), 0);
    end if;
    for e in select * from cct_execucoes where tenant_id = t.tenant_id and resultado in ('INTERROMPIDA','ERRO_TOTAL') and not alertado order by inicio loop
      v_msg := format('EXECUÇÃO %s – iniciada %s (%s): %s', e.resultado, to_char(e.inicio at time zone 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'),
                      coalesce(e.origem, '?'), coalesce(e.motivo, 'sem motivo registrado'));
      perform public.cct_registrar_incidente(t.tenant_id, 'APLICATIVO:execucao:' || e.id::text, 'APLICATIVO', 'CRITICO', v_msg, null, null, to_jsonb(e));
      perform public.cct_email_db2(t.tenant_id, v_dest, 'Artecon · CCT Monitor — Execução ' || lower(e.resultado),
                                   '<p>' || v_msg || '</p>' || coalesce('<p><a href="' || e.run_url || '">Abrir o log no GitHub</a></p>', ''));
      update cct_execucoes set alertado = true where id = e.id;
      n_alerta := n_alerta + 1;
    end loop;
  end loop;
  return jsonb_build_object('alertas', n_alerta, 'resolvidos', n_res, 'ref', p_ref);
end $function$;

create or replace function public.cct_vigia_robo(p_ref timestamptz default now(), p_disparar boolean default true, p_alertar boolean default true)
returns jsonb language plpgsql security definer set search_path = public as $function$
declare t record; c record; v_prev timestamptz; v_pend int; v_rodando boolean; v_tentativas int; v_key text; v_req bigint;
        v_acao text; v_res jsonb := '[]'::jsonb; v_hb jsonb;
begin
  -- v0.23.1: o agendamento do GitHub atrasa horas; o banco (pg_cron, a cada 15 min) aciona o robo logo apos o horario configurado
  for t in select tenant_id from cct_config loop
    select * into c from cct_config where tenant_id = t.tenant_id;
    v_prev := public.cct_ultima_prevista(t.tenant_id, p_ref);
    v_acao := 'nada'; v_pend := null;
    if v_prev is not null and p_ref < v_prev + interval '8 hours' then
      select count(*) into v_pend from cct_sindicatos
       where tenant_id = t.tenant_id and ativo and monitorar and (ultima_consulta is null or ultima_consulta < v_prev);
      select exists (select 1 from cct_execucoes where tenant_id = t.tenant_id and resultado = 'EM_EXECUCAO' and inicio > now() - interval '4 hours') into v_rodando;
      select count(*) into v_tentativas from cct_execucoes where tenant_id = t.tenant_id and inicio >= v_prev and origem = 'github-actions';
      if v_pend = 0 then v_acao := 'em dia';
      elsif v_rodando then v_acao := 'robo executando';
      elsif v_tentativas >= 3 then v_acao := 'limite de 3 execucoes desde o horario';
      elsif c.ultimo_disparo_em is not null and c.ultimo_disparo_em > now() - interval '40 minutes' then v_acao := 'aguardando disparo recente';
      else v_acao := 'disparar';
      end if;
    end if;
    if v_acao = 'disparar' and p_disparar then
      select decrypted_secret into v_key from vault.decrypted_secrets where name = 'GITHUB_DISPATCH_TOKEN' limit 1;
      if v_key is null or coalesce(c.github_repo, '') = '' then
        v_acao := 'sem token ou repositorio';
      else
        begin
          v_req := net.http_post('https://api.github.com/repos/' || c.github_repo || '/actions/workflows/monitor-cct.yml/dispatches',
                                 jsonb_build_object('ref', coalesce(nullif(c.github_branch, ''), 'main'), 'inputs', jsonb_build_object('forcar', 'false')),
                                 '{}'::jsonb,
                                 jsonb_build_object('Accept', 'application/vnd.github+json', 'Authorization', 'Bearer ' || v_key, 'X-GitHub-Api-Version', '2022-11-28',
                                                    'User-Agent', 'cct-monitor-supabase', 'Content-Type', 'application/json'),
                                 15000);
          update cct_config set ultimo_disparo_req = v_req, ultimo_disparo_em = now(), ultimo_disparo_tipo = 'agenda' where tenant_id = t.tenant_id;
          v_acao := 'disparado';
        exception when others then v_acao := 'falha ao disparar: ' || sqlerrm; end;
      end if;
    end if;
    v_res := v_res || jsonb_build_object('tenant', t.tenant_id, 'prevista', v_prev, 'pendentes', v_pend, 'acao', v_acao, 'req', v_req);
  end loop;
  -- alertas: pula o ciclo logo apos um disparo, enquanto o robo abre a execucao no GitHub
  if p_alertar then
    if exists (select 1 from cct_config where ultimo_disparo_tipo = 'agenda' and ultimo_disparo_em > now() - interval '30 minutes') then
      v_hb := jsonb_build_object('pulado', 'disparo recente');
    else
      v_hb := public.cct_alertas_robo(p_ref);
    end if;
  end if;
  return jsonb_build_object('ref', p_ref, 'agenda', v_res, 'alertas', v_hb);
end $function$;

-- vigia a cada 15 min (substitui o comando que falhava)
select cron.schedule('cct-heartbeat', '*/15 * * * *', 'select public.cct_vigia_robo(now())');

-- C) funções internas: só o robô (service_role) e o pg_cron (postgres)
revoke all on function public.cct_email_db2(uuid, text[], text, text, text) from public, anon, authenticated;
revoke all on function public.cct_alertas_robo(timestamptz) from public, anon, authenticated;
revoke all on function public.cct_vigia_robo(timestamptz, boolean, boolean) from public, anon, authenticated;
revoke all on function public.cct_registrar_incidente(uuid, text, text, text, text, uuid, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.cct_resolver_incidente(uuid, text, text) from public, anon, authenticated;
revoke all on function public.cct_execucao_encerrar(uuid, text, text, jsonb) from public, anon, authenticated;
revoke all on function public.cct_verificar_heartbeat(integer) from public, anon, authenticated;
revoke all on function public.cct_verificar_heartbeat(timestamptz) from public, anon, authenticated;
revoke all on function public.cct_email_db(uuid, text[], text, text, text) from public, anon, authenticated;
grant execute on function public.cct_email_db2(uuid, text[], text, text, text) to service_role;
grant execute on function public.cct_alertas_robo(timestamptz) to service_role;
grant execute on function public.cct_vigia_robo(timestamptz, boolean, boolean) to service_role;
grant execute on function public.cct_registrar_incidente(uuid, text, text, text, text, uuid, uuid, jsonb) to service_role;
grant execute on function public.cct_resolver_incidente(uuid, text, text) to service_role;
grant execute on function public.cct_execucao_encerrar(uuid, text, text, jsonb) to service_role;

-- Limpeza OPCIONAL (rodar no SQL Editor; o conector do Supabase não executa DROP):
--   drop function if exists public.cct_verificar_heartbeat(integer);          -- versão antiga que causava a ambiguidade
--   drop function if exists public.cct_vigia(timestamptz, boolean);           -- primeira versão de 13/10, substituída por cct_vigia_robo
