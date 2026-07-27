# dm_followers

> ⚠️ **DM em massa é o sinal de spam nº 1 do Instagram** — risco de bloqueio bem
> maior que follow. Comece com caps minúsculos, varie a mensagem (spintax) e rode
> `--dry-run` primeiro. Viola os ToS; é a sua conta e o seu risco.

Manda DM pros **novos seguidores** lidos da **aba de notificações** ("começou a
seguir você"). Processa do mais antigo pro mais novo, com retomada: na 1ª vez você
escolhe de quem começar; depois ele só pega os novos desde o último.

Mesma base do like-bot: Chrome logado via Playwright, chamadas internas via página
logada. Endpoints em [`../../DM_API_REFERENCE.md`](../../DM_API_REFERENCE.md).

## Setup
```bash
cd projetos/quase-nada-bots/workers/dm-followers-instagram
pip install -r requirements.txt
python -m playwright install chromium
```

## Uso
```bash
python main.py --login                              # 1ª vez: login manual
python main.py --dry-run --start-from juliatilco    # simula a partir de um seguidor
python main.py --start-from juliatilco              # 1ª vez pra valer
python main.py                                      # próximos runs: só os novos
python main.py --debug                              # dump do feed em output/
```

### Retomada
Na 1ª vez, `--start-from <username>` define de quem começar (do mais antigo pra
frente). Ao enviar, salva o `timestamp` do último; nos próximos runs só processa
quem é mais novo que o salvo. **Quem já recebeu DM nunca recebe de novo** — via
`state.json` E uma **verificação dupla** que abre a conversa e procura a nossa marca
(`MARCA_TEMPLATE`) antes de mandar, mesmo que o `state` falhe ou zere.

## Mensagem — `config.py` → `MENSAGEM`
`{username}` vira o nick do destinatário. Suporta **spintax** `{a|b|c}` (escolhe um
aleatório) pra variar o texto e reduzir flag de spam. **Varie bastante.**

## Limites — `config.py`
| Parâmetro | Padrão | O quê |
|-----------|--------|-------|
| `MAX_DMS_DIA` | 0 | 0 = sem cap diário |
| `MAX_DMS_HORA` | 0 | 0 = sem cap horário |
| `MAX_DMS_POR_RUN` | 0 | 0 = manda pra todos os novos de uma vez |
| `DELAY_DM` | 5–20 s | pausa entre uma pessoa e outra |
| `PAUSA_LONGA_CADA` | 0 | 0 = sem pausa longa |
| `USAR_JANELA` | False | False = roda a qualquer hora; True = limita ao `ACTIVE_HOURS` |
| `ACTIVE_HOURS` | 9–23 | horário humano (só vale com `USAR_JANELA=True`) |
| `COMECAR_DE` | — | 1ª run: de qual seguidor começar |

> Caps de volume desligados por padrão (lista pequena). O **kill-switch** continua
> sendo a proteção: para no bloqueio real. Suba/ligue os caps se for escalar.

Kill-switch: qualquer bloqueio (`feedback_required`/`spam`/429/HTML) **para o run** e
imprime o saldo (enviadas / puladas). Erro detalhado vai pra `output/logs/`.

Alguns action-blocks não vêm num sinal estruturado — o IG responde `criar_thread` com
403 genérico ("Ocorreu um erro. Tente novamente") ou 200 sem `thread_v2_id`. Esses
casos são logados com o status HTTP + trecho do corpo (pra ver o motivo real) e, se
acontecerem **6 vezes seguidas**, o run **para limpo** em vez de martelar a lista
inteira e piorar o bloqueio.

## Arquivos
| Arquivo | Papel |
|---------|-------|
| `config.py` | mensagem, caps, constantes da API |
| `safety.py` | estado/retomada, caps, delays, kill-switch |
| `ig.py` | sessão Playwright + feed de notificações, criar thread, enviar DM |
| `main.py` | retomada + loop de envio + CLI |
