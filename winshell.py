"""Windows tray icon + global hotkey, ctypes only (no dependencies).

A hidden window on a background thread owns the tray icon and the hotkey. It reports what
happened through `Tray.events` ("toggle" or "quit") so the Tk thread can poll it safely.
"""
import ctypes, queue, threading
from ctypes import wintypes as wt
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY, WM_CLOSE, WM_HOTKEY, WM_USER = 0x0002, 0x0010, 0x0312, 0x0400
WM_TRAY = WM_USER + 1
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
NIIF_INFO, NIN_BALLOONUSERCLICK = 1, 0x405
MOD_NOREPEAT, IDI_APPLICATION = 0x4000, 32512
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON = 1, 0x0010, 49
ICON_FILE = Path(__file__).with_name("worklog.ico")
HOTKEY_ID = 1
MODS = {"alt": 1, "ctrl": 2, "control": 2, "shift": 4, "win": 8}


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                ("d", ctypes.c_ubyte * 8)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uTimeout", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wt.HICON
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HICON
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]


def parse_hotkey(text):
    """'win+shift+w' -> (modifier flags, virtual-key code). Raises ValueError if unusable."""
    mods, key = 0, None
    for part in text.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part[:1] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 12:
            key = 0x6F + int(part[1:])
        else:
            raise ValueError(f"Can't read hotkey part '{part}' in '{text}'")
    if key is None or not mods:
        raise ValueError(f"Hotkey '{text}' needs at least one modifier and one key, e.g. ctrl+alt+w")
    return mods | MOD_NOREPEAT, key


def style_titlebar(tk_window, caption, text, border):
    """Dark title bar with custom colours ('#rrggbb'). Colours need Windows 11; the dark mode flag
    alone works on Windows 10 1903+. Fails silently elsewhere."""
    def colorref(hex_color):
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return r | g << 8 | b << 16

    try:
        dwm = ctypes.WinDLL("dwmapi")
        tk_window.update_idletasks()
        hwnd = user32.GetParent(tk_window.winfo_id())  # Tk's frame window is the real top-level
        for attr, value in ((20, 1), (35, colorref(caption)), (36, colorref(text)), (34, colorref(border))):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(wt.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def single_instance(name="WorklogWidgetMutex"):
    """Returns a handle to keep alive, or None if another copy is already running."""
    handle = kernel32.CreateMutexW(None, False, name)
    return None if ctypes.get_last_error() == 183 else handle  # ERROR_ALREADY_EXISTS


class Tray:
    def __init__(self, hotkey, tip="Worklog"):
        self.events = queue.Queue()
        self.hotkey_error = None
        self.hwnd = None
        self._ready = threading.Event()
        self._mods_key = parse_hotkey(hotkey)
        threading.Thread(target=self._run, args=(tip,), daemon=True).start()
        self._ready.wait(5)

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _load_icon(self):
        """worklog.ico next to this file at tray size; the default Windows icon if it's missing."""
        if ICON_FILE.exists():
            size = user32.GetSystemMetrics(SM_CXSMICON)
            icon = user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if icon:
                return icon
        return user32.LoadIconW(None, IDI_APPLICATION)

    def _icon(self, action):
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self.tip
        shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    def notify(self, title, text):
        """Tray balloon / Windows notification. Clicking it posts a 'show' event."""
        if not self.hwnd:
            return
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, 1, "Show / hide")
        user32.AppendMenuW(menu, 0, 2, "Quit")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # required or the menu never closes
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self.events.put("toggle" if cmd == 1 else "quit")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY:
            self.events.put("toggle")
            return 0
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.events.put("toggle")
            elif event == WM_RBUTTONUP:
                self._menu()
            elif event == NIN_BALLOONUSERCLICK:
                self.events.put("show")
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._icon(NIM_DELETE)
            user32.UnregisterHotKey(hwnd, HOTKEY_ID)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self, tip):
        self.tip = tip
        self._proc = WNDPROC(self._wndproc)  # keep a reference or it gets garbage collected
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.hInstance = kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "WorklogTrayWindow"
        user32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "Worklog", 0, 0, 0, 0, 0,
                                           None, None, cls.hInstance, None)
        self._icon(NIM_ADD)
        mods, key = self._mods_key
        if not user32.RegisterHotKey(self.hwnd, HOTKEY_ID, mods, key):
            self.hotkey_error = "Hotkey is already used by another program"
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
