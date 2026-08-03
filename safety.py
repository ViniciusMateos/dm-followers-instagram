"""
Camada de segurança do dm_followers: estado/retomada, caps, delays humanos,
janela de horário e kill-switch de bloqueio. Adaptado do like-bot.
"""
import json
import os
import sys
import time
import random
import logging
from datetime import datetime

import config


# ───────────────────────────── log ─────────────────────────────
def setup_logger():
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logger = logging.getLogger("dmfollowers")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    # LOG POR SESSÃO: cada run grava seu PRÓPRIO arquivo em output/logs/run_<timestamp>.log,
    # pra você abrir DEPOIS e ver exatamente o que rolou naquele run (o run.log acumula TODOS
    # os runs e vira uma sopa ilegível). Mantém só os 30 mais recentes pra não encher o disco.
    _logs_dir = os.path.join(config.OUTPUT_DIR, "logs")
    os.makedirs(_logs_dir, exist_ok=True)
    _sess = logging.FileHandler(
        os.path.join(_logs_dir, "run_" + time.strftime("%Y%m%d_%H%M%S") + ".log"), encoding="utf-8")
    _sess.setFormatter(fmt)
    try:
        _antigos = sorted(f for f in os.listdir(_logs_dir)
                          if f.startswith("run_") and f.endswith(".log"))
        for _f in _antigos[:-30]:
            os.remove(os.path.join(_logs_dir, _f))
    except Exception:
        pass
    logger.addHandler(fh)
    logger.addHandler(_sess)
    logger.addHandler(ch)
    return logger


log = setup_logger()


def fmt_tempo(segundos):
    seg = int(round(segundos))
    if seg < 60:
        return f"{seg}s"
    m, s = divmod(seg, 60)
    return f"{m}m {s}s" if s else f"{m}m"


def _dormir_contando(t, motivo=""):
    """Dorme `t` segundos. Em pausas longas (>15s) emite [espera] a cada ~12s com o tempo
    restante → app/Live Activity mostram a contagem regressiva ('faltam Xm Ys')."""
    if t <= 15:
        time.sleep(t)
        return
    fim = time.monotonic() + t
    while True:
        restam = fim - time.monotonic()
        if restam <= 0.5:
            break
        print(f"[espera] {int(round(restam))} {motivo}".rstrip(), flush=True)
        time.sleep(min(restam, 12))


# ────────────────────────── exceções ───────────────────────────
class BloqueioDetectado(Exception):
    """Instagram sinalizou ação bloqueada / checkpoint / spam."""


class LimiteAtingido(Exception):
    """Bateu um cap — parar com elegância."""


# ─────────────────── detecção de bloqueio ───────────────────────
MENSAGENS_BLOQUEIO = {
    "feedback_required", "checkpoint_required", "challenge_required",
    "login_required", "consent_required", "rate_limit_error",
}

_STATUS = {
    200: "OK", 400: "requisição recusada (validação ou ação bloqueada)",
    401: "não autenticado — a sessão expirou/caiu (reimporte os cookies)",
    403: "proibido — sessão inválida ou ação barrada",
    429: "RATE LIMIT — ações demais num intervalo curto; o IG está te limitando",
    500: "erro interno do servidor do IG (transitório)",
    502: "bad gateway no IG (transitório)",
    503: "serviço indisponível / sobrecarga no IG (transitório)",
    504: "timeout no servidor do IG (transitório)",
}


def explicar_status(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "status desconhecido"
    if code in _STATUS:
        return _STATUS[code]
    if 560 <= code <= 599:
        return ("código não-padrão do Meta (throttle/sobrecarga) — quase sempre é o IG te "
                "SEGURANDO por excesso de ações (rate limit), não erro de dados")
    if 500 <= code < 600:
        return "erro no servidor do IG (5xx, geralmente transitório)"
    if 400 <= code < 500:
        return "requisição recusada pelo IG (4xx)"
    return "status inesperado"


def checar_bloqueio(status_code, texto):
    """Levanta BloqueioDetectado SÓ com sinal estruturado real (sem substring solta)."""
    texto = texto or ""
    if status_code == 429:
        raise BloqueioDetectado(f"HTTP 429 — {explicar_status(429)}.")
    body = texto[len("for (;;);"):] if texto.startswith("for (;;);") else texto
    try:
        j = json.loads(body)
    except Exception:
        j = None
    if isinstance(j, dict):
        msg = str(j.get("message", "")).lower()
        status = str(j.get("status", "")).lower()
        fb = j.get("feedback_message") or j.get("feedback_title") or ""
        if msg in MENSAGENS_BLOQUEIO:
            raise BloqueioDetectado(f'ação bloqueada (message="{msg}").' + (f' O IG disse: "{fb}"' if fb else ""))
        if j.get("spam") is True:
            raise BloqueioDetectado(f"ação marcada como SPAM pelo IG." + (f' O IG disse: "{fb}"' if fb else ""))
        if j.get("checkpoint_url") or j.get("challenge"):
            raise BloqueioDetectado("checkpoint/desafio — o IG quer que você confirme que é você no app.")
        if status == "fail" and any(k in msg for k in ("feedback", "checkpoint", "challenge", "blocked")):
            raise BloqueioDetectado(f'ação falhou (message="{msg}").' + (f' O IG disse: "{fb}"' if fb else ""))
        return
    if body.lstrip()[:1] in ("<",) and status_code in (200, 302, 400):
        raise BloqueioDetectado(f"HTTP {status_code} — o IG devolveu uma página HTML "
                                f"(sessão caiu ou checkpoint), não dados.")


# ───────────────────────── estado ──────────────────────────────
class State:
    def __init__(self, path=config.STATE_FILE):
        self.path = path
        self.data = {
            "dmed_pks": [],            # quem já recebeu DM
            "last_timestamp": 0,       # timestamp do último seguidor processado
            "dm_events": [],           # epochs dos envios (caps rolantes)
        }
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception as e:
                log.warning("Não consegui ler state.json (%s); começando limpo.", e)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)

    def ja_enviou(self, pk):
        return str(pk) in self.data["dmed_pks"]

    def marcar_enviado(self, pk, timestamp):
        pk = str(pk)
        if pk not in self.data["dmed_pks"]:
            self.data["dmed_pks"].append(pk)
        if timestamp and timestamp > self.data.get("last_timestamp", 0):
            self.data["last_timestamp"] = timestamp
        self.data["dm_events"].append(int(time.time()))
        self.save()

    # caps rolantes
    def _prune(self):
        agora = int(time.time())
        self.data["dm_events"] = [t for t in self.data["dm_events"] if agora - t < 24 * 3600]

    def dms_ultima_hora(self):
        self._prune(); agora = int(time.time())
        return sum(1 for t in self.data["dm_events"] if agora - t < 3600)

    def dms_ultimo_dia(self):
        self._prune(); return len(self.data["dm_events"])


# ───────────────────── limites / delays ────────────────────────
class Guard:
    def __init__(self, state: State, dry_run=False):
        self.state = state
        self.dry_run = dry_run
        self._n = 0
        self._dry_extra = 0
        self.enviadas = 0
        self.puladas = 0

    def checar_janela(self, ignorar=False):
        if ignorar or not config.APLICAR_CAPS or not getattr(config, "USAR_JANELA", False):
            return
        h = datetime.now().hour
        ini, fim = config.ACTIVE_HOURS
        if not (ini <= h < fim):
            raise LimiteAtingido(f"Fora da janela ({ini}h–{fim}h). Agora: {h}h.")

    def pode_enviar(self):
        if not config.APLICAR_CAPS:
            return
        if config.MAX_DMS_DIA and self.state.dms_ultimo_dia() + self._dry_extra >= config.MAX_DMS_DIA:
            raise LimiteAtingido(f"Cap diário atingido ({config.MAX_DMS_DIA}).")
        if config.MAX_DMS_HORA and self.state.dms_ultima_hora() + self._dry_extra >= config.MAX_DMS_HORA:
            raise LimiteAtingido(f"Cap horário atingido ({config.MAX_DMS_HORA}).")

    def pos_dm(self):
        self._n += 1
        if config.PAUSA_LONGA_CADA and self._n % config.PAUSA_LONGA_CADA == 0:
            self.dormir(config.PAUSA_LONGA, "pausa longa")
        else:
            self.dormir(config.DELAY_DM, "entre perfis")

    def pos_dm_dry(self):
        self._dry_extra += 1

    def dormir(self, faixa, motivo=""):
        a, b = faixa
        t = random.uniform(a, b)
        if self.dry_run:
            log.info("[dry-run] dormiria %s (%s)", fmt_tempo(t), motivo)
            return
        log.info("dormindo %s (%s)", fmt_tempo(t), motivo)
        _dormir_contando(t, motivo)
