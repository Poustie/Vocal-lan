"""Fumée de l'interface : construit l'UI avec FakeEngine, déclenche des actions, ferme sans erreur.

Aucune carte son, aucun réseau, aucun fichier de réglages réel : les Settings restent en mémoire
(save() ne touche pas le disque dans le stub, et on n'appelle jamais Settings.load()).
"""
import gc
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Jamais dans le vrai %APPDATA%\Vocal LAN : dossier de données temporaire.
os.environ["VOCAL_LAN_DATA"] = tempfile.mkdtemp(prefix="vocal-lan-ui-test-")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fake_engine  # noqa: E402
from fake_engine import FakeEngine, FakeHotkeys  # noqa: E402

ui = pytest.importorskip("ui")


def make_settings(first_run_done=True, name="Seb"):
    s = fake_engine.Settings()
    s.name = name
    s.ui.first_run_done = first_run_done
    return s


@pytest.fixture
def make_app():
    apps = []

    def factory(scenario="full", first_run_done=True, name="Seb", **kw):
        s = make_settings(first_run_done, name)
        eng = FakeEngine(s, scenario=scenario, **kw)
        eng.start(online=first_run_done)
        hk = FakeHotkeys(delay=0)
        app = None
        for attempt in range(4):
            try:
                app = ui.App(eng, s, hk, use_tray=False)
                break
            except Exception as exc:  # noqa: BLE001
                if "display" in str(exc).lower():
                    pytest.skip("pas d'affichage disponible")
                # Python du Windows Store : la 2e initialisation de Tcl échoue parfois juste
                # après la destruction d'une racine précédente. On réessaie un peu plus tard.
                if attempt == 3 or "TclError" not in type(exc).__name__:
                    raise
                gc.collect()
                time.sleep(0.6)
        apps.append(app)
        return app, eng, s, hk

    yield factory
    for a in apps:
        a.quit_app()


def pump(app, seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def test_main_window_renders_peers(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    assert len(app.rows) == 4
    assert app.title() == "Vocal LAN (5)"
    assert "5 personnes" in app.status_lbl.cget("text")
    # mises à jour différentielles : les widgets ne sont pas recréés d'un tick à l'autre
    before = {k: id(v) for k, v in app.rows.items()}
    pump(app)
    assert before == {k: id(v) for k, v in app.rows.items()}


def test_actions_mic_deaf_copy_ctrl_m(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    app.toggle_mic()
    app.toggle_deaf()
    pump(app)
    assert ("set_muted", True) in eng.calls and ("set_deafened", True) in eng.calls
    assert app.mic_btn.title_lbl.cget("text") == "Micro coupé"
    assert app.deaf_btn.title_lbl.cget("text") == "Casque coupé"
    app.event_generate("<Control-m>")
    pump(app)
    assert ("set_muted", False) in eng.calls
    app.copy_ip()
    assert app.clipboard_get() == eng.local_ip
    assert "mute" in eng.cues


def test_peer_row_controls(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    row = next(iter(app.rows.values()))
    row._toggle_mute()
    row._volume_changed(1.6)
    assert s.peer_prefs[row.peer_id]["muted"] is True
    assert s.peer_prefs[row.peer_id]["volume"] == pytest.approx(1.6)
    pump(app)
    assert row.mute_btn.cget("text") == "Rétablir"


def test_join_leave_events_cues_and_toast(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    assert app._empty_shown is True
    pid = eng.add_fake_peer("Zoé")
    pump(app)
    assert "join" in eng.cues and pid in app.rows
    assert app._empty_shown is False
    assert "Zoé" in app.toast_lbl.cget("text")
    eng.remove_fake_peer(pid)
    pump(app)
    assert "leave" in eng.cues and pid not in app.rows
    # sons désactivés : plus de cue
    s.ui.sounds = False
    n = len(eng.cues)
    eng.add_fake_peer("Max")
    pump(app)
    assert len(eng.cues) == n


def test_empty_state_firewall_hint_after_20s(make_app):
    app, eng, s, hk = make_app("alone", alone_offset=25)
    pump(app)
    assert app._alone_hint_shown is True
    app2_status = app.status_lbl.cget("text")
    assert "Seul" in app2_status


def test_banners_for_audio_and_settings_events(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    eng.break_audio(0.3)
    pump(app, 0.2)
    assert app.banners.visible("audio_ev") or app.banners.visible("audio_state")
    pump(app, 0.8)
    assert not app.banners.visible("audio_ev")
    eng.push_event("settings_warning", "Réglages réinitialisés.")
    pump(app)
    assert app.banners.visible("settings")


def test_settings_window_and_key_capture(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    app.open_settings()
    pump(app, 0.5)
    win = app.settings_win
    assert win is not None and win.winfo_exists()
    # pseudo validé à Entrée
    win.name_entry.delete(0, "end")
    win.name_entry.insert(0, "Sébastien")
    win._commit_name(True)
    assert s.name == "Sébastien"
    # capture de touche (FakeHotkeys répond tout de suite)
    win.key_pickers[0].btn.invoke()
    pump(app)
    assert s.ptt_key == "f9" and hk.apply_count >= 1
    # mode PTT, préférences de fenêtre (sans toucher au démarrage Windows)
    win.ptt_var.set(1)
    win._ptt_changed()
    assert s.ptt_mode is True
    win.switches["always_on_top"].set(True)
    win._switch("always_on_top", win.switches["always_on_top"])
    pump(app)
    assert s.ui.always_on_top is True
    win.gate.slider.set(-40)
    win.gate._moved(-40)
    assert s.gate_db == -40
    win.mode_seg.set("Stable")
    win._mode_changed("Stable")
    assert s.buffer_mode == "stable"
    win.devices.refresh()
    win.close()
    assert app.settings_win is None


def test_add_ip_errors_are_readable(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    dlg = ui.AddIpDialog(app)
    pump(app, 0.2)
    row = next(w for w in dlg.winfo_children() if isinstance(w, ui.AddIpRow))
    row.entry.insert(0, "pas une ip")
    row.submit()
    pump(app, 0.4)                           # add_peer_ip tourne hors du thread Tk
    assert "adresse" in row.msg.cget("text").lower()
    row.entry.delete(0, "end")
    row.entry.insert(0, "http://192.168.1.77/")      # préfixe collé depuis un navigateur
    row.submit()
    pump(app, 0.4)
    assert "192.168.1.77" in s.manual_peers
    dlg.destroy()


def test_help_and_firewall_flow(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    app.open_help()
    pump(app, 0.3)
    called = []
    app._do_firewall()                      # simulé : FakeEngine ne touche jamais au vrai pare-feu
    assert ("repair_firewall",) in eng.calls
    pump(app, 1.8)
    assert "autorisé" in app.help_win.fw_status.cget("text").lower()


def test_wizard_flow_goes_online(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    wiz = app.wizard
    assert wiz is not None and eng._online is False
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()                       # -> étape 2
    pump(app, 0.2)
    wiz._toggle_monitor()
    assert ("set_monitor", True, 15) in eng.calls
    wiz.next()                       # -> étape 3
    wiz.mode_var.set(1)
    wiz._mode_changed()
    assert wiz.next_btn.cget("state") == "disabled"          # PTT sans touche
    wiz.key_picker.btn.invoke()
    pump(app, 0.3)
    assert s.ptt_key == "f9"
    wiz.next()                       # Terminer
    pump(app, 0.2)
    assert app.wizard is None
    assert s.name == "Léo" and s.ptt_mode is True
    assert s.ui.first_run_done is True and eng._online is True


def test_geometry_saved_and_closing_contract(make_app):
    app, eng, s, hk = make_app("full")
    hk.start()
    pump(app, 0.3)
    app.geometry("600x700+120+90")
    pump(app, 1.0)
    assert s.ui.geometry and s.ui.geometry.startswith("600x700")
    app.quit_app()
    app.quit_app()                   # idempotent
    assert ("shutdown",) in eng.calls and hk.started is False
    assert s.dirty is False          # settings.save() appelé à la fermeture


# ---------------------------------------------------------------------------
# Revue croisée (phase 4)
# ---------------------------------------------------------------------------
def test_mute_cues_follow_state_changes_not_clicks(make_app):
    """Les raccourcis globaux changent l'état dans le moteur sans passer par l'UI : le son suit quand même."""
    app, eng, s, hk = make_app("alone")
    pump(app)
    eng.cues.clear()
    eng.set_muted(True)                      # comme le ferait hotkeys.py
    pump(app, 0.2)
    assert eng.cues == ["mute"]
    eng.set_muted(False)
    pump(app, 0.2)
    assert eng.cues == ["mute", "unmute"]
    s.ui.sounds = False
    eng.set_muted(True)
    pump(app, 0.2)
    assert eng.cues == ["mute", "unmute"]    # sons désactivés : rien


def test_deafen_also_mutes_mic_and_restores(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    eng.cues.clear()
    eng.set_deafened(True)                   # raccourci global
    pump(app, 0.3)
    assert eng.muted is True and eng.deafened is True
    assert eng.cues == ["mute"]              # un seul son, pas deux
    eng.set_deafened(False)
    pump(app, 0.3)
    assert eng.muted is False and eng.deafened is False
    assert eng.cues == ["mute", "unmute"]
    # micro déjà coupé avant la sourdine : il le reste après
    eng.set_muted(True)
    pump(app, 0.2)
    eng.set_deafened(True)
    pump(app, 0.2)
    eng.set_deafened(False)
    pump(app, 0.3)
    assert eng.muted is True and eng.deafened is False
    # rétablir le micro pendant la sourdine rétablit aussi le son
    eng.set_muted(False)
    pump(app, 0.2)
    eng.set_deafened(True)
    pump(app, 0.2)
    app.toggle_mic()
    pump(app, 0.3)
    assert eng.muted is False and eng.deafened is False


def test_set_devices_does_not_freeze_ui_and_reverts_on_failure(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    dev = app.settings_win.devices
    eng.device_delay = 0.5
    menu = dev.menus["in"]
    menu.set("Micro casque (USB Gaming Headset)")
    t0 = time.monotonic()
    dev._changed()
    assert time.monotonic() - t0 < 0.15                 # l'appel ne bloque pas
    assert dev.busy is True and "Changement" in dev.status.cget("text")
    pump(app, 0.9)
    assert dev.busy is False
    assert s.dev_in == "Micro casque (USB Gaming Headset)"
    # un périphérique qui ne s'ouvre pas : retour à l'ancien et message clair
    eng.fail_devices = {"Webcam HD (Micro intégré)"}
    menu.set("Webcam HD (Micro intégré)")
    dev._changed()
    pump(app, 1.6)
    assert dev.busy is False
    assert s.dev_in == "Micro casque (USB Gaming Headset)"
    assert app.banners.visible("audio_pick")
    txt = app.banners._banners["audio_pick"].label.cget("text")
    assert "remis" in txt and "Choisis un autre micro" in txt and "Actualiser" in txt


def test_refresh_devices_waits_for_event(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    dev = app.settings_win.devices
    assert len(dev._maps["in"]) == 4                    # défaut + 3
    dev.refresh()
    assert dev.refreshing is True and dev.refresh_btn.cget("state") == "disabled"
    pump(app, 0.9)
    assert dev.refreshing is False and dev.refresh_btn.cget("state") == "normal"
    assert len(dev._maps["in"]) == 5                    # le casque Bluetooth est apparu


def test_eight_peers_long_and_accented_names(make_app):
    app, eng, s, hk = make_app("alone", name="Éloïse-Marguerite-Anne-Sophie-Dupont")
    pump(app)
    names = ["Zoé", "Ångström", "Jean-Christophe-Alexandre-Maximilien", "WWWWWWWWWWWWWWWWWWWWWWWW",
             "Seb", "Seb", "李小龙", "Ünal"]
    for n in names:
        eng.add_fake_peer(n)
    app.geometry("480x560")
    t0 = time.monotonic()
    pump(app, 0.8)
    assert len(app.rows) == 8
    assert app.title() == "Vocal LAN (9)"
    # arrivées simultanées : un seul son et une seule notification groupée
    assert eng.cues.count("join") == 1
    assert "autres" in app.toast_lbl.cget("text")
    # les deux « Seb » sont distingués
    labels = [r.name_lbl.cget("text") for r in app.rows.values()]
    assert sum(1 for t in labels if t.startswith("Seb (.")) == 2
    # chaque ligne garde ses boutons visibles dans la fenêtre minimale
    app.update_idletasks()
    for r in app.rows.values():
        assert r.mute_btn.winfo_rootx() + r.mute_btn.winfo_width() <= app.winfo_rootx() + app.winfo_width() + 2
    assert time.monotonic() - t0 < 5


def test_dialog_titles_do_not_start_with_app_title(make_app):
    """L'instance unique retrouve la fenêtre principale par préfixe « Vocal LAN » : aucune fenêtre
    secondaire ne doit commencer par ce titre."""
    app, eng, s, hk = make_app("alone")
    pump(app)
    app.open_settings(); app.open_help(); app.open_add_ip(); app.ask_firewall()
    pump(app, 0.4)
    titles = [w.title() for w in _walk(app) if isinstance(w, ctk_toplevel())]   # le pare-feu dépend de l'aide
    assert len(titles) >= 4
    assert not [t for t in titles if t.startswith("Vocal LAN")]


def ctk_toplevel():
    import customtkinter
    return customtkinter.CTkToplevel


def test_key_capture_cancelled_on_settings_close_and_duplicates_refused(make_app):
    app, eng, s, hk = make_app("alone")
    hk.delay = 5                                         # la touche n'arrive jamais pendant le test
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    win = app.settings_win
    win.key_pickers[1].btn.invoke()
    assert hk.capturing is True
    win.close()
    assert hk.capturing is False and getattr(hk, "cancelled", 0) == 1
    # une touche déjà utilisée ailleurs est refusée
    s.ptt_key = "f9"
    app.open_settings()
    pump(app, 0.4)
    kp = app.settings_win.key_pickers[1]                 # « couper le micro »
    kp._got("f9")
    assert s.mute_key is None
    assert "déjà" in app.toast_lbl.cget("text")
    kp._got("a")                                         # lettre seule : acceptée mais avertie
    assert s.mute_key == "a" and "écris" in app.toast_lbl.cget("text")


# ---------------------------------------------------------------------------
# Phase 4b
# ---------------------------------------------------------------------------
def test_add_ip_does_not_block_tk_and_shows_searching(make_app):
    app, eng, s, hk = make_app("alone", )
    pump(app)
    eng.add_ip_delay = 0.8
    dlg = ui.AddIpDialog(app)
    pump(app, 0.2)
    row = next(w for w in dlg.winfo_children() if isinstance(w, ui.AddIpRow))
    row.entry.insert(0, "192.168.1.80")
    t0 = time.time()
    row.submit()
    assert time.time() - t0 < 0.3                    # rend la main tout de suite
    assert "echerche" in row.msg.cget("text")
    pump(app, 1.3)
    assert "192.168.1.80" in s.manual_peers
    assert "Ajouté" in row.msg.cget("text")
    dlg.destroy()


def test_show_window_event_and_single_add_ip_dialog(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    app.withdraw()
    pump(app, 0.2)
    assert app.state() == "withdrawn"
    eng.push_event("show_window")
    pump(app, 0.5)
    assert app.state() == "normal"
    app.open_add_ip()
    first = app.add_ip_win
    app.open_add_ip()
    assert app.add_ip_win is first
    pump(app, 0.5)                           # laisse passer les after() de customtkinter
    first.destroy()
    pump(app, 0.3)


def test_render_isolates_steps_and_still_saves(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)

    def boom(*_a):
        raise RuntimeError("boum")
    app._render_list = boom
    calls = []
    s.save_if_dirty = lambda: calls.append(1)
    app._last_save = 0.0
    pump(app, 0.4)
    assert calls                             # save_if_dirty a bien tourné malgré l'étape en échec


def test_wizard_shows_device_error_inside_wizard(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    wiz = app.wizard
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()
    pump(app, 0.4)
    wiz.picker._reverted("Cet appareil ne marche pas.", None, None)   # chemin réel d'un échec
    assert wiz.err.winfo_manager() and "ne marche pas" in wiz.err.label.cget("text")
    wiz.picker._error(None)
    assert not wiz.err.winfo_manager()


def test_wizard_go_online_port_used_is_reported(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    eng.go_online_error = fake_engine.EngineError("PORT_USED", "Le port est déjà pris.")
    wiz = app.wizard
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()
    wiz.next()
    wiz.finish()
    pump(app, 0.3)
    assert app.wizard is None                              # pas bloqué dans l'assistant
    assert app.banners.visible("net_open")
    eng.go_online_error = None
    assert app.go_online() is True
    assert not app.banners.visible("net_open") and eng._online


def test_key_picker_destroyed_during_capture_is_harmless(make_app):
    app, eng, s, hk = make_app("alone")
    hk.delay = 5
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    kp = next(w for w in _walk(app.settings_win) if isinstance(w, ui.KeyPicker))
    kp._click()
    assert app._capture_cb is not None
    app.settings_win.close()
    pump(app, 0.3)
    assert app._capture_cb is None
    kp._got("f9")                                          # widget détruit : ne lève pas


def _walk(w):
    for c in w.winfo_children():
        yield c
        yield from _walk(c)


def test_deaf_rule_lives_in_engine_not_in_ui(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    eng.calls.clear()
    app.toggle_deaf()
    pump(app, 0.3)
    # l'UI n'appelle que set_deafened : aucun set_muted de sa part
    assert [c for c in eng.calls if c[0] in ("set_muted", "set_deafened")] == [("set_deafened", True)]
    assert eng.muted and eng.deafened


# ---------------------------------------------------------------------------
# Dernière passe de polissage (v5)
# ---------------------------------------------------------------------------
def _texts(widget):
    """Tous les textes affichés sous un widget (labels, boutons, interrupteurs)."""
    out = []
    try:
        t = widget.cget("text")
        if isinstance(t, str) and t:
            out.append(t)
    except Exception:  # noqa: BLE001
        pass
    for w in widget.winfo_children():
        out.extend(_texts(w))
    return out


def test_peer_row_shows_hears_me_positively(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    marie, lea = app.rows["m1"], app.rows["l3"]          # Léa ne nous reçoit pas
    assert marie.sub4_lbl.cget("text").strip(" ·") == "T'entend" and marie.sub3_lbl.cget("text") == ""
    assert lea.sub3_lbl.cget("text") == "Ne te reçoit pas" and "T'entend" not in lea.sub4_lbl.cget("text")
    eng.set_peer_hears("l3", True)
    eng.set_peer_hears("m1", False)
    pump(app)
    assert lea.sub4_lbl.cget("text").strip(" ·") == "T'entend" and lea.sub3_lbl.cget("text") == ""
    assert marie.sub3_lbl.cget("text") == "Ne te reçoit pas"


def test_banner_when_nobody_hears_me(make_app, monkeypatch):
    monkeypatch.setattr(ui, "NOBODY_HEARS_AFTER_S", 0.6)
    app, eng, s, hk = make_app("nobody")
    pump(app, 0.2)
    assert not app.alert.visible("nobody")               # pas tout de suite : le temps de se découvrir
    pump(app, 1.0)
    assert app.alert.visible("nobody")
    b = app.alert._banners["nobody"]
    assert "Personne ne te reçoit" in b.label.cget("text") and "pare-feu" in b.label.cget("text")
    assert b.button.winfo_manager()
    b.button.invoke()
    pump(app, 0.3)
    assert app.help_win is not None and app.help_win.winfo_exists()
    # dès qu'un ami nous reçoit, le bandeau disparaît
    eng.set_peer_hears("t2", True)
    pump(app, 0.3)
    assert not app.alert.visible("nobody")


def test_no_banner_when_someone_hears_me(make_app, monkeypatch):
    monkeypatch.setattr(ui, "NOBODY_HEARS_AFTER_S", 0.2)
    app, eng, s, hk = make_app("full")                   # Marie nous reçoit, Léa non
    pump(app, 0.8)
    assert not app.alert.visible("nobody")


def test_vocabulary_casque_not_sourdine(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    eng.set_deafened(True)
    app._twm_until = time.monotonic() + 5                # le bandeau « tu parles micro coupé » est maintenu
    pump(app, 0.4)
    alltext = " | ".join(_texts(app) + [app._mic_tip, app._deaf_tip])
    assert "ourdine" not in alltext
    assert app.deaf_btn.title_lbl.cget("text") == "Casque coupé"
    assert app.me_row.sub_lbl.cget("text").startswith("Casque coupé")
    app.alert.show("twm", "x")                           # le rendu le remplace dès le tick suivant
    pump(app, 0.3)
    b = app.alert._banners["twm"]
    assert b.label.cget("text").startswith("Casque coupé : tu n'entends personne")
    assert b.button.cget("text") == "Tout réactiver"
    # réglages : « près de l'horloge » et explication de la stabilité du son
    app.open_settings()
    pump(app, 0.4)
    texts = _texts(app.settings_win)
    assert any("près de l'horloge" in t for t in texts)
    assert not any("zone de notification" in t.lower() or "ourdine" in t for t in texts)
    assert any("Si le son hache" in t for t in texts)
    assert any("Couper / rétablir le casque" in t for t in texts)


def test_tray_vocabulary_micro_casque(make_app):
    class FakeTray:
        available = True

        def __init__(self):
            import queue as q
            self.commands = q.SimpleQueue()
            self.last = None

        def update(self, state, title, mic_on, sound_on, denoise=None):
            self.last = (state, title)

        def notify(self, msg):
            pass

        def stop(self):
            pass

    app, eng, s, hk = make_app("full")
    app.tray = FakeTray()
    eng.set_deafened(True)
    pump(app, 0.3)
    assert "casque et micro coupés" in app.tray.last[1] and "son coup" not in app.tray.last[1]


def test_long_shortcuts_wrap_in_big_buttons(make_app):
    app, eng, s, hk = make_app("full")
    s.mute_key, s.deafen_key = "ctrl_right+f10", "mouse5"
    app.geometry("480x600")
    pump(app, 0.6)
    for btn in (app.mic_btn, app.deaf_btn):
        lbl = btn.hint_lbl
        lines = lbl.cget("text").count("\n") + 1
        assert lbl.winfo_height() >= 13 * lines          # toutes les lignes tiennent dans le label
        assert lbl.winfo_y() + lbl.winfo_height() <= btn.winfo_height()
    assert "F10" in app.mic_btn.hint_lbl.cget("text") and "Souris 5" in app.deaf_btn.hint_lbl.cget("text")


def test_toast_does_not_cover_participants(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    app.toast("Marie a rejoint")
    pump(app, 0.2)
    t = app.toast_lbl
    lst = app.list_holder
    assert t.winfo_rooty() >= lst.winfo_rooty() + lst.winfo_height() - 2       # sous la liste
    assert t.winfo_rooty() + t.winfo_height() <= app.bar.winfo_rooty() + app.bar.winfo_height()


def test_long_names_have_full_name_tooltip(make_app):
    app, eng, s, hk = make_app("alone", name="Alexandre-Jean-Baptiste Montmorency")
    pid = eng.add_fake_peer("Ångström-Wolfeschlegelstein-Hausen")
    pump(app, 0.4)
    row = app.rows[pid]
    assert "…" in row.name_lbl.cget("text")
    assert "Ångström-Wolfeschlegelstein-Hausen" in row._name_tip.text
    assert "Alexandre-Jean-Baptiste Montmorency" in app.me_row._name_tip.text


def test_version_warning_is_a_readable_banner(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    eng.push_event("network_warning", "L'ordinateur 192.168.1.77 utilise une autre version de Vocal LAN : "
                   "il faut installer la même version partout.")
    pump(app, 0.3)
    keys = [k for k in app.banners._banners if k.startswith("net_version")]
    assert keys and app.banners.visible(keys[0])
    txt = app.banners._banners[keys[0]].label.cget("text")
    assert "pas la même version" in txt and "192.168.1.77" in txt


def test_wizard_step3_hint_under_disabled_button(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    wiz = app.wizard
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()
    wiz.next()
    pump(app, 0.3)
    assert wiz.mode_var.get() == 0 and not wiz.nav_hint.winfo_ismapped()
    wiz.mode_var.set(1)
    wiz._mode_changed()
    pump(app, 0.3)
    assert str(wiz.next_btn.cget("state")) == "disabled"
    assert wiz.nav_hint.winfo_ismapped() and "Micro toujours ouvert" in wiz.nav_hint.cget("text")
    wiz.mode_var.set(0)
    wiz._mode_changed()
    pump(app, 0.2)
    assert not wiz.nav_hint.winfo_ismapped() and str(wiz.next_btn.cget("state")) == "normal"


def test_wizard_error_has_advice_and_refresh(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    wiz = app.wizard
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()
    pump(app, 0.3)
    assert hasattr(wiz.picker, "refresh_btn")
    wiz.picker._reverted("Ce micro ne s'ouvre pas.", None, None, "micro")
    t = wiz.err.label.cget("text")
    assert "Choisis un autre micro dans la liste ou rebranche-le, puis clique Actualiser" in t


def test_help_window_fits_and_scrolls(make_app):
    app, eng, s, hk = make_app("alone")
    pump(app)
    app.open_help()
    pump(app, 0.5)
    h = app.help_win
    assert h.winfo_height() >= 520
    assert any("Toujours rien" in t for t in _texts(h))
    assert h.winfo_children() and h.body_scroll.winfo_exists()


# =========================================================================== 2.1 : voix et suppression du bruit
JARGON = ("dB", "RNNoise", "VAD", "AGC", "rnnoise", "Wiener", "RMS", "dBFS")


def _no_jargon(widget):
    import re as _re
    for t in _texts(widget):
        for bad in JARGON:
            assert not _re.search(r"(?<![A-Za-zé])" + _re.escape(bad) + r"(?![A-Za-zé])", t), (bad, t)


def test_settings_my_voice_levels_style_and_delay(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    win = app.settings_win
    assert win.nr_seg.get() == "Aucune" and win.style_seg.get() == "Naturelle"
    assert "aucun" in win.delay_lbl.cget("text") and "total" in win.delay_lbl.cget("text")
    for label, level in (("Léger", "light"), ("Normal", "normal"), ("Fort", "strong")):
        ms = ui.denoise_delay(level)
        win.nr_seg.set(label)
        win._nr_changed(label)
        pump(app, 0.2)
        assert s.denoise_level == level and ("set_denoise_level", level) in eng.calls
        assert f"+{ms} ms" in win.delay_lbl.cget("text")
        assert win.nr_desc.cget("text").strip()
    # +25 ms (Normal et Fort) reste sous 30 ms : pas d'orange, une phrase rassurante et honnête
    assert win.delay_lbl.cget("text_color") != ui.ORANGE
    assert ui.LAG_REASSURE in win.delay_sub.cget("text")
    win.nr_seg.set("Léger")
    win._nr_changed("Léger")
    pump(app, 0.2)
    assert win.delay_lbl.cget("text_color") != ui.ORANGE
    win.style_seg.set("Plus claire")
    win._style_changed("Plus claire")
    assert s.voice_style == "clear"
    _no_jargon(win)


def test_settings_ab_listening_and_double_meter(make_app):
    app, eng, s, hk = make_app("full")
    s.denoise_level = "normal"
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    win = app.settings_win
    assert str(win.ab_seg.cget("state")) == "disabled"          # pas d'écoute en cours : rien à comparer
    win.monitor_btn.invoke()
    pump(app, 0.3)
    assert eng._monitor_until is not None and str(win.ab_seg.cget("state")) == "normal"
    win.ab_seg.set("Sans filtre")
    win._ab_changed("Sans filtre")
    pump(app, 0.2)
    assert eng._monitor_processed is False and win.ab_seg.get() == "Sans filtre"
    win.ab_seg.set("Avec filtre")
    win._ab_changed("Avec filtre")
    assert eng._monitor_processed is True
    # double barre : « Après » ne dépasse jamais « Avant » quand le bruit est réduit
    eng.force_noise_floor = 0.4
    for _ in range(10):
        pump(app, 0.1)
        assert win.meter_out.level <= win.meter_in.level + 0.02


def test_settings_advanced_tout_desactiver_and_toggles(make_app):
    app, eng, s, hk = make_app("full")
    s.denoise_level, s.voice_style = "strong", "clear"
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    win = app.settings_win
    win.hp_seg.set("Forte")
    win._hp_changed("Forte")
    assert s.fx_highpass_hz == 120
    app.eng_call("set_fx", recv_leveler=False)
    pump(app, 0.2)
    assert win.fx_vars["recv_leveler"].get() is False
    win._disable_all()
    pump(app, 0.3)
    assert (s.denoise_level, s.voice_style, s.fx_highpass, s.fx_limiter) == ("off", "natural", False, False)
    assert win.nr_seg.get() == "Aucune" and win.style_seg.get() == "Naturelle"


def test_main_chip_cuts_and_restores_noise_reduction(make_app):
    app, eng, s, hk = make_app("full")
    s.denoise_level = "normal"
    pump(app)
    assert app.nr_chip.winfo_ismapped() and app.nr_chip.cget("text") == "Bruit réduit"
    assert "Normal" in app._nr_tip and f"+{ui.denoise_delay('normal')} ms" in app._nr_tip
    app.nr_chip.invoke()
    pump(app, 0.2)
    assert s.denoise_level == "off" and app.nr_chip.cget("text") == "Réduire le bruit"
    assert "Normal" in app._nr_tip
    app.nr_chip.invoke()                     # rétablit le dernier cran choisi
    pump(app, 0.2)
    assert s.denoise_level == "normal" and app.nr_chip.cget("text") == "Bruit réduit"


def test_main_chip_first_activation_defaults_to_normal(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    assert s.denoise_level == "off"
    app.toggle_denoise()
    pump(app, 0.2)
    assert s.denoise_level == "normal"


def test_denoise_failed_banner_and_retry(make_app):
    app, eng, s, hk = make_app("denoise_fail")
    pump(app, 0.3)
    assert not app.banners.visible("denoise")
    pump(app, 2.4)                                         # le suppresseur tombe en panne à 2 s
    assert app.banners.visible("denoise")
    b = app.banners._banners["denoise"]
    assert "sans coupure" in b.label.cget("text") and b.button.cget("text") == "Réessayer"
    assert app.nr_chip.cget("text") == "Bruit réduit : arrêté"
    assert "relancer" in app._nr_tip
    b.button.invoke()
    pump(app, 0.3)
    assert not app.banners.visible("denoise") and app.nr_chip.cget("text") == "Bruit réduit"
    _no_jargon(app)


def test_clipping_alert_shows_then_clears(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    assert not app.alert.visible("clip")
    eng.force_clipping = True
    pump(app, 0.3)
    assert app.alert.visible("clip")
    assert "Ton micro sature" in app.alert._banners["clip"].label.cget("text")
    eng.force_clipping = False
    eng._mic_smooth = 0.0
    pump(app, 0.3)
    assert app.alert.visible("clip") or not eng.snapshot().clipping      # maintenue ~2 s : pas de clignotement
    app._clip_until = 0.0
    eng._mic_smooth = 0.0
    app.last_snap = None
    pump(app, 0.2)
    if not eng.snapshot().clipping:
        assert not app.alert.visible("clip")


def test_peer_clean_button_tooltip_and_toggle(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    row = app.rows["m1"]
    assert row.clean_btn.cget("text") == "Nettoyer sa voix" and "+25 ms" in row._clean_tip
    row.clean_btn.invoke()
    pump(app, 0.2)
    assert s.peer_prefs["m1"]["clean"] is True and row.clean_btn.cget("text") == "Voix nettoyée"
    assert "+25 ms" in row._clean_tip
    row.clean_btn.invoke()
    pump(app, 0.2)
    assert s.peer_prefs["m1"]["clean"] is False and row.clean_btn.cget("text") == "Nettoyer sa voix"


def test_peer_room_button_toggle(make_app):
    """« Dans ma pièce » : bouton par ami, mémorisé, texte et aide qui changent."""
    app, eng, s, hk = make_app("full")
    pump(app)
    row = app.rows["m1"]
    assert row.room_btn.cget("text") == "Dans ma pièce" and "même pièce" in row._room_tip
    row.room_btn.invoke()
    pump(app, 0.2)
    assert s.peer_prefs["m1"]["room"] is True and row.room_btn.cget("text") == "Écho retiré"
    assert ("set_peer_room", "m1", True) in eng.calls
    row.room_btn.invoke()
    pump(app, 0.2)
    assert s.peer_prefs["m1"]["room"] is False and row.room_btn.cget("text") == "Dans ma pièce"


def test_peer_clean_limit_gives_clear_message(make_app):
    app, eng, s, hk = make_app("clean3")
    pump(app)
    assert [app.rows[i].clean_btn.cget("text") for i in ("m1", "t2", "l3")] == ["Voix nettoyée"] * 3
    row = app.rows["k4"]
    row.clean_btn.invoke()
    pump(app, 0.3)
    assert ("set_peer_clean", "k4", True, "CLEAN_LIMIT") in eng.calls
    assert row.clean_btn.cget("text") == "Nettoyer sa voix"          # pas activé
    assert "3 voix" in app.toast_lbl.cget("text")
    assert "Maximum" in row.sub3_lbl.cget("text")
    # retirer le nettoyage d'un ami libère une place
    app.rows["m1"].clean_btn.invoke()
    pump(app, 0.2)
    row._clean_msg = ("", 0.0, "")
    row.clean_btn.invoke()
    pump(app, 0.2)
    assert s.peer_prefs["k4"]["clean"] is True


def test_noisy_friend_gets_discreet_suggestion(make_app):
    app, eng, s, hk = make_app("noisy_friend")
    pump(app)
    tom, marie = app.rows["t2"], app.rows["m1"]
    assert "Nettoyer" in tom.sub3_lbl.cget("text") and tom.sub3_lbl.cget("text_color") == ui.TEXT_DIM
    assert marie.sub3_lbl.cget("text") == ""
    tom.clean_btn.invoke()
    pump(app, 0.2)
    assert tom.sub3_lbl.cget("text") == ""                   # plus de suggestion une fois nettoyé


def test_peer_without_clean_support_hides_button(make_app):
    """Moteur 2.0 (PeerView sans « clean ») : le bouton n'apparaît pas, rien ne plante."""
    app, eng, s, hk = make_app("full")
    pump(app)
    row = app.rows["m1"]
    assert row._clean_shown is True

    class Old:
        id, name, ip = "m1", "Marie", "192.168.1.5"
        speaking, level, volume, muted = False, 0.0, 1.0, False
        remote_muted = remote_deafened = False
        quality, ping_ms, loss_pct, buffer_ms, hears_me = "good", 2.0, 0.0, 10, True

    row.update_peer(Old())
    assert row._clean_shown is False and not row.clean_btn.winfo_ismapped()


def test_wizard_page2_preselects_normal_and_shows_delay(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    wiz = app.wizard
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()
    pump(app, 0.4)
    assert s.denoise_level == "normal"                      # nouvelle installation : Normal présélectionné
    assert wiz.nr_seg.get() == "Normal" and f"+{ui.denoise_delay('normal')} ms" in wiz.nr_delay.cget("text")
    wiz.nr_seg.set("Fort")
    wiz._nr_changed("Fort")
    pump(app, 0.3)
    assert s.denoise_level == "strong" and f"+{ui.denoise_delay('strong')} ms" in wiz.nr_delay.cget("text")
    assert wiz.nr_delay.cget("text_color") != ui.ORANGE          # +25 ms : sous 30 ms, pas d'orange
    assert ui.LAG_REASSURE in wiz.nr_info.cget("text")
    assert str(wiz.ab_seg.cget("state")) == "disabled"
    wiz._toggle_monitor()
    pump(app, 0.3)
    assert str(wiz.ab_seg.cget("state")) == "normal"
    wiz.ab_seg.set("Sans filtre")
    wiz._ab_changed("Sans filtre")
    assert eng._monitor_processed is False
    assert not wiz.reco.winfo_ismapped()                    # pas de bruit ambiant : pas de recommandation
    _no_jargon(wiz)


def test_wizard_page2_recommends_strong_and_clear_when_noisy(make_app):
    app, eng, s, hk = make_app("alone", first_run_done=False, name="")
    pump(app)
    wiz = app.wizard
    wiz.name_entry.delete(0, "end")
    wiz.name_entry.insert(0, "Léo")
    wiz.next()
    eng.force_noise_floor = 0.35
    pump(app, 7.0)
    assert wiz._reco_state == "shown" and wiz.reco.winfo_ismapped()
    wiz._apply_reco()
    pump(app, 0.2)
    assert s.denoise_level == "strong" and s.voice_style == "clear"
    assert not wiz.reco.winfo_ismapped()


def test_tray_menu_denoise_command_and_state(make_app):
    class FakeTray:
        available = True

        def __init__(self):
            import queue as q
            self.commands = q.SimpleQueue()
            self.last = "?"

        def update(self, state, title, mic_on, sound_on, denoise=None):
            self.last = denoise

        def notify(self, msg):
            pass

        def stop(self):
            pass

    app, eng, s, hk = make_app("full")
    app.tray = FakeTray()
    pump(app, 0.3)
    assert app.tray.last is False
    app.tray.commands.put("denoise")
    pump(app, 0.3)
    assert s.denoise_level == "normal" and app.tray.last is True
    app.tray.commands.put("denoise")
    pump(app, 0.3)
    assert s.denoise_level == "off" and app.tray.last is False


def test_tray_real_object_accepts_denoise_state():
    import tray as tray_mod
    t = tray_mod.Tray()
    assert t._denoise_on is None
    t.update("grey", "Vocal LAN", True, True, denoise=True)
    assert t._denoise_on is True


def test_main_window_has_no_jargon(make_app):
    app, eng, s, hk = make_app("full")
    s.denoise_level = "strong"
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    _no_jargon(app)
    _no_jargon(app.settings_win)


def test_delay_turns_orange_only_above_30_ms(make_app, monkeypatch):
    import fake_engine
    app, eng, s, hk = make_app("full")
    monkeypatch.setitem(fake_engine.DENOISE_DELAY_MS, "strong", 40.0)
    s.denoise_level = "strong"
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    win = app.settings_win
    assert win.delay_lbl.cget("text_color") == ui.ORANGE
    assert "Léger" in win.delay_sub.cget("text") and ui.LAG_REASSURE not in win.delay_sub.cget("text")
    monkeypatch.setitem(fake_engine.DENOISE_DELAY_MS, "strong", 30.0)
    pump(app, 0.4)
    assert win.delay_lbl.cget("text_color") != ui.ORANGE            # 30 ms exactement : pas encore orange
    _no_jargon(win)


def test_normal_and_strong_descriptions_differ_and_say_the_same_delay():
    assert ui.DENOISE_TEXT["normal"] != ui.DENOISE_TEXT["strong"]
    assert "naturelle" in ui.DENOISE_TEXT["normal"] and "maximum" in ui.DENOISE_TEXT["strong"]
    assert ui.denoise_delay("normal") == ui.denoise_delay("strong") == 25


def test_manual_filter_is_greyed_with_normal_or_strong(make_app):
    app, eng, s, hk = make_app("full")
    s.denoise_level = "off"
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    win = app.settings_win
    assert str(win.gate.slider.cget("state")) == "normal"
    s.denoise_level = "strong"
    pump(app, 0.4)
    assert str(win.gate.slider.cget("state")) == "disabled" and "Inutile" in win.gate_hint.cget("text")
    s.denoise_level = "light"
    pump(app, 0.4)
    assert str(win.gate.slider.cget("state")) == "normal"          # Léger garde le filtre manuel


def test_disable_all_also_resets_manual_filter(make_app):
    app, eng, s, hk = make_app("full")
    s.gate_db = -40.0
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    app.settings_win._disable_all()
    assert s.gate_db <= -69.5


def test_peer_cleaner_failure_is_a_toast_not_a_banner_and_card_says_paused(make_app):
    import fake_engine
    app, eng, s, hk = make_app("full")
    pump(app)
    eng.events.put(fake_engine.Event("denoise_failed", "Le nettoyage de la voix de Marie s'est arrêté : sa voix "
                                     "passe sans traitement.", "m1", "Marie", code="peer"))
    pump(app, 0.4)
    assert not app.banners.visible("denoise")
    assert "Marie" in app.toast_lbl.cget("text")
    assert s.denoise_level == "off"                                  # rien n'a été relancé ni activé


def test_peer_card_says_paused_when_clean_is_asked_but_not_running(make_app):
    app, eng, s, hk = make_app("full")
    s.peer_prefs.setdefault("m1", {})["clean"] = True
    pump(app)
    row = app.rows["m1"]
    assert row.clean_btn.cget("text") == "Voix nettoyée"
    eng.fail_denoise()                          # clean demandé mais clean_active faux
    pump(app, 0.4)
    assert row.clean_btn.cget("text") == "En pause" and "pour l'instant" in row._clean_tip
    assert "pause" in row.sub3_lbl.cget("text")


def test_peer_clean_unavailable_message_speaks_of_voices_not_of_my_voice(make_app):
    import fake_engine
    app, eng, s, hk = make_app("full")
    pump(app)

    def boom(pid, on):
        raise fake_engine.EngineError("DENOISE_UNAVAILABLE", "La suppression du bruit n'est pas disponible. "
                                      "Ta voix passe sans traitement.")
    eng.set_peer_clean = boom
    app.rows["m1"].clean_btn.invoke()
    pump(app, 0.3)
    t = app.toast_lbl.cget("text")
    assert "nettoyage des voix" in t and "Ta voix" not in t
    assert app.rows["m1"].clean_btn.cget("text") == "Nettoyer sa voix"


# ---------------------------------------------------------------- v2.1c : correctifs de la revue
def test_peer_clean_unavailable_card_text_is_not_the_limit_text(make_app):
    """B3 : la ligne grise de la carte reflète le code d'erreur, pas toujours « Maximum : 3 voix »."""
    import fake_engine
    app, eng, s, hk = make_app("full")
    pump(app)

    def boom(pid, on):
        raise fake_engine.EngineError("DENOISE_UNAVAILABLE", "La suppression du bruit n'est pas disponible.")
    eng.set_peer_clean = boom
    row = app.rows["m1"]
    row.clean_btn.invoke()
    pump(app, 0.3)
    t = row.sub3_lbl.cget("text")
    assert "indisponible" in t and "Maximum" not in t and "3 voix" not in t


def test_chip_does_not_announce_restored_when_denoise_is_missing(make_app):
    """B7 : DLL absente, clic sur la puce : pas de « rétabli : Normal » ; on retombe sur Léger, dit honnêtement."""
    app, eng, s, hk = make_app("full")
    eng.denoise_ok = False
    pump(app)
    app._last_denoise = "normal"
    app.nr_chip.invoke()
    pump(app, 0.2)
    assert s.denoise_level == "light"
    t = app.toast_lbl.cget("text")
    assert "Normal" not in t and "Léger" in t


def test_natural_style_text_does_not_promise_cut_rumbles_when_highpass_is_off(make_app):
    """B8."""
    app, eng, s, hk = make_app("full")
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    win = app.settings_win
    assert "grondements" in win.style_desc.cget("text")
    eng.set_fx(highpass=False)
    pump(app, 0.3)
    t = win.style_desc.cget("text")
    assert "grondements" not in t and "coupé" not in t


def test_visible_texts_use_delai_not_retard_nor_cran(make_app):
    """B9 : un seul mot (« délai »), et pas de « cran » dans l'interface visible."""
    app, eng, s, hk = make_app("full")
    s.denoise_level = "strong"
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    texts = " ".join(_texts(app.settings_win)).lower() + " " + " ".join(_texts(app)).lower()
    texts += " " + ui.LAG_EXPLAIN.lower() + " ".join(ui.DENOISE_TEXT.values()).lower() + ui.STYLE_TEXT["clear"].lower()
    assert "retard" not in texts and "cran" not in texts.replace("écran", "").replace("ecran", "")


def test_settings_listening_hint_is_truthful_and_explains_silence(make_app):
    """m2 + coordination moteur : on ne promet pas que la barre ne bouge pas ; on dit que « Avec filtre »
    est ce que les amis reçoivent (donc rien si l'envoi est fermé)."""
    app, eng, s, hk = make_app("full")
    s.denoise_level = "normal"
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    win = app.settings_win
    win.monitor_btn.invoke()
    pump(app, 0.3)
    t = win.ab_hint.cget("text")
    assert "ne doit presque pas bouger" not in t
    assert "nettement baisser" in t
    assert "exactement ce que reçoivent tes amis" in t and "touche relâchée" in t


def test_settings_tick_does_not_reconfigure_segments_every_time(make_app):
    """B5 : pas de configure(state=...) à chaque tick (1,7 ms chacun dans customtkinter)."""
    app, eng, s, hk = make_app("full")
    pump(app)
    app.open_settings()
    pump(app, 0.4)
    win = app.settings_win
    calls = []
    for seg in (win.ab_seg, win.hp_seg):
        orig = seg.configure

        def spy(*a, _o=orig, _n=id(seg), **k):
            if "state" in k:
                calls.append(_n)
            return _o(*a, **k)
        seg.configure = spy
    pump(app, 0.6)
    assert calls == []


def test_wizard_recommendation_needs_two_seconds_of_non_speech():
    """B10 : la recommandation « Fort » repose sur les pauses (>= 2 s cumulées), jamais sur un échantillon
    isolé ni sur quelqu'un qui parle sans arrêt."""
    import wizard
    est = wizard.Wizard.noise_floor_estimate
    t = [i * 0.05 for i in range(240)]
    import math as _m
    steady = [(x, 0.35) for x in t]
    speech = [(x, 0.5 + 0.4 * _m.sin(x * 2 * _m.pi * 4)) for x in t]
    one_dip = speech[:200] + [(x, 0.3) for x, _ in speech[200:205]] + speech[205:]
    assert est(steady)[0] == pytest.approx(0.35) and est(steady)[1] >= 2.0
    assert est(speech)[0] is None                                  # parole continue : pas de recommandation
    assert est(one_dip)[0] is None                                 # un seul creux : pas assez de pause
    assert est(steady[:30])[0] is None                             # 1,5 s : pas assez
    mixed = speech[:100] + steady[:80] + speech[180:]              # 4 s de pause stable au milieu de la parole
    assert est(mixed)[0] == pytest.approx(0.35)
    # bruit calme : la pause est trouvée mais sous le seuil
    quiet = [(x, 0.05) for x in t]
    assert est(quiet)[0] == pytest.approx(0.05) and est(quiet)[0] < ui.NOISY_FLOOR


def test_ecoute_avec_filtre_micro_coupe_ou_touche_relachee_le_dit(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    win = app.settings_win
    win.monitor_btn.invoke()
    pump(app, 0.3)
    assert not win.mon_warn.winfo_manager()                      # micro ouvert : rien à signaler
    app.engine.set_muted(True)
    pump(app, 0.4)
    assert win.mon_warn.cget("text") == "Ton micro est coupé : personne ne t'entend"
    win.ab_seg.set("Sans filtre")
    win._ab_changed("Sans filtre")
    pump(app, 0.3)
    assert not win.mon_warn.winfo_manager()                      # Sans filtre reste audible
    win.ab_seg.set("Avec filtre")
    win._ab_changed("Avec filtre")
    app.engine.set_muted(False)
    app.engine.set_ptt(True)
    pump(app, 0.4)
    assert win.mon_warn.cget("text") == "Touche relâchée : personne ne t'entend"
    eng.ptt_down()
    pump(app, 0.4)
    assert not win.mon_warn.winfo_manager()


def test_infobulle_en_pause_utilise_clean_reason(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    row = app.rows["m1"]
    row.clean_btn.invoke()
    eng.force_clean_reason = "Tu nettoies déjà 3 voix, c'est le maximum."
    pump(app, 0.4)
    assert row.clean_btn.cget("text") == "En pause"
    assert "Tu nettoies déjà 3 voix, c'est le maximum." in row._clean_tip
    eng.force_clean_reason = None
    pump(app, 0.4)
    assert row.clean_btn.cget("text") == "Voix nettoyée"


def test_normal_et_fort_signalent_la_limite_du_chuchotement(make_app):
    app, eng, s, hk = make_app("full")
    pump(app)
    app.open_settings()
    pump(app, 0.3)
    win = app.settings_win
    for label in ("Normal", "Fort", "Léger"):
        win.nr_seg.set(label)
        win._nr_changed(label)
        pump(app, 0.2)
        has = "chuchotes" in win.nr_desc.cget("text")
        assert has == (label != "Léger")
    assert "Léger ou Aucune" in ui.DENOISE_TEXT["normal"] and "chuchotes" in ui.DENOISE_TEXT["strong"]
    import wizard
    assert wizard.DENOISE_TEXT is ui.DENOISE_TEXT
