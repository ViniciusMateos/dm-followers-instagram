"""
Configuração do worker dm_followers — manda DM pros novos seguidores.

Lê a aba de notificações ("começou a seguir você"), processa do mais antigo pro
mais recente, manda a mensagem (com o nick da pessoa na 1ª linha) e salva o último
processado pra retomar. DM é o automatismo de MAIOR risco de ban — caps minúsculos.

Endpoints em ../../DM_API_REFERENCE.md.
"""
import os

_BASE = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────── Sessão / navegador ─────────────────
# Sessão do IG (cookies). Fica FORA do browser_profile de propósito: o Chromium
# deste server não persiste cookie nenhum em disco (testado — nem os que o próprio
# site seta sobrevivem a fechar/reabrir). Então a sessão vive aqui e é reinjetada a
# cada abertura; o perfil serve só pra cache/fingerprint.
# Sessão UNIVERSAL: UMA pra todos os bots (é a mesma conta de IG). Fica no dir pai comum
# (quase_nada_bots/), não no dir do worker — assim TODOS os bots leem a MESMA sessão, sem
# ninguém "importar num bot e copiar pros outros". Override por IG_SESSION_FILE.
SESSION_FILE = os.environ.get("IG_SESSION_FILE") or os.path.join(
    os.path.dirname(os.path.dirname(_BASE)), "session_cookies.json")
# profile do Chromium — sobrescritível por env (IG_USER_DATA_DIR). O backend aponta um profile
# POR CONTA, pra cada conta ser um "device" distinto pro IG (senão conectar/rodar uma conta mata
# a sessão das outras no mesmo device compartilhado).
USER_DATA_DIR = os.environ.get("IG_USER_DATA_DIR") or os.path.join(_BASE, "browser_profile")


# ─────────────────────── Proxy (opcional, configurável pelo app) ──────────
# Grava proxy.json {enabled, server, username, password}. Formato do Playwright.
def _carregar_proxy():
    import json
    f = os.path.join(_BASE, "proxy.json")
    if os.path.exists(f):
        try:
            d = json.load(open(f, encoding="utf-8"))
            if d.get("enabled") and d.get("server"):
                return {k: d[k] for k in ("server", "username", "password") if d.get(k)}
        except Exception:
            pass
    return None


PROXY = _carregar_proxy()


def _envbool(nome, padrao):
    v = os.environ.get(nome)
    return padrao if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# Default = PC (headed). No SERVIDOR headless: IG_HEADLESS=1 e IG_CHROME_REAL=0 no .env.
HEADLESS = _envbool("IG_HEADLESS", False)
USAR_CHROME_REAL = _envbool("IG_CHROME_REAL", True)
LOCALE = "pt-BR"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# ───────────────── Constantes da API (da captura) ───────────
IG_APP_ID = "936619743392459"
ASBD_ID = "359341"
DOC_ACTIVITY = "26398841236455905"     # PolarisActivityFeedStoriesViewQuery (/graphql/query)
DOC_DM_SEND = "26911679871773184"      # IGDirectTextSendMutation (/api/graphql)

# ───────────────────── MENSAGEM ─────────────────────────────
# {username} é trocado pelo nick do destinatário. (Mensagem fixa, sem variação.)
# Ainda suporta spintax {a|b|c} se um dia quiser variar — mas aqui está fixa.
MENSAGEM = (
    "{username},\n\n"
    "Siga o @brechoquasenadaa pra acompanhar os próximos drops!!\n\n"
    "Primeira compra no brechó tem desconto de 10% em qualquer item!"
)

# Âncora da VERIFICAÇÃO DUPLA: antes de enviar, o bot olha DENTRO da conversa e, se já tem
# uma mensagem NOSSA contendo este trecho, NÃO remanda. É um pedaço FIXO do template (o @).
MARCA_TEMPLATE = "brechoquasenadaa"

# ───────────────────── LIMITES DE SEGURANÇA ─────────────────
# Lista pequena (<50) → caps generosos pra processar tudo numa run. O kill-switch
# segue ligado: se o IG bloquear no meio, para na hora e salva de onde parou.
APLICAR_CAPS = True
MAX_DMS_DIA = 0             # 0 = SEM cap diário
MAX_DMS_HORA = 0           # 0 = SEM cap horário
MAX_DMS_POR_RUN = 0        # 0 = SEM limite por execução (manda todos os novos de uma vez)

DELAY_DM = (5, 20)          # entre uma pessoa e outra
PAUSA_LONGA_CADA = 0        # 0 = SEM pausa longa
PAUSA_LONGA = (120, 300)    # (ignorado se PAUSA_LONGA_CADA = 0)
DELAY_ACAO_UI = (1.5, 4.0)  # dwell ao abrir perfil / conversa

# Janela de horário: DESLIGADA por padrão — roda a QUALQUER hora. Ligue USAR_JANELA=True
# pra limitar ao ACTIVE_HOURS abaixo (menos cara de bot de madrugada). Vinicius pediu sem.
USAR_JANELA = False
ACTIVE_HOURS = (9, 23)       # só vale se USAR_JANELA=True

# 1ª RUN: de qual seguidor começar (do mais antigo dele pro mais recente).
# Depois disso ele salva o último e retoma sozinho (ignora este valor).
COMECAR_DE = "n.mondra"

# Comportamento fixo: só processa "começou a seguir você" — o filtro é hardcoded em
# ig.novos_seguidores (type == 3). Esta flag não é lida, fica só de registro.
SO_NOVOS_SEGUIDORES = True

# ─────────────────────────── Paths ──────────────────────────
OUTPUT_DIR = os.path.join(_BASE, "output")
STATE_FILE = os.path.join(OUTPUT_DIR, "state.json")
LOG_FILE = os.path.join(OUTPUT_DIR, "run.log")


def conta_da_sessao():
    """ds_user_id da sessão salva — QUEM está logado agora.

    O estado (quem já recebeu DM / quem já foi seguido) é POR CONTA: trocar de conta e
    herdar o histórico da anterior faz o bot achar que já falou com todo mundo e não fazer
    nada — ou pior, pular gente que nunca foi contatada por ESTA conta.

    Lê do arquivo de sessão pra não precisar abrir o navegador antes de montar o State.
    """
    import json
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            for c in json.load(f):
                if c.get("name") == "ds_user_id":
                    v = str(c.get("value") or "").strip()
                    if v:
                        return v
    except Exception:
        pass
    return ""


def state_file(conta=""):
    """Arquivo de estado DESTA conta.

    Sem conta identificada NÃO cai no state.json antigo de propósito: herdar o histórico de
    outra conta faz o bot pular gente que esta conta nunca contatou (já aconteceu). Melhor
    começar limpo do que mentir sobre o que já foi feito.
    """
    nome = f"state-{conta}.json" if conta else "state-desconhecida.json"
    return os.path.join(OUTPUT_DIR, nome)
