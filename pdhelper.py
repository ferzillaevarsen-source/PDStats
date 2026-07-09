"""
PDStats Helper — авто-импорт турнирной истории из PokerDom
F1 → захват → push в GitHub → браузер подхватывает автоматически
"""
import sys, time, threading, json, ctypes, base64

# ── Настройки ─────────────────────────────────────────────────────────────────
HOTKEY        = "f1"
GITHUB_REPO   = "ferzillaevarsen-source/PDStats"
GITHUB_BRANCH = "main"
GITHUB_FILE   = "pdimport.json"

# Токен читается из pdhelper_config.json (не попадает в git)
import os as _os, pathlib as _pathlib
_cfg_path = _pathlib.Path(__file__).parent / "pdhelper_config.json"
if not _cfg_path.exists():
    _cfg_path.write_text('{"github_token": ""}', encoding="utf-8")
    ctypes.windll.user32.MessageBoxW(0,
        f"Создан файл настроек:\n{_cfg_path}\n\n"
        "Открой его и вставь свой GitHub Token в поле github_token.",
        "PDStats Helper — первый запуск", 0x40)
_cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
GITHUB_TOKEN = _cfg.get("github_token", "")

# ── Зависимости ───────────────────────────────────────────────────────────────
try:
    import win32gui, win32con, win32clipboard
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
_file_sha = None  # SHA текущего pdimport.json (нужен для обновления)

def gh_get_sha():
    """Получить текущий SHA файла (нужен для PUT)"""
    global _file_sha
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    try:
        r = requests.get(url, headers=_gh_headers, timeout=10)
        if r.status_code == 200:
            _file_sha = r.json().get("sha")
        elif r.status_code == 404:
            _file_sha = None  # файл ещё не создан
    except Exception:
        pass

def gh_push(text: str) -> bool:
    """Обновить pdimport.json в репозитории"""
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

def capture():
    global _status
    _status = "capturing"

    wins = find_pokerdom()
    if not wins:
        _status = "error"
        notify("PDStats Helper", "Окно PokerDom не найдено. Откройте клиент.")
        return

    hwnd, _ = wins[0]

    # Активируем окно PokerDom
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.5)

    # Очищаем буфер перед копированием
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.CloseClipboard()
    except Exception:
        try: win32clipboard.CloseClipboard()
        except: pass

    # Ctrl+A → Ctrl+C
    keyboard.send("ctrl+a")
    time.sleep(0.2)
    keyboard.send("ctrl+c")
    time.sleep(0.5)

    # Читаем результат
    text = ""
    try:
        win32clipboard.OpenClipboard()
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except Exception:
            pass
        win32clipboard.CloseClipboard()
    except Exception:
        try: win32clipboard.CloseClipboard()
        except: pass

    if not text or not text.strip():
        _status = "error"
        notify("PDStats Helper", "Буфер пуст. Открой вкладку Турнир в PokerDom и попробуй снова.")
        return

    _status = "pushing"
    lines = len([l for l in text.splitlines() if l.strip()])
    notify("PDStats Helper", f"{lines} строк — отправляю в GitHub...")

    if gh_push(text):
        _status = "ok"
        notify("PDStats Helper", "Готово! Данные появятся в браузере через 5 сек.")
    else:
        _status = "error"

def on_hotkey():
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
        pystray.MenuItem(f"F1 — захват из PokerDom", None, enabled=False),
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
    gh_get_sha()          # получаем SHA файла заранее
    keyboard.add_hotkey(HOTKEY, on_hotkey)
    run_tray()            # блокирующий вызов
