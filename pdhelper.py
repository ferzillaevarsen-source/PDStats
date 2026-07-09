"""
PDStats Helper — авто-импорт турнирной истории из PokerDom
F1 → захват (Ctrl+A, Ctrl+C в PokerDom) → push в GitHub → браузер подхватывает
"""
import sys, time, threading, json, ctypes, base64, logging, traceback

# ── Настройки ─────────────────────────────────────────────────────────────────
HOTKEY        = "f1"   # можно изменить в pdhelper_config.json ("hotkey")
GITHUB_REPO   = "ferzillaevarsen-source/PDStats"
GITHUB_BRANCH = "main"
GITHUB_FILE   = "pdimport.json"

# ── Лог ───────────────────────────────────────────────────────────────────────
import os as _os, pathlib as _pathlib

_log_path = _pathlib.Path(__file__).parent / "pdhelper.log"
logging.basicConfig(
    filename=str(_log_path),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("pdhelper")
log.info("=" * 60)
log.info("PDStats Helper запущен")

# Токен читается из pdhelper_config.json (не попадает в git)
_cfg_path = _pathlib.Path(__file__).parent / "pdhelper_config.json"
if not _cfg_path.exists():
    _cfg_path.write_text('{"github_token": ""}', encoding="utf-8")
    ctypes.windll.user32.MessageBoxW(0,
        f"Создан файл настроек:\n{_cfg_path}\n\n"
        "Открой его и вставь свой GitHub Token в поле github_token.",
        "PDStats Helper — первый запуск", 0x40)
_cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
GITHUB_TOKEN = _cfg.get("github_token", "")
HOTKEY       = _cfg.get("hotkey", HOTKEY)

# ── Зависимости ───────────────────────────────────────────────────────────────
try:
    import win32gui, win32con, win32clipboard, win32api, win32process
    import keyboard
    import pystray
    import requests
    from PIL import Image, ImageDraw
except ImportError as e:
    ctypes.windll.user32.MessageBoxW(0,
        f"Не хватает библиотек:\n{e}\n\n"
        "Выполни:\npython -m pip install pywin32 keyboard pystray Pillow requests",
        "PDStats Helper", 0x10)
    sys.exit(1)

# ── Состояние ─────────────────────────────────────────────────────────────────
_icon   = None
_status = "idle"

# ── GitHub API ────────────────────────────────────────────────────────────────
_gh_headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}
_file_sha = None

def gh_get_sha():
    global _file_sha
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    try:
        r = requests.get(url, headers=_gh_headers, timeout=10)
        if r.status_code == 200:
            _file_sha = r.json().get("sha")
        elif r.status_code == 404:
            _file_sha = None
    except Exception:
        pass

def gh_push(text: str) -> bool:
    global _file_sha
    payload = {
        "ts":     time.strftime("%Y-%m-%d %H:%M:%S"),
        "text":   text,
        "status": "ok",
        "lines":  len([l for l in text.splitlines() if l.strip()]),
    }
    content_b64 = base64.b64encode(
        json.dumps(payload, ensure_ascii=True).encode("ascii")
    ).decode()
    body = {
        "message": f"pdstats import {payload['ts']}",
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }
    if _file_sha:
        body["sha"] = _file_sha
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    try:
        r = requests.put(url, headers=_gh_headers, json=body, timeout=15)
        if r.status_code in (200, 201):
            _file_sha = r.json()["content"]["sha"]
            return True
        else:
            notify("PDStats Helper", f"GitHub ошибка {r.status_code}: {r.text[:80]}")
            return False
    except Exception as e:
        notify("PDStats Helper", f"Сеть: {e}")
        return False

# ── Захват PokerDom ───────────────────────────────────────────────────────────
def find_pokerdom():
    result = []
    def cb(hwnd, _):
        t = win32gui.GetWindowText(hwnd)
        if ("pokerdom" in t.lower() or "покердом" in t.lower()) and win32gui.IsWindowVisible(hwnd):
            result.append((hwnd, t))
    win32gui.EnumWindows(cb, None)
    return result

def force_to_foreground(hwnd):
    """Надёжный вывод окна на передний план (обходит ограничение Windows на фоновые процессы)."""
    try:
        iconic = win32gui.IsIconic(hwnd)
        log.debug(f"force_to_foreground: hwnd={hwnd}, iconic={iconic}")
        if iconic:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            log.debug("ShowWindow(SW_RESTORE) вызван")

        fg_hwnd = win32gui.GetForegroundWindow()
        fg_tid, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
        our_tid  = win32api.GetCurrentThreadId()
        log.debug(f"fg_hwnd={fg_hwnd}, fg_tid={fg_tid}, our_tid={our_tid}")

        attached = False
        if fg_tid and fg_tid != our_tid:
            try:
                win32process.AttachThreadInput(our_tid, fg_tid, True)
                attached = True
                log.debug("AttachThreadInput: OK")
            except Exception as e:
                log.warning(f"AttachThreadInput failed: {e}")

        win32gui.BringWindowToTop(hwnd)
        log.debug("BringWindowToTop: OK")
        win32gui.SetForegroundWindow(hwnd)
        log.debug("SetForegroundWindow: OK")
        try:
            win32gui.SetActiveWindow(hwnd)
            log.debug("SetActiveWindow: OK")
        except Exception as e:
            log.warning(f"SetActiveWindow failed: {e}")

        if attached:
            try:
                win32process.AttachThreadInput(our_tid, fg_tid, False)
                log.debug("AttachThreadInput detach: OK")
            except Exception as e:
                log.warning(f"AttachThreadInput detach failed: {e}")

        actual_fg = win32gui.GetForegroundWindow()
        log.debug(f"Текущее foreground после переключения: {actual_fg} (целевое: {hwnd}, совпадает: {actual_fg==hwnd})")
    except Exception as e:
        log.error(f"force_to_foreground exception: {e}\n{traceback.format_exc()}")

def capture():
    global _status
    _status = "capturing"
    log.info("─── capture() вызван ───")

    wins = find_pokerdom()
    log.info(f"find_pokerdom: найдено окон = {len(wins)}")
    for hwnd, title in wins:
        log.info(f"  hwnd={hwnd}, title={title!r}")

    if not wins:
        _status = "error"
        log.error("PokerDom не найден")
        notify("PDStats Helper", "Окно PokerDom не найдено. Откройте клиент.")
        return

    hwnd, title = wins[0]
    log.info(f"Используем окно: {title!r} hwnd={hwnd}")

    # Надёжно выводим PokerDom на передний план
    force_to_foreground(hwnd)
    time.sleep(0.5)

    # Кликаем в нижние 2/3 окна — область таблицы турниров
    try:
        rect = win32gui.GetWindowRect(hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = rect[1] + (rect[3] - rect[1]) * 2 // 3
        log.debug(f"Окно rect={rect}, клик в ({cx}, {cy})")
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.12)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.25)
        log.debug("Клик по окну: OK")
    except Exception as e:
        log.error(f"Клик по окну failed: {e}\n{traceback.format_exc()}")

    # Очищаем буфер
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.CloseClipboard()
        log.debug("Буфер очищен")
    except Exception as e:
        log.warning(f"Очистка буфера: {e}")
        try: win32clipboard.CloseClipboard()
        except: pass

    # Ctrl+A → Ctrl+C
    log.debug("Отправляю Ctrl+A...")
    keyboard.send("ctrl+a")
    time.sleep(0.3)
    log.debug("Отправляю Ctrl+C...")
    keyboard.send("ctrl+c")
    time.sleep(0.6)
    log.debug("Ctrl+A, Ctrl+C отправлены")

    # Читаем результат — перебираем все доступные форматы
    text = ""
    try:
        win32clipboard.OpenClipboard()

        # Перечисляем все форматы в буфере
        fmt_list = []
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            try:
                fname = win32clipboard.GetClipboardFormatName(fmt)
            except Exception:
                fname = f"#{fmt}"
            fmt_list.append(f"{fmt}={fname}")
            fmt = win32clipboard.EnumClipboardFormats(fmt)
        log.info(f"Форматы в буфере: {fmt_list}")

        # Пробуем CF_UNICODETEXT (13)
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            log.info("Прочитан CF_UNICODETEXT")
        except Exception:
            pass

        # Если не вышло — пробуем CF_TEXT (1, ANSI)
        if not text:
            try:
                raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                text = raw.decode("cp1251", errors="replace") if isinstance(raw, bytes) else str(raw)
                log.info("Прочитан CF_TEXT (cp1251)")
            except Exception as e:
                log.warning(f"CF_TEXT failed: {e}")

        # Если не вышло — пробуем HTML Format
        if not text:
            try:
                html_fmt = win32clipboard.RegisterClipboardFormat("HTML Format")
                raw = win32clipboard.GetClipboardData(html_fmt)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                log.info(f"Прочитан HTML Format (первые 500): {raw[:500]!r}")
                text = raw  # парсер разберётся или покажем в логе
            except Exception as e:
                log.warning(f"HTML Format failed: {e}")

        win32clipboard.CloseClipboard()
    except Exception as e:
        log.error(f"OpenClipboard failed: {e}")
        try: win32clipboard.CloseClipboard()
        except: pass

    log.info(f"Буфер обмена итог: длина={len(text)}, первые 300 симв.: {text[:300]!r}")

    if not text or not text.strip():
        _status = "error"
        log.error("Буфер пуст после Ctrl+A+C")
        notify("PDStats Helper", "Буфер пуст. Открой вкладку ТУРНИР в PokerDom и попробуй снова.")
        return

    _status = "pushing"
    lines = len([l for l in text.splitlines() if l.strip()])
    log.info(f"Текст захвачен: {lines} строк, отправляю в GitHub...")
    notify("PDStats Helper", f"{lines} строк — отправляю в GitHub...")

    if gh_push(text):
        _status = "ok"
        log.info("GitHub push: успех")
        notify("PDStats Helper", "Готово! Данные появятся в браузере через 5 сек.")
    else:
        _status = "error"
        log.error("GitHub push: ошибка")

def on_hotkey():
    log.info(f"Хоткей {HOTKEY!r} нажат")
    threading.Thread(target=capture, daemon=True).start()

# ── Трей ──────────────────────────────────────────────────────────────────────
def make_icon():
    img = Image.new("RGB", (64, 64), "#0f172a")
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill="#00c896")
    d.text((16, 20), "PD", fill="white")
    return img

def notify(title, msg):
    if _icon:
        try: _icon.notify(msg, title)
        except Exception: pass

def run_tray():
    global _icon
    menu = pystray.Menu(
        pystray.MenuItem(f"{HOTKEY.upper()} — захват из PokerDom", None, enabled=False),
        pystray.MenuItem(f"Репо: {GITHUB_REPO}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Захватить сейчас", lambda i, _: on_hotkey()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", lambda i, _: i.stop()),
    )
    _icon = pystray.Icon("PDStats Helper", make_icon(), "PDStats Helper", menu)
    _icon.run()

# ── Запуск ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"Хоткей: {HOTKEY!r}, репо: {GITHUB_REPO}, файл: {GITHUB_FILE}")
    log.info(f"Лог: {_log_path}")
    gh_get_sha()
    log.info(f"Начальный SHA файла: {_file_sha!r}")
    # suppress=True — клавиша не проходит в Windows (без снижения громкости!)
    keyboard.add_hotkey(HOTKEY, on_hotkey, suppress=True)
    log.info("Хоткей зарегистрирован, трей запускается...")
    run_tray()
