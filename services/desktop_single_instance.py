"""Process-wide desktop launcher guard; intentionally independent of web/app startup."""
import ctypes
import os

MUTEX_NAME = r"Local\Amazon_SPAPI_Desktop_App_SingleInstance"
ERROR_ALREADY_EXISTS = 183

class SingleInstanceGuard:
    def __init__(self, name=MUTEX_NAME): self.name, self.handle, self.owned = name, None, False
    def acquire(self):
        if os.name != "nt": return True
        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateMutexW(None, True, self.name)
        if not self.handle: raise OSError("Unable to create application single-instance mutex")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.handle); self.handle = None; return False
        self.owned = True
        return True
    def release(self):
        if self.handle and os.name == "nt":
            if self.owned: ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
        self.handle, self.owned = None, False
