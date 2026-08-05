# Changelog

## [1.2.0] — 2026-08-05

### Adicionado
- feat: **rotação de mensagens** — 5 variações mais suaves (sem apelo de desconto, sem emoji), sorteando por envio pra cortar o flag de "mesma mensagem em massa"
- feat: **pausa humana intercalada** entre os DMs — no meio da leva sai, navega no feed (rola/curte) e volta, quebrando o padrão de rajada
- feat: **contagem regressiva nas pausas** (marcador `[espera]`) no app/Live Activity + timeout defensivo do Playwright

## [1.1.2] — 2026-07-29

### Adicionado
- feat: profile do Chromium **por conta** (`IG_USER_DATA_DIR`) — o backend aponta um profile por conta pra isolar o "device" no IG (evita que conectar/rodar uma conta derrube a sessão das outras)

### Modificado
- update: mensagem pós-login **universal** ("já pode rodar os bots") em vez da dica do CLI

## [1.1.1] — 2026-07-27

### Corrigido
- fix: **detecção de soft-block de DM** — `criar_thread` voltando 403 genérico ("Ocorreu um erro. Tente novamente") ou 200 sem `thread_v2_id` agora loga o status HTTP + trecho do corpo (era `None` silencioso, sem motivo) e **para o run após 6 falhas seguidas**, em vez de martelar a lista inteira e piorar o bloqueio

### Documentação
- docs: README documenta a parada por 403/soft-block após 6 falhas de thread

## [1.1.0] — 2026-07-22

### Adicionado
- feat: **sessão universal** do Instagram — uma pra todos os bots, no dir pai comum (ninguém importa num bot e copia pros outros)
- feat: **log isolado por run** (`output/logs/run_<timestamp>.log`, mantém os 30 mais recentes)

### Corrigido
- fix: **não remanda DM** pra quem já recebeu — verificação dupla que abre a conversa e procura a marca do template (`ja_mandou_msg`), além do `state.json`
- fix: run **não trava mais no proxy** — removidos os `goto` por-pessoa que engasgavam no túnel; timeouts (`AbortController`) nas chamadas e retry no `criar_thread`

### Modificado
- update: janela de horário desligada por padrão (`USAR_JANELA=False`)
- update: usa o `ds_user_id` da conta REALMENTE logada no browser (não do arquivo de sessão) — evita gravar estado na conta errada

### Documentação
- docs: README — caminho de setup, dedup duplo e `USAR_JANELA`

## [1.0.1] — 2026-06-28

### Modificado
- update: caps de volume desligados por padrão (`MAX_DMS_DIA`/`HORA`/`POR_RUN` = 0) — manda pra todos os novos de uma vez; delays, janela e kill-switch seguem ligados

### Documentação
- docs: tabela de limites do README atualizada (caps desligados, delays atuais)

## [1.0.0] — 2026-06-26

### Adicionado
- feat: worker que manda DM pros novos seguidores lidos da aba de notificações
- Lê o feed de atividades (`PolarisActivityFeedStoriesViewQuery`) e filtra "começou a seguir você"
- Navegação humana: abre o perfil → cria a conversa → manda a DM, com dwells e pausas aleatórias
- Retomada: 1ª run começa do `COMECAR_DE`; salva o último e nos próximos runs só pega os novos
- Mensagem com o nick do destinatário (suporta spintax, mas configurada fixa)
- Caps por dia/hora/run, janela de horário, kill-switch de bloqueio e saldo final
- `--login`, `--import-cookies`, `--dry-run`, `--start-from`, `--start-from-oldest`, `--debug`
- Mensagens de erro explicativas (status HTTP em português)
