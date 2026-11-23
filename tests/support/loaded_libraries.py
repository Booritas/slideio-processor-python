"""Lists the shared libraries mapped into the current process."""
import ctypes
import os
import sys


def _windows():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModules.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    psapi.EnumProcessModules.restype = wintypes.BOOL
    kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetModuleFileNameW.restype = wintypes.DWORD

    process = kernel32.GetCurrentProcess()
    count = 1024
    while True:
        modules = (wintypes.HMODULE * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModules(process, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if needed.value <= ctypes.sizeof(modules):
            break
        count = needed.value // ctypes.sizeof(wintypes.HMODULE) + 16

    paths = []
    buffer = ctypes.create_unicode_buffer(32768)
    for module in modules[: needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        length = kernel32.GetModuleFileNameW(module, buffer, len(buffer))
        if length:
            paths.append(buffer.value)
    return paths


def _linux():
    paths = []
    with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
        for line in maps:
            fields = line.split(None, 5)
            if len(fields) == 6 and fields[5].startswith("/"):
                path = fields[5].strip()
                if path not in paths:
                    paths.append(path)
    return paths


def _macos():
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    system._dyld_image_count.restype = ctypes.c_uint32
    system._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    system._dyld_get_image_name.restype = ctypes.c_char_p
    paths = []
    for index in range(system._dyld_image_count()):
        name = system._dyld_get_image_name(index)
        if name:
            paths.append(os.fsdecode(name))
    return paths


def loaded_library_paths():
    """Returns the absolute paths of every shared library mapped into this process."""
    if sys.platform == "win32":
        return _windows()
    if sys.platform == "darwin":
        return _macos()
    return _linux()
