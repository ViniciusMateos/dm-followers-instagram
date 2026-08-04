"""
Modularização do dm-followers: PERFIS (modos) de tempo/limite.

Um PERFIL ("modo") agrupa os knobs ajustáveis (delays, caps, janela de horário).
Vem com 3 modos prontos: `padrao` (os valores de hoje), `agressivo`, `calmo`.
DM é o automatismo de MAIOR risco de ban — o modo `calmo` é o recomendado.

Não tem "chats" aqui (o bot trabalha na lista de seguidores). De qual @ começar é o
`--start-from`; sem isso, retoma do último run (histórico já salvo no state).

Persiste em `perfis.json`. Os DEFAULTS vivem no código (clone novo já funciona).
"""
import copy
import json
import os

_BASE = os.path.dirname(os.path.abspath(__file__))
PERFIS_FILE = os.path.join(_BASE, "perfis.json")

# ─── knobs de um perfil (= os valores de HOJE, do config.py) ───
PERFIL_PADRAO = {
    "aplicar_caps": True,
    "max_dms_dia": 0,            # 0 = sem cap
    "max_dms_hora": 0,
    "max_dms_por_run": 0,
    "delay_dm": [5, 20],         # entre uma pessoa e outra (segundos)
    "pausa_longa_cada": 0,       # 0 = sem pausa longa
    "pausa_longa": [120, 300],
    "delay_acao_ui": [1.5, 4.0],
    "active_hours": [9, 23],
    "humanizar": True,           # pausas navegando no meio (quebra o padrão de rajada)
    "pausa_cada": 8,             # a cada N DMs, dá uma pausa humana (0 = off)
}

_MODOS_BUILTIN = {
    "padrao": {},               # exatamente como você usa hoje
    "agressivo": {
        "aplicar_caps": False,
        "delay_dm": [3, 10],
        "pausa_longa_cada": 0,
    },
    "calmo": {                   # ⭐ recomendado p/ DM (menor risco de bloqueio)
        "aplicar_caps": True,
        "max_dms_por_run": 30,
        "delay_dm": [20, 60],
        "pausa_longa_cada": 10,
        "pausa_longa": [180, 420],
    },
}

_MAP_CONFIG = {
    "aplicar_caps": "APLICAR_CAPS", "max_dms_dia": "MAX_DMS_DIA",
    "max_dms_hora": "MAX_DMS_HORA", "max_dms_por_run": "MAX_DMS_POR_RUN",
    "delay_dm": "DELAY_DM", "pausa_longa_cada": "PAUSA_LONGA_CADA",
    "pausa_longa": "PAUSA_LONGA", "delay_acao_ui": "DELAY_ACAO_UI",
    "active_hours": "ACTIVE_HOURS",
    "humanizar": "HUMANIZAR", "pausa_cada": "PAUSA_CADA",
}


def _default_perfis():
    out = {}
    for nome, override in _MODOS_BUILTIN.items():
        p = copy.deepcopy(PERFIL_PADRAO)
        p.update(override)
        out[nome] = p
    return out


def carregar_perfis():
    if os.path.exists(PERFIS_FILE):
        try:
            with open(PERFIS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            for nome, p in list(d.items()):
                base = copy.deepcopy(PERFIL_PADRAO)
                base.update(p)
                d[nome] = base
            return d
        except Exception:
            pass
    d = _default_perfis()
    salvar_perfis(d)
    return d


def salvar_perfis(d):
    with open(PERFIS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def get_perfil(nome):
    return carregar_perfis().get(nome)


def salvar_perfil(nome, valores):
    perfis = carregar_perfis()
    base = copy.deepcopy(PERFIL_PADRAO)
    base.update(valores or {})
    perfis[nome] = base
    salvar_perfis(perfis)
    return base


def aplicar(config, perfil):
    """Sobrescreve os atributos do módulo `config` com os valores do perfil."""
    for campo, attr in _MAP_CONFIG.items():
        if campo in perfil:
            v = perfil[campo]
            if isinstance(v, list) and len(v) == 2:
                v = tuple(v)
            setattr(config, attr, v)
