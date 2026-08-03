"""
Cliente do Instagram pro dm_followers. Mesma estratégia do like-bot: dirigir um
Chrome logado via Playwright e fazer as chamadas de dentro da página logada (fetch
same-origin). Endpoints em ../../DM_API_REFERENCE.md.
"""
import json
import os
import re
import time
import random

from playwright.sync_api import sync_playwright

import config
from safety import log, checar_bloqueio, BloqueioDetectado, explicar_status

# ───────────── JS injetado na página logada ─────────────
JS_TOKENS = r"""
() => {
  const html = document.documentElement.innerHTML;
  const pick = (re) => { const m = html.match(re); return m ? m[1] : null; };
  const cookie = (n) => {
    const m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]+)'));
    return m ? decodeURIComponent(m[1]) : null;
  };
  const dtsg = pick(/"DTSGInitialData",\[\],\{"token":"([^"]+)"/)
            || pick(/"dtsg":\{"token":"([^"]+)"/)
            || pick(/name="fb_dtsg" value="([^"]+)"/);
  const lsd = pick(/"LSD",\[\],\{"token":"([^"]+)"/)
            || pick(/"lsd":\{"token":"([^"]+)"/);
  const av = pick(/"actorID":"(\d+)"/) || pick(/"IG_USER_EIMU":"(\d+)"/)
          || pick(/"viewerId":"(\d+)"/) || cookie('ds_user_id');
  let claim = '0';
  try { claim = sessionStorage.getItem('www-claim-v2') || '0'; } catch (e) {}
  return { dtsg, lsd, av, claim, csrf: cookie('csrftoken'), dsuser: cookie('ds_user_id') };
}
"""

JS_GRAPHQL = r"""
async (p) => {
  const body = new URLSearchParams();
  body.set('av', p.av);
  body.set('__a', '1');
  body.set('__comet_req', '7');
  body.set('dpr', '1');
  body.set('fb_dtsg', p.dtsg);
  body.set('jazoest', p.jazoest);
  body.set('lsd', p.lsd);
  body.set('fb_api_caller_class', 'RelayModern');
  body.set('fb_api_req_friendly_name', p.friendly);
  body.set('server_timestamps', 'true');
  body.set('doc_id', p.doc_id);
  body.set('variables', p.variables);
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 25000);   // proxy morto NÃO pendura pra sempre
  try {
    const r = await fetch(p.endpoint || '/api/graphql', {
      method: 'POST', credentials: 'include', signal: ctrl.signal, headers: {
        'content-type': 'application/x-www-form-urlencoded',
        'x-fb-friendly-name': p.friendly, 'x-csrftoken': p.csrf,
        'x-asbd-id': p.asbd, 'x-ig-app-id': p.appid,
      }, body: body.toString() });
    return { status: r.status, text: await r.text() };
  } finally { clearTimeout(to); }
}
"""

JS_CREATE_THREAD = r"""
async (p) => {
  const body = new URLSearchParams();
  body.set('recipient_users', '["' + p.pk + '"]');
  body.set('fb_dtsg', p.dtsg);
  body.set('jazoest', p.jazoest);
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 25000);   // proxy morto NÃO pendura pra sempre
  try {
    const r = await fetch('/api/v1/direct_v2/create_group_thread/', {
      method: 'POST', credentials: 'include', signal: ctrl.signal, headers: {
        'content-type': 'application/x-www-form-urlencoded',
        'x-ig-app-id': p.appid, 'x-asbd-id': p.asbd, 'x-csrftoken': p.csrf,
        'x-requested-with': 'XMLHttpRequest', 'x-ig-www-claim': p.claim,
      }, body: body.toString() });
    return { status: r.status, text: await r.text() };
  } finally { clearTimeout(to); }
}
"""


JS_API_GET = r"""
async (p) => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 25000);   // proxy morto NÃO pendura pra sempre
  try {
    const r = await fetch(p.url, { credentials: 'include', signal: ctrl.signal, headers: {
      'x-ig-app-id': p.appid, 'x-asbd-id': p.asbd, 'x-csrftoken': p.csrf,
      'x-requested-with': 'XMLHttpRequest', 'x-ig-www-claim': p.claim,
    }});
    return { status: r.status, text: await r.text() };
  } finally { clearTimeout(to); }
}
"""


def _jazoest(dtsg):
    return "2" + str(sum(ord(c) for c in dtsg)) if dtsg else ""


def _parse_json(text):
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    return json.loads(text)


def _otid():
    """offline_threading_id: inteiro grande único por mensagem."""
    return str(int(time.time() * 1000) * 1000 + random.randint(0, 999999))


class IG:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._pw = None
        self.ctx = None
        self.page = None
        self.tokens = {}

    # ─────────── ciclo de vida ───────────
    def abrir(self):
        self._pw = sync_playwright().start()
        kwargs = dict(
            headless=config.HEADLESS, locale=config.LOCALE, user_agent=config.USER_AGENT,
            viewport={"width": 1280, "height": 820},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        if getattr(config, "PROXY", None):
            kwargs["proxy"] = config.PROXY
            log.info("Proxy ativo: %s", config.PROXY.get("server"))
        if getattr(config, "USAR_CHROME_REAL", False):
            kwargs["channel"] = "chrome"
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(config.USER_DATA_DIR, **kwargs)
        except Exception as e:
            if "channel" in kwargs:
                log.warning("Chrome real não encontrado (%s); usando Chromium.", e)
                kwargs.pop("channel")
                self.ctx = self._pw.chromium.launch_persistent_context(config.USER_DATA_DIR, **kwargs)
            else:
                raise
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(20000)   # ação do Playwright falha em 20s em vez de pendurar
        self._restaurar_sessao()   # o perfil não guarda cookie; a sessão vem do arquivo
        return self

    def fechar(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def __enter__(self):
        return self.abrir()

    def __exit__(self, *a):
        self.fechar()

    # ─────────── sessão ───────────
    def ir(self, url, timeout=30000):
        # 30s (era 60s): as requisições saem pelo túnel reverso até o PC de casa, então um
        # goto que não resolveu em 30s não vai resolver — melhor falhar rápido do que pendurar
        # a run inteira meio minuto por página.
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        self.page.wait_for_timeout(1500)

    def visitar_perfil(self, username, timeout=20000):
        """Abre o perfil só pra dar um 'dwell' humano antes da DM. NÃO é essencial — a DM
        vai pela thread, não pelo perfil. Por isso é NÃO-FATAL: pelo túnel o perfil às vezes
        demora/estoura; se der ruim, a gente segue direto pra DM em vez de travar o run
        (era isso que pendurava tudo em 'Page.goto: Timeout 60000ms')."""
        try:
            self.ir(f"https://www.instagram.com/{username}/", timeout=timeout)
            return True
        except Exception as e:
            log.warning("  ~ perfil de @%s não carregou a tempo — sigo direto pra DM (%s)",
                        username, str(e).splitlines()[0][:60])
            return False

    def _cookies(self):
        try:
            cks = self.ctx.cookies("https://www.instagram.com")
        except Exception:
            cks = self.ctx.cookies()
        return {c["name"]: c["value"] for c in cks}

    def usuario(self):
        """@username da conta logada AGORA.

        Vem do /data/shared_data/ (o viewer), que é a fonte que o próprio IG usa. O
        ds_user_id do cookie é só um número — e saber QUAL conta está rodando importa:
        já rodamos com a conta errada sem perceber.

        Devolve "" se não conseguir (nunca derruba a run por causa disso).
        """
        try:
            return self.page.evaluate("""async () => {
                const r = await fetch('/data/shared_data/');
                if (!r.ok) return '';
                const j = await r.json();
                return (j.config && j.config.viewer && j.config.viewer.username) || '';
            }""") or ""
        except Exception:
            return ""

    def logado(self):
        return bool(self._cookies().get("sessionid"))

    def importar_cookies(self, cookies):
        """Injeta os cookies do navegador normal e, se logou, GRAVA a sessão em disco.

        Não dá pra confiar no browser_profile: o Chromium deste server não persiste cookie
        (testado). Por isso a sessão é salva em SESSION_FILE e reinjetada a cada abrir().
        """
        self.ctx.add_cookies(cookies)
        # navega pra "assentar" a sessão, mas NÃO-FATAL: pelo túnel o instagram.com às vezes
        # estoura o timeout, e isso NÃO quer dizer que a sessão é ruim — os cookies já foram
        # injetados e o logado() checa o COOKIE, não a página. Tenta 2x com folga e segue.
        for _tent in range(2):
            try:
                self.ir("https://www.instagram.com/", timeout=45000)
                break
            except Exception as e:
                log.warning("~ instagram.com demorou a abrir (%d/2): %s — sigo pro cookie",
                            _tent + 1, str(e).splitlines()[0][:50])
        if not self.logado():
            return False
        self.salvar_sessao()
        return True

    def salvar_sessao(self):
        """Grava os cookies atuais do instagram.com — é isso que sobrevive entre execuções."""
        try:
            cks = self.ctx.cookies("https://www.instagram.com")
            with open(config.SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(cks, f)
            os.chmod(config.SESSION_FILE, 0o600)   # é credencial: só o dono lê
            log.info("Sessão salva (%d cookies).", len(cks))
        except Exception as e:
            log.warning("Não consegui salvar a sessão: %s", e)

    def _restaurar_sessao(self):
        """Reinjeta a sessão salva no contexto recém-aberto. Silencioso quando não há."""
        if not os.path.exists(config.SESSION_FILE):
            return
        try:
            with open(config.SESSION_FILE, encoding="utf-8") as f:
                cks = json.load(f)
            if cks:
                self.ctx.add_cookies(cks)
        except Exception as e:
            log.warning("Não consegui restaurar a sessão salva: %s", e)

    def carregar_tokens(self):
        self.tokens = self.page.evaluate(JS_TOKENS)
        ck = self._cookies()
        self.tokens["csrf"] = self.tokens.get("csrf") or ck.get("csrftoken")
        self.tokens["av"] = self.tokens.get("av") or ck.get("ds_user_id")
        self.tokens["jazoest"] = _jazoest(self.tokens.get("dtsg") or "")
        falta = [k for k in ("csrf", "dtsg", "lsd") if not self.tokens.get(k)]
        if falta:
            log.warning("Tokens ausentes: %s — confira se a página carregou logada.", falta)
        return self.tokens

    def _base(self):
        return {"appid": config.IG_APP_ID, "asbd": config.ASBD_ID,
                "csrf": self.tokens.get("csrf"), "claim": self.tokens.get("claim", "0"),
                "av": self.tokens.get("av"), "dtsg": self.tokens.get("dtsg"),
                "lsd": self.tokens.get("lsd"), "jazoest": self.tokens.get("jazoest")}

    # ─────────── operações ───────────
    def novos_seguidores(self):
        """Lê a aba de notificações e devolve quem 'começou a seguir você',
        do mais antigo pro mais novo: [{pk, username, timestamp}]."""
        res = self.page.evaluate(JS_GRAPHQL, {
            **self._base(), "endpoint": "/graphql/query",
            "friendly": "PolarisActivityFeedStoriesViewQuery", "doc_id": config.DOC_ACTIVITY,
            "variables": json.dumps({"inbox_request_data": {}, "pending_request_data": {}},
                                    separators=(",", ":"))})
        checar_bloqueio(res["status"], res["text"])
        data = _parse_json(res["text"])
        try:
            inbox = data["data"]["xdt_activity_inbox"]
        except (KeyError, TypeError):
            log.error("Feed de atividades em formato inesperado. Rode com --debug.")
            return []
        stories = (inbox.get("new_stories") or []) + (inbox.get("old_stories") or [])
        out = []
        for s in stories:
            if s.get("type") != 3:                      # 3 = "começou a seguir você"
                continue
            args = s.get("args") or {}
            users = args.get("users") or []
            if not users:
                continue
            u = users[0]
            out.append({"pk": str(u.get("pk") or u.get("id")),
                        "username": u.get("username", "?"),
                        "timestamp": float(args.get("timestamp") or 0)})
        out.sort(key=lambda x: x["timestamp"])          # antigo -> novo
        return out

    def criar_thread(self, pk, tentativas=3):
        """Cria/abre a DM com o usuário; retorna thread_v2_id.

        Resiliente ao 'Execution context was destroyed': quando a visita de perfil estoura o
        timeout, o load continua em segundo plano e o page.evaluate aqui pode rodar bem na hora
        em que a navegação COMMITA — aí o contexto morre e explode. Não é erro de verdade: é só
        a página assentando. Espera e repete (ler é idempotente)."""
        ult = None
        for t in range(tentativas):
            try:
                res = self.page.evaluate(JS_CREATE_THREAD, {**self._base(), "pk": str(pk)})
                checar_bloqueio(res["status"], res["text"])
                txt = (res.get("text") or "").strip()
                if not txt:
                    log.warning("  ~ criar_thread: HTTP %s corpo VAZIO (soft-block de DM?)", res["status"])
                    return None
                j = _parse_json(txt)
                tid = j.get("thread_v2_id") or (j.get("thread") or {}).get("thread_v2_id")
                if not tid:   # 200 sem o id = assinatura de soft-block; loga pra a gente VER
                    log.warning("  ~ criar_thread: HTTP %s sem thread_v2_id (soft-block?) — %s",
                                res["status"], txt[:80])
                return tid
            except Exception as e:
                m = str(e).lower()
                # transitório = página assentando (visita de perfil que estourou) OU proxy/rede
                # caiu no meio (abort/failed to fetch/ERR_PROXY). Nenhum é fatal: espera e repete;
                # se não vier, o loop de DM PULA essa pessoa — não derruba/pendura o run inteiro.
                transitorio = ("execution context" in m or "most likely because of a navigation" in m
                               or "abort" in m or "failed to fetch" in m or "networkerror" in m
                               or "err_proxy" in m or "net::err" in m or "load failed" in m)
                if transitorio:
                    ult = e
                    log.warning("  ~ criar_thread: transitório (%s) — tentativa %d/%d",
                                str(e).splitlines()[0][:45], t + 1, tentativas)
                    self.page.wait_for_timeout(2500)
                    continue
                raise
        log.warning("  ~ criar_thread desistiu (%s) — pulo essa pessoa",
                    str(ult).splitlines()[0][:45] if ult else "?")
        return None

    def ja_mandou_msg(self, thread_id, marca):
        """VERIFICAÇÃO DUPLA anti-duplicata: True se a conversa JÁ tem uma mensagem NOSSA
        contendo `marca` (a âncora do template). Mesmo que o state falhe/zere, isso impede
        remandar a DM. Em caso de dúvida (não deu pra checar), devolve False — o state é o
        filtro primário; aqui é a rede de segurança, não pode bloquear envio legítimo à toa."""
        url = (f"https://www.instagram.com/api/v1/direct_v2/threads/{thread_id}/"
               "?visual_message_return_type=unseen&limit=40")
        try:
            res = self.page.evaluate(JS_API_GET, {**self._base(), "url": url})
        except Exception:
            return False
        if res.get("status") != 200:
            return False
        try:
            th = _parse_json(res["text"]).get("thread") or {}
        except Exception:
            return False
        viewer = str(th.get("viewer_id") or self.tokens.get("dsuser")
                     or self._cookies().get("ds_user_id") or "")
        alvo = (marca or "").lower()
        for it in th.get("items") or []:
            if viewer and str(it.get("user_id")) != viewer:   # só as NOSSAS mensagens
                continue
            if alvo and alvo in str(it.get("text") or "").lower():
                return True
        return False

    def enviar_dm(self, thread_v2_id, texto):
        """Envia o texto na thread. Retorna o dict de resposta."""
        variables = {
            "ig_thread_igid": str(thread_v2_id),
            "offline_threading_id": _otid(),
            "recipient_igids": None, "replied_to_client_context": None,
            "replied_to_item_id": None, "reply_to_message_id": None, "sampled": None,
            "text": {"sensitive_string_value": texto},
            "mentions": [], "mentioned_user_ids": [], "commands": None,
            "forwarded_from_thread_id": None, "is_forwarded_from_own_message": None,
            "send_attribution": "igd_web_chat_tab:in_thread",
        }
        res = self.page.evaluate(JS_GRAPHQL, {
            **self._base(), "endpoint": "/api/graphql",
            "friendly": "IGDirectTextSendMutation", "doc_id": config.DOC_DM_SEND,
            "variables": json.dumps(variables, separators=(",", ":"))})
        checar_bloqueio(res["status"], res["text"])
        try:
            return _parse_json(res["text"])
        except Exception:
            st = res.get("status")
            corpo = (res.get("text") or "").strip()
            detalhe = f'o IG respondeu: "{corpo[:150]}"' if corpo else "o IG não retornou nada (corpo vazio)"
            raise BloqueioDetectado(f"envio travado — HTTP {st}: {explicar_status(st)}. {detalhe}")
