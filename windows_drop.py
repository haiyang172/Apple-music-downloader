"""Accept Explorer file drops on the application's own Windows widgets."""

import ctypes
from ctypes import wintypes
import os
from tkinter import Misc, TclError


_WM_DROPFILES = 0x0233
_WM_NCDESTROY = 0x0082
_GA_ROOT = 2
_MAX_FILES = 512
_MAX_PATH = 32767
_MAX_DROP_CHARS = 262144


def install_drop_target(window, callback):
    """Register an Explorer drop target on the Tk thread and return its cleanup."""
    if os.name != "nt":
        return lambda: None

    hwnd_type = wintypes.HWND
    size_type = ctypes.c_size_t
    result_type = ctypes.c_ssize_t
    subclass_proc = ctypes.WINFUNCTYPE(result_type, hwnd_type, wintypes.UINT, size_type,
                                      result_type, size_type, size_type)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, hwnd_type, result_type)

    user32 = ctypes.WinDLL("user32")
    user32.GetAncestor.argtypes = (hwnd_type, wintypes.UINT)
    user32.GetAncestor.restype = hwnd_type
    user32.EnumChildWindows.argtypes = (hwnd_type, enum_proc, result_type)
    user32.EnumChildWindows.restype = wintypes.BOOL

    controls = ctypes.WinDLL("comctl32")
    controls.SetWindowSubclass.argtypes = (hwnd_type, subclass_proc, size_type, size_type)
    controls.SetWindowSubclass.restype = wintypes.BOOL
    controls.RemoveWindowSubclass.argtypes = (hwnd_type, subclass_proc, size_type)
    controls.RemoveWindowSubclass.restype = wintypes.BOOL
    controls.DefSubclassProc.argtypes = (hwnd_type, wintypes.UINT, size_type, result_type)
    controls.DefSubclassProc.restype = result_type

    shell = ctypes.WinDLL("shell32")
    shell.DragAcceptFiles.argtypes = (hwnd_type, wintypes.BOOL)
    shell.DragAcceptFiles.restype = None
    shell.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
    shell.DragQueryFileW.restype = wintypes.UINT
    shell.DragFinish.argtypes = (wintypes.HANDLE,)
    shell.DragFinish.restype = None

    attached = set()
    batches = []
    idle = None
    closed = False

    def drain():
        nonlocal idle
        idle = None
        pending = list(batches)
        batches.clear()
        for paths in pending:
            if closed:
                break
            callback(paths)

    def read_paths(handle):
        paths = []
        remaining = _MAX_DROP_CHARS
        count = shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        for index in range(min(count, _MAX_FILES)):
            length = shell.DragQueryFileW(handle, index, None, 0)
            if not 0 < length <= min(remaining, _MAX_PATH):
                continue
            path = ctypes.create_unicode_buffer(length + 1)
            if shell.DragQueryFileW(handle, index, path, len(path)) == length:
                paths.append(path.value)
                remaining -= length
        return paths

    @subclass_proc
    def on_message(hwnd, message, wparam, lparam, _identifier, _reference):
        nonlocal idle
        if message == _WM_DROPFILES:
            try:
                paths = read_paths(wparam)
                if paths and not closed:
                    batches.append(paths)
                    if idle is None:
                        idle = window.after_idle(drain)
            except (OSError, TclError):
                batches.clear()
            finally:
                shell.DragFinish(wparam)
            return 0
        if message == _WM_NCDESTROY:
            attached.discard(hwnd)
            controls.RemoveWindowSubclass(hwnd, on_message, 1)
        return controls.DefSubclassProc(hwnd, message, wparam, lparam)

    def attach(hwnd):
        if hwnd and hwnd not in attached and controls.SetWindowSubclass(hwnd, on_message, 1, 0):
            attached.add(hwnd)
            shell.DragAcceptFiles(hwnd, True)

    @enum_proc
    def attach_child(hwnd, _context):
        attach(hwnd)
        return True

    def root_of(widget):
        hwnd = widget.winfo_id()
        return user32.GetAncestor(hwnd, _GA_ROOT) or hwnd

    def on_map(event):
        widget = event.widget
        if closed or not isinstance(widget, Misc):
            return
        root = root_of(window)
        if widget is window:
            attach(root)
            user32.EnumChildWindows(root, attach_child, 0)
        elif root_of(widget) == root:
            attach(widget.winfo_id())

    def on_destroy(event):
        if event.widget is window:
            cleanup()

    root = root_of(window)
    attach(root)
    user32.EnumChildWindows(root, attach_child, 0)
    if not attached:
        raise OSError("Windows 无法启用文件拖放。")

    map_binding = window.bind("<Map>", on_map, add="+")
    destroy_binding = window.bind("<Destroy>", on_destroy, add="+")

    def cleanup():
        nonlocal closed, idle
        if closed:
            return
        closed = True
        if idle is not None:
            window.after_cancel(idle)
            idle = None
        batches.clear()
        for hwnd in attached.copy():
            shell.DragAcceptFiles(hwnd, False)
            controls.RemoveWindowSubclass(hwnd, on_message, 1)
        attached.clear()
        window.unbind("<Map>", map_binding)
        window.unbind("<Destroy>", destroy_binding)

    return cleanup
