"""
PDStats Helper — авто-импорт турнирной истории из PokerDom.

Как работает:
  Хоткей (по умолчанию F9) → окно PokerDom на передний план → Ctrl+A, Ctrl+C →
  текст из буфера кладётся в локальный сервер http://127.0.0.1:12345/data,
  который браузер опрашивает напрямую. GitHub — необязательный облачный фолбэк.

Локального канала достаточно для одного ПК. GitHub включается, только если в
pdhelper_config.json задан github_token.
"""
import sys, time, threading, json, ctypes, base64, logging, traceback, webbrowser
import http.server

# ── Настройки по умолчанию (переопределяются в pdhelper_config.json) ────────────
HOTKEY        = "f9"
LOCAL_PORT    = 12345
GITHUB_REPO   = "ferzillaevarsen-source/PDStats"
GITHUB_BRANCH = "main"
GITHUB_FILE   = "pdimport.json"

# CORS: какие сайты-origin'ы могут читать локальный сервер.
# cors_allow_all=True сохраняет прежнее поведение (любой сайт). Для приватности
# поставьте False и перечислите свои origin'ы в cors_origins.
DEFAULT_CORS_ORIGINS = [
    "https://pdstats.ru", "https://www.pdstats.ru",
    "http://localhost", "http://127.0.0.1", "null",
]

import os as _os, pathlib as _pathlib

# ── Лог ─────────────────────────────────────────────────────────────────────────
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

# ── Конфиг ──────────────────────────────────────────────────────────────────────
_cfg_path = _pathlib.Path(__file__).parent / "pdhelper_config.json"
_DEFAULT_CFG = {
    "_comment": "github_token необязателен — без него работает только локальный "
                "канал (127.0.0.1). Задайте его, только если нужен облачный "
                "фолбэк через GitHub. cors_allow_all=false + cors_origins "
                "ограничивают, какие сайты могут читать локальные данные.",
    "github_token": "",
    "hotkey": HOTKEY,
    "local_port": LOCAL_PORT,
    "restore_clipboard": True,
    "cors_allow_all": True,
    "cors_origins": DEFAULT_CORS_ORIGINS,
}
if not _cfg_path.exists():
    _cfg_path.write_text(json.dumps(_DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    ctypes.windll.user32.MessageBoxW(0,
        f"Создан файл настроек:\n{_cfg_path}\n\n"
        "Helper уже готов к работе через локальный канал — просто запустите его "
        "и нажмите хоткей в окне PokerDom.\n\n"
        "GitHub-токен нужен ТОЛЬКО если хотите облачный фолбэк. Он опционален.",
        "PDStats Helper — первый запуск", 0x40)

try:
    _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
except Exception as e:
    log.error(f"Не удалось прочитать конфиг: {e}. Использую значения по умолчанию.")
    _cfg = dict(_DEFAULT_CFG)

GITHUB_TOKEN   = (_cfg.get("github_token") or "").strip()
HOTKEY         = _cfg.get("hotkey", HOTKEY)
LOCAL_PORT     = int(_cfg.get("local_port", LOCAL_PORT))
RESTORE_CLIP   = bool(_cfg.get("restore_clipboard", True))
CORS_ALLOW_ALL = bool(_cfg.get("cors_allow_all", True))
CORS_ORIGINS   = set(_cfg.get("cors_origins", DEFAULT_CORS_ORIGINS))
GITHUB_ENABLED = bool(GITHUB_TOKEN)

log.info(f"Режим: {'локальный + GitHub' if GITHUB_ENABLED else 'только локальный'}; "
         f"хоткей={HOTKEY!r}; порт={LOCAL_PORT}; cors_all={CORS_ALLOW_ALL}")

# ── Зависимости ───────────────────────────────────────────────────────────────
try:
    import win32gui, win32con, win32clipboard, win32api, win32process
    import keyboard
    import pystray
    from PIL import Image, ImageDraw
    if GITHUB_ENABLED:
        import requests
except ImportError as e:
    ctypes.windll.user32.MessageBoxW(0,
        f"Не хватает библиотек:\n{e}\n\n"
        "Выполни:\npython -m pip install pywin32 keyboard pystray Pillow requests",
        "PDStats Helper", 0x10)
    sys.exit(1)

# ── Состояние ─────────────────────────────────────────────────────────────────
_icon         = None
_status       = "idle"
_local_data   = None                 # последний захват — отдаётся локальным сервером
_data_lock    = threading.Lock()     # защита _local_data между потоками
_capture_lock = threading.Lock()     # защита от повторного входа в capture()

def set_local_data(d):
    global _local_data
    with _data_lock:
        _local_data = d

def get_local_data():
    with _data_lock:
        return _local_data or {"status": "empty"}

# ── Локальный HTTP-сервер (браузер опрашивает напрямую, без GitHub) ────────────
def _allowed_origin(origin: str):
    """Возвращает значение для Access-Control-Allow-Origin или None (запретить)."""
    if CORS_ALLOW_ALL:
        return "*"
    if origin and origin in CORS_ORIGINS:
        return origin
    return None

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_cors(self):
        acao = _allowed_origin(self.headers.get("Origin", ""))
        if acao:
            self.send_header("Access-Control-Allow-Origin", acao)
            if acao != "*":
                self.send_header("Vary", "Origin")
        elif self.headers.get("Origin"):
            log.debug(f"CORS: origin отклонён — {self.headers.get('Origin')!r}")
        # Обязателен для Chrome Private Network Access (file:// → http://127.0.0.1)
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('', '/'):
            # Раздаём index.html — тогда страница и /data на одном origin'е (нет CORS)
            html_file = _pathlib.Path(__file__).parent / 'index.html'
            try:
                body = html_file.read_bytes()
            except Exception:
                body = b'<h1>index.html not found in ' + str(html_file).encode() + b'</h1>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # /data или любой другой путь → JSON с данными
        body = json.dumps(get_local_data()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):  # не засорять лог
        pass

def _start_local_server():
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), _Handler)
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        log.info(f"Локальный сервер запущен: http://127.0.0.1:{LOCAL_PORT}/data")
        srv.serve_forever()
    except OSError as e:
        log.warning(f"Локальный сервер не запустился на порту {LOCAL_PORT}: {e}")
        notify("PDStats Helper",
               f"Порт {LOCAL_PORT} занят — возможно, helper уже запущен.")
    except Exception as e:
        log.error(f"Локальный сервер упал: {e}\n{traceback.format_exc()}")

# ── GitHub API (необязательный фолбэк) ─────────────────────────────────────────
_gh_headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_file_sha = None

def gh_get_sha():
    global _file_sha
    if not GITHUB_ENABLED:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}?ref={GITHUB_BRANCH}"
    try:
        r = requests.get(url, headers=_gh_headers, timeout=10)
        if r.status_code == 200:
            _file_sha = r.json().get("sha")
        elif r.status_code == 404:
            _file_sha = None
        elif r.status_code in (401, 403):
            log.warning(f"GitHub: доступ отклонён ({r.status_code}). Проверьте токен/права.")
    except Exception as e:
        log.debug(f"gh_get_sha: {e}")

def gh_push(text: str) -> bool:
    global _file_sha
    if not GITHUB_ENABLED:
        return False
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
        # SHA устарел (кто-то обновил файл) — перечитываем и пробуем ещё раз
        if r.status_code == 409:
            log.info("GitHub 409 — обновляю SHA и повторяю")
            gh_get_sha()
            if _file_sha:
                body["sha"] = _file_sha
                r = requests.put(url, headers=_gh_headers, json=body, timeout=15)
                if r.status_code in (200, 201):
                    _file_sha = r.json()["content"]["sha"]
                    return True
        notify("PDStats Helper", f"GitHub ошибка {r.status_code}: {r.text[:80]}")
        return False
    except Exception as e:
        notify("PDStats Helper", f"Сеть: {e}")
        return False

# ── Захват PokerDom ───────────────────────────────────────────────────────────
VK_CONTROL = 0x11
VK_A       = 0x41
VK_C       = 0x43

def keydown(vk):
    win32api.keybd_event(vk, 0, 0, 0)

def keyup(vk):
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

def send_ctrl(vk):
    """Отправляет Ctrl+vk через keybd_event (минует хук keyboard-библиотеки)."""
    keydown(VK_CONTROL); keydown(vk)
    time.sleep(0.06)
    keyup(vk); keyup(VK_CONTROL)


def find_pokerdom():
    result = []
    def cb(hwnd, _):
        t = win32gui.GetWindowText(hwnd)
        if ("pokerdom" in t.lower() or "покердом" in t.lower()) and win32gui.IsWindowVisible(hwnd):
            result.append((hwnd, t))
    win32gui.EnumWindows(cb, None)
    return result

def find_chrome_widget(parent):
    """Ищет Chrome_RenderWidgetHostHWND внутри Electron-окна."""
    found = []
    def cb(hwnd, _):
        try:
            if win32gui.GetClassName(hwnd) == 'Chrome_RenderWidgetHostHWND':
                found.append(hwnd)
        except Exception:
            pass
        return True
    try:
        win32gui.EnumChildWindows(parent, cb, None)
    except Exception:
        pass
    return found

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

def get_clipboard_text():
    """Возвращает текущий текст буфера (CF_UNICODETEXT) или None."""
    try:
        win32clipboard.OpenClipboard()
        try:
            t = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:
            t = None
        win32clipboard.CloseClipboard()
        return t
    except Exception:
        try: win32clipboard.CloseClipboard()
        except Exception: pass
        return None

def set_clipboard_text(t):
    """Восстанавливает текстовый буфер обмена."""
    if not t:
        return
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, t)
        win32clipboard.CloseClipboard()
    except Exception as e:
        log.warning(f"set_clipboard_text: {e}")
        try: win32clipboard.CloseClipboard()
        except Exception: pass

def read_clipboard() -> str:
    """Читает текст из буфера обмена, пробует Unicode и ANSI."""
    text = ""
    try:
        win32clipboard.OpenClipboard()
        fmt_list = []
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            try: fname = win32clipboard.GetClipboardFormatName(fmt)
            except: fname = f"#{fmt}"
            fmt_list.append(f"{fmt}={fname}")
            fmt = win32clipboard.EnumClipboardFormats(fmt)
        log.info(f"Форматы в буфере: {fmt_list}")
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            log.info("Прочитан CF_UNICODETEXT")
        except Exception:
            pass
        if not text:
            try:
                raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                text = raw.decode("cp1251", errors="replace") if isinstance(raw, bytes) else str(raw)
                log.info("Прочитан CF_TEXT")
            except Exception as e:
                log.warning(f"CF_TEXT failed: {e}")
        win32clipboard.CloseClipboard()
    except Exception as e:
        log.error(f"OpenClipboard failed: {e}")
        try: win32clipboard.CloseClipboard()
        except: pass
    return text


def capture():
    """Один захват. Защищён от повторного входа: параллельные вызовы игнорируются."""
    if not _capture_lock.acquire(blocking=False):
        log.info("capture: уже выполняется — повторный вызов пропущен")
        return
    global _status
    try:
        _status = "capturing"
        log.info("─── capture() вызван ───")

        wins = find_pokerdom()
        log.info(f"find_pokerdom: {[(h,t) for h,t in wins]}")
        if not wins:
            _status = "error"
            notify("PDStats Helper", "Окно PokerDom не найдено. Откройте клиент.")
            return

        hwnd, title = wins[0]
        log.info(f"Окно: {title!r} hwnd={hwnd}")

        # Сохраняем буфер пользователя, чтобы вернуть его после захвата
        prev_clip = get_clipboard_text() if RESTORE_CLIP else None

        # Выводим PokerDom на передний план
        force_to_foreground(hwnd)
        time.sleep(0.5)

        # Ищем Chrome_RenderWidgetHostHWND — реальный рендерер Electron
        widgets = find_chrome_widget(hwnd)
        log.info(f"Chrome_RenderWidgetHostHWND: {widgets}")

        render = widgets[-1] if widgets else None
        if render:
            try:
                win32gui.SetFocus(render)
                log.debug(f"SetFocus → {render}: OK")
                time.sleep(0.2)
            except Exception as e:
                log.warning(f"SetFocus render: {e}")

        # Очищаем буфер
        try:
            win32clipboard.OpenClipboard(); win32clipboard.EmptyClipboard(); win32clipboard.CloseClipboard()
            log.debug("Буфер очищен")
        except Exception as e:
            log.warning(f"EmptyClipboard: {e}")
            try: win32clipboard.CloseClipboard()
            except: pass

        # Ctrl+A → Ctrl+C через keybd_event (минует хук keyboard-библиотеки)
        log.debug("Отправляю Ctrl+A...")
        send_ctrl(VK_A)
        time.sleep(0.4)
        log.debug("Отправляю Ctrl+C...")
        send_ctrl(VK_C)
        time.sleep(0.7)

        text = read_clipboard()
        log.info(f"Буфер: длина={len(text)}, первые 300 симв.: {text[:300]!r}")

        # Возвращаем пользователю его буфер
        if RESTORE_CLIP:
            set_clipboard_text(prev_clip)

        if not text or not text.strip():
            _status = "error"
            log.error("Буфер пуст после Ctrl+A+C")
            notify("PDStats Helper", "Буфер пуст. Открой вкладку ТУРНИР в PokerDom и попробуй снова.")
            return

        lines = len([l for l in text.splitlines() if l.strip()])
        log.info(f"Захвачено {lines} строк")

        # Сразу кладём в локальный сервер — браузер подхватит менее чем за секунду
        set_local_data({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text, "status": "ok"})
        log.info("Локальные данные обновлены")

        if not GITHUB_ENABLED:
            _status = "ok"
            notify("PDStats Helper", f"Готово: {lines} строк переданы в браузер.")
            return

        _status = "pushing"
        notify("PDStats Helper", f"{lines} строк — отправляю в GitHub...")
        if gh_push(text):
            _status = "ok"
            log.info("GitHub push: успех")
            notify("PDStats Helper", "Готово! Данные уже в браузере (или через ~5 сек через GitHub).")
        else:
            # Локальный канал всё равно сработал — это не фатально
            _status = "ok"
            log.error("GitHub push: ошибка (локальный канал доставил данные)")
            notify("PDStats Helper", "Данные переданы локально. GitHub недоступен — это не критично.")
    except Exception as e:
        _status = "error"
        log.error(f"capture exception: {e}\n{traceback.format_exc()}")
        notify("PDStats Helper", f"Ошибка захвата: {e}")
    finally:
        _capture_lock.release()

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
    local_url = f"http://127.0.0.1:{LOCAL_PORT}/"
    mode_line = f"Репо: {GITHUB_REPO}" if GITHUB_ENABLED else "Режим: только локальный (127.0.0.1)"
    menu = pystray.Menu(
        pystray.MenuItem(f"PokerDom: Ctrl+A, Ctrl+C → {HOTKEY.upper()}", None, enabled=False),
        pystray.MenuItem(mode_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть PDStats в браузере", lambda i, _: webbrowser.open(local_url)),
        pystray.MenuItem("Захватить сейчас", lambda i, _: on_hotkey()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", lambda i, _: i.stop()),
    )
    _icon = pystray.Icon("PDStats Helper", make_icon(), "PDStats Helper", menu)

    def _startup_notify():
        time.sleep(1.0)
        notify("PDStats Helper",
               f"Открывай PDStats по адресу:\n{local_url}")
    threading.Thread(target=_startup_notify, daemon=True).start()

    _icon.run()

# ── Запуск ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"Хоткей: {HOTKEY!r}, GitHub: {'вкл' if GITHUB_ENABLED else 'выкл'}, файл: {GITHUB_FILE}")
    log.info(f"Лог: {_log_path}")
    threading.Thread(target=_start_local_server, daemon=True).start()
    if GITHUB_ENABLED:
        gh_get_sha()
        log.info(f"Начальный SHA файла: {_file_sha!r}")
    # suppress=True — клавиша не проходит в приложение (не мешает игре)
    try:
        keyboard.add_hotkey(HOTKEY, on_hotkey, suppress=True)
        log.info("Хоткей зарегистрирован, трей запускается...")
    except Exception as e:
        log.error(f"Не удалось зарегистрировать хоткей {HOTKEY!r}: {e}")
        ctypes.windll.user32.MessageBoxW(0,
            f"Не удалось зарегистрировать хоткей {HOTKEY!r}: {e}\n\n"
            "Попробуйте запустить от имени администратора или сменить hotkey в конфиге.",
            "PDStats Helper", 0x30)
    run_tray()
