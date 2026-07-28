"""
dm_followers — manda DM pros novos seguidores (aba de notificações).

Fluxo:
  1. lê a aba de notificações ("começou a seguir você"), do mais antigo pro mais novo
  2. na 1ª vez você escolhe de qual seguidor começar (--start-from @user)
  3. manda a mensagem (template com o nick da pessoa na 1ª linha + spintax)
  4. salva o último processado; no próximo run pega só os mais novos

Uso:
  python main.py --login                  # 1ª vez: login manual
  python main.py --dry-run --start-from juliatilco   # simula a partir de um user
  python main.py --start-from juliatilco  # 1ª vez pra valer (escolhe de quem começa)
  python main.py                          # próximos runs: só os novos desde o último
  python main.py --debug                  # dump do feed de atividades

Modular (modos de tempo):
  python main.py --listar-modos           # mostra os modos (padrao/agressivo/calmo)
  python main.py --modo calmo             # roda com tempos mais lentos (recomendado p/ DM)
  python main.py --modo agressivo --start-from fulano
"""
import argparse
import os
import re
import sys
import random
import time
import traceback
from datetime import datetime

import config
import perfis
from safety import State, Guard, log, BloqueioDetectado, LimiteAtingido
from ig import IG

LOGS_ERRO_DIR = os.path.join(config.OUTPUT_DIR, "logs")

_T_INICIO = time.monotonic()


def _dur_run():
    """Tempo total desta execução, formatado (ex: '3m 12s')."""
    s = int(time.monotonic() - _T_INICIO)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def progresso(done, total, label=""):
    """Marcador machine-readable pro backend/app desenharem a barra de progresso."""
    print(f"[progress] {done} {total} {label}".rstrip(), flush=True)


def _carregar_cookies(path):
    """Lê um JSON de cookies (ex: extensão Cookie-Editor) e converte pro formato Playwright."""
    import json
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    ss_map = {"no_restriction": "None", "unspecified": "Lax", "lax": "Lax",
              "strict": "Strict", "none": "None"}
    out = []
    for c in raw:
        ck = {"name": c["name"], "value": c["value"],
              "domain": c.get("domain") or ".instagram.com", "path": c.get("path", "/"),
              "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure", True)),
              "sameSite": ss_map.get(str(c.get("sameSite", "")).lower(), "Lax")}
        exp = c.get("expirationDate") or c.get("expires")
        if exp and not c.get("session"):
            ck["expires"] = int(float(exp))
        out.append(ck)
    return out


def modo_importar_cookies(path):
    cookies = _carregar_cookies(path)
    log.info("Importando %d cookies de %s…", len(cookies), path)
    with IG() as ig:
        ok = ig.importar_cookies(cookies)
    if ok:
        log.info("Sessão logada! Já pode rodar os bots.")
        return
    # Sair != 0 aqui é obrigatório: quem chama é o app ("Conectar Instagram"), e ele decide
    # pelo código de saída se avisa "conectado" ou "deu ruim". Saindo 0 numa falha, o app
    # anunciava sessão conectada com o login inválido.
    log.error("Importei os cookies mas a sessão NÃO está logada. Exporte de novo com a "
              "conta logada no instagram.com (precisa de um sessionid válido).")
    sys.exit(1)


def montar_mensagem(username):
    """Troca {username} e resolve spintax {a|b|c} (escolhe um aleatório)."""
    txt = config.MENSAGEM.replace("{username}", username)
    while re.search(r"\{[^{}]*\|[^{}]*\}", txt):
        txt = re.sub(r"\{([^{}]*\|[^{}]*)\}",
                     lambda m: random.choice(m.group(1).split("|")), txt, count=1)
    return txt


def imprimir_saldo(guard, motivo=""):
    extra = f" — {motivo}" if motivo else ""
    log.info("──────────────── SALDO DA EXECUÇÃO%s ────────────────", extra)
    log.info("   DMs enviadas .......... %d", guard.enviadas)
    log.info("   puladas (já enviou) ... %d", guard.puladas)
    log.info("   tempo de execução ..... %s", _dur_run())
    log.info("─────────────────────────────────────────────────────")
    # marcador machine-readable pro histórico (vai pro stdout E pro run.log)
    log.info("[saldo] enviadas=%d puladas=%d", guard.enviadas, guard.puladas)


def tratar_erro(exc, titulo):
    os.makedirs(LOGS_ERRO_DIR, exist_ok=True)
    caminho = os.path.join(LOGS_ERRO_DIR, "erro_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        caminho = "(não consegui salvar o arquivo de erro)"
    log.error("%s: %s", titulo, str(exc)[:160])
    log.error("   detalhes completos em: %s", caminho)


def modo_login():
    log.info("Abrindo navegador para login manual…")
    with IG() as ig:
        ig.ir("https://www.instagram.com/")
        input(">>> Loga na janela do Chrome e aperte ENTER aqui quando estiver no feed… ")
        log.info("Sessão detectada." if ig.logado() else "Não detectei sessionid — confira o login.")


def escolher_candidatos(novos, state, start_from, start_oldest):
    """Aplica a regra de retomada. novos vêm do mais antigo pro mais novo."""
    nao_enviados = [f for f in novos if not state.ja_enviou(f["pk"])]
    if start_from:
        idx = next((i for i, f in enumerate(novos) if f["username"].lower() == start_from.lower()), None)
        if idx is None:
            log.error("--start-from %s: não achei esse usuário no feed de notificações.", start_from)
            return []
        ts = novos[idx]["timestamp"]
        return [f for f in nao_enviados if f["timestamp"] >= ts]
    last = state.data.get("last_timestamp", 0)
    if last > 0:
        return [f for f in nao_enviados if f["timestamp"] > last]
    if start_oldest:
        return nao_enviados
    log.warning("Primeira vez: escolhe de quem começar com --start-from <username> "
                "(ou --start-from-oldest pra mandar pra todos os visíveis). Não vou agir sozinho.")
    return []


def run(dry=False, start_from=None, start_oldest=False, debug=False, ignorar_janela=False):
    # O histórico é POR CONTA: cada conta tem a SUA lista de quem já recebeu DM. Sem isso,
    # trocar de conta faz o bot herdar o histórico da anterior e achar que já falou com
    # todo mundo (aconteceu: conta nova mandou só 2 DMs).
    conta = config.conta_da_sessao()
    state = State(config.state_file(conta))
    guard = Guard(state, dry_run=dry)
    try:
        guard.checar_janela(ignorar=ignorar_janela)
    except LimiteAtingido as e:
        log.info("Não vou rodar agora: %s", e)
        return

    log.info("Abrindo Instagram (%s)…", "DRY-RUN" if dry else "AÇÃO REAL")
    with IG(dry_run=dry) as ig:
        # nav de setup RESILIENTE: rodando 2 bots no MESMO túnel, a home (pesada) às vezes
        # não carrega em 30s por contenção. Tenta 3x com folga (45s) em vez de "erro fatal" na
        # primeira. Depois disso a DM é só fetch (não recarrega página), então basta abrir 1x.
        aberto = False
        for _tent in range(3):
            try:
                ig.ir("https://www.instagram.com/", timeout=45000)
                aberto = True
                break
            except Exception as e:
                log.warning("~ a home do IG demorou a abrir (%d/3): %s — repito",
                            _tent + 1, str(e).splitlines()[0][:50])
        if not aberto:
            log.error("Não consegui abrir o Instagram (túnel congestionado?). Tenta de novo em "
                      "instantes, ou roda um bot por vez.")
            return
        if not ig.logado():
            log.error("Sem sessão logada. Rode `python main.py --login` primeiro.")
            return
        # regrava a sessão a cada run: mantém o cookie fresco E garante que a PRÓXIMA run
        # saiba qual conta é esta (é do arquivo de sessão que sai o ds_user_id)
        ig.salvar_sessao()
        # ds_user_id da conta REALMENTE logada NESTE browser (dos cookies do contexto) — NÃO do
        # arquivo de sessão central, que virou RACY com 2 bots escrevendo junto e devolveu
        # "conta desconhecida" → state-desconhecida.json → REMANDOU DM pra todo mundo. Fonte da
        # verdade = o cookie deste browser. Se o arquivo deu outra coisa, reaponta o state AGORA.
        conta_live = (ig._cookies() or {}).get("ds_user_id")
        if conta_live and conta_live != conta:
            conta = conta_live
            state = State(config.state_file(conta))
            guard.state = state
        # QUAL conta está rodando — sempre, em toda run. Rodar com a conta errada sem
        # perceber já custou caro (histórico de uma conta aplicado em outra).
        log.info("Conta: @%s (%s) | histórico: %s", ig.usuario() or "?",
                 conta or "id não identificado", os.path.basename(state.path))
        ig.carregar_tokens()

        try:
            novos = ig.novos_seguidores()
            log.info("%d novos seguidores no feed de notificações.", len(novos))
            if debug:
                os.makedirs(config.OUTPUT_DIR, exist_ok=True)
                import json
                with open(os.path.join(config.OUTPUT_DIR, "debug_seguidores.json"), "w", encoding="utf-8") as f:
                    json.dump(novos, f, ensure_ascii=False, indent=1)
                log.info("debug: feed salvo em output/debug_seguidores.json")

            # Conta SEM histórico = conta nova: varre TODOS os seguidores visíveis e faz o
            # fluxo completo. Nada foi enviado por ESTA conta, então não há de onde "retomar".
            # (O COMECAR_DE do config só vale se você mandar explicitamente — era um marco de
            # quando o bot rodava numa conta só, e numa conta nova ele trava tudo: o usuário
            # dele não está no feed, o bot não acha e não faz nada.)
            primeira_vez = state.data.get("last_timestamp", 0) == 0
            if not start_from and not start_oldest and primeira_vez:
                start_oldest = True
                log.info("Conta sem histórico — varrendo todos os %d seguidores do feed.",
                         len(novos))

            candidatos = escolher_candidatos(novos, state, start_from, start_oldest)
            # MAX_DMS_POR_RUN = 0 (ou caps off) → manda pra todos os novos
            limite = (config.MAX_DMS_POR_RUN if config.APLICAR_CAPS else 0) or len(candidatos)
            candidatos = candidatos[:limite]
            if not candidatos:
                log.info("Ainda não tem novos seguidores pra mandar DM. 👋")
                return
            log.info("Vão receber DM (%d): %s", len(candidatos),
                     ", ".join("@" + c["username"] for c in candidatos[:10]) +
                     (" …" if len(candidatos) > 10 else ""))

            total = len(candidatos)
            progresso(0, total, "iniciando")
            falhas_thread = 0   # criar_thread falhando EM SÉRIE = soft-block de DM → para de martelar
            for i, c in enumerate(candidatos):
                progresso(i, total, f"@{c['username']}")
                guard.pode_enviar()
                texto = montar_mensagem(c["username"])
                # SEM goto de página por pessoa. Visitar o perfil e "abrir a conversa" eram só
                # dwell humano — e é JUSTO o que trava no proxy: um goto pesado que engasga deixa
                # o Chromium preso e o page.evaluate seguinte pendura pra sempre. A DM vai por
                # FETCH (criar_thread + enviar_dm), que não precisa carregar página nenhuma (o
                # browser já está numa página logada do IG). Mantém só as PAUSAS pra ritmo humano.
                guard.dormir(config.DELAY_ACAO_UI, "preparando")
                thread = ig.criar_thread(c["pk"])
                if not thread:
                    falhas_thread += 1
                    log.warning("! não consegui abrir thread com @%s — pulando (%d seguidas)",
                                c["username"], falhas_thread)
                    if falhas_thread >= 6:
                        raise BloqueioDetectado(
                            "6 threads falharam seguidas ao criar — provável soft-block de DM na "
                            "conta. Parando pra não piorar; deixe a conta descansar uns dias.")
                    continue
                falhas_thread = 0   # criou a thread → zera o contador
                # VERIFICAÇÃO DUPLA: olha DENTRO da conversa. Se a NOSSA DM já está lá, NÃO
                # remanda — blindagem contra state zerado/errado (foi o que remandou pra todos).
                if ig.ja_mandou_msg(thread, config.MARCA_TEMPLATE):
                    log.info("• @%s já tem a nossa DM na conversa — pulando (verificação dupla)",
                             c["username"])
                    state.marcar_enviado(c["pk"], c["timestamp"])   # marca pra não rechecar
                    guard.puladas = getattr(guard, "puladas", 0) + 1
                    continue
                guard.dormir(config.DELAY_ACAO_UI, "abrindo conversa")
                if dry:
                    log.info("│ [dry] DM → @%s (pk %s)", c["username"], c["pk"])
                    log.info("│       %s", texto.replace("\n", " ⏎ ")[:120])
                    guard.enviadas += 1; guard.pos_dm_dry()
                    continue
                ig.enviar_dm(thread, texto)        # levanta BloqueioDetectado se falhar
                state.marcar_enviado(c["pk"], c["timestamp"])
                guard.enviadas += 1
                log.info("✓ DM enviada → @%s", c["username"])
                guard.pos_dm()
            progresso(total, total, "concluído")
        except LimiteAtingido as e:
            log.info("Parando (cap atingido): %s", e)
        except BloqueioDetectado as e:
            tratar_erro(e, "BLOQUEIO do Instagram — parando o run")
        except KeyboardInterrupt:
            log.info("Interrompido manualmente (Ctrl+C).")
        except Exception as e:
            tratar_erro(e, "erro inesperado — parando o run")
        finally:
            imprimir_saldo(guard, "simulado" if dry else "")


def main():
    ap = argparse.ArgumentParser(description="dm_followers")
    ap.add_argument("--login", action="store_true", help="login manual (1ª vez)")
    ap.add_argument("--import-cookies", metavar="FILE", help="importa cookies (JSON do Cookie-Editor) e pula o login")
    ap.add_argument("--dry-run", action="store_true", help="simula sem enviar")
    ap.add_argument("--start-from", metavar="USER", help="1ª vez: começar a partir desse seguidor")
    ap.add_argument("--start-from-oldest", action="store_true", help="manda pra todos os visíveis")
    ap.add_argument("--debug", action="store_true", help="dump do feed de atividades")
    ap.add_argument("--ignore-window", action="store_true", help="ignora janela de horário")
    # ── modularização (modos de tempo) ──
    ap.add_argument("--modo", metavar="NOME", default="padrao", help="modo: padrao, agressivo, calmo…")
    ap.add_argument("--listar-modos", action="store_true", help="lista os modos salvos e sai")
    a = ap.parse_args()

    if a.listar_modos:
        for nome, p in perfis.carregar_perfis().items():
            log.info("modo: %-12s caps=%s | dms/run=%s | delay_dm=%s | pausa_cada=%s",
                     nome, p["aplicar_caps"], p["max_dms_por_run"],
                     p["delay_dm"], p["pausa_longa_cada"])
        return
    if a.import_cookies:
        modo_importar_cookies(a.import_cookies)
        return
    if a.login:
        modo_login()
        return

    # aplica o MODO escolhido no config antes de rodar
    perfil = perfis.get_perfil(a.modo)
    if not perfil:
        log.error("Modo '%s' não existe. Use --listar-modos.", a.modo)
        sys.exit(2)
    perfis.aplicar(config, perfil)
    log.info("Modo: %s  |  delay_dm: %s  |  caps: %s", a.modo,
             config.DELAY_DM, config.APLICAR_CAPS)

    try:
        run(dry=a.dry_run, start_from=a.start_from, start_oldest=a.start_from_oldest,
            debug=a.debug, ignorar_janela=a.ignore_window)
    except KeyboardInterrupt:
        log.info("Interrompido.")
    except Exception as e:
        tratar_erro(e, "erro fatal")
        sys.exit(2)


if __name__ == "__main__":
    main()
