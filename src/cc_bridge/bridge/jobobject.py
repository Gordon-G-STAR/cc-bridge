from __future__ import annotations

import ctypes
import sys


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_ASSIGN_ACCESS = PROCESS_TERMINATE | PROCESS_SET_QUOTA

_BOOL = ctypes.c_int
_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_LARGE_INTEGER = ctypes.c_longlong
_SIZE_T = ctypes.c_size_t
_ULONG_PTR = ctypes.c_size_t
_ULONGLONG = ctypes.c_ulonglong


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", _ULONGLONG),
        ("WriteOperationCount", _ULONGLONG),
        ("OtherOperationCount", _ULONGLONG),
        ("ReadTransferCount", _ULONGLONG),
        ("WriteTransferCount", _ULONGLONG),
        ("OtherTransferCount", _ULONGLONG),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _ULONG_PTR),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


def supported() -> bool:
    return sys.platform == "win32"


_kernel32 = None

if supported():
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateJobObjectW.argtypes = [_HANDLE, ctypes.c_wchar_p]
    _kernel32.CreateJobObjectW.restype = _HANDLE

    _kernel32.SetInformationJobObject.argtypes = [
        _HANDLE,
        ctypes.c_int,
        ctypes.POINTER(JOBOBJECT_EXTENDED_LIMIT_INFORMATION),
        _DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = _BOOL

    _kernel32.OpenProcess.argtypes = [_DWORD, _BOOL, _DWORD]
    _kernel32.OpenProcess.restype = _HANDLE

    _kernel32.AssignProcessToJobObject.argtypes = [_HANDLE, _HANDLE]
    _kernel32.AssignProcessToJobObject.restype = _BOOL

    _kernel32.TerminateJobObject.argtypes = [_HANDLE, ctypes.c_uint]
    _kernel32.TerminateJobObject.restype = _BOOL

    _kernel32.CloseHandle.argtypes = [_HANDLE]
    _kernel32.CloseHandle.restype = _BOOL


class _NoopJob:
    def assign(self, pid: int) -> bool:
        return False


class _WindowsJob:
    def __init__(self, handle):
        self._handle = handle

    def assign(self, pid: int) -> bool:
        if not self._handle or pid <= 0 or _kernel32 is None:
            return False

        process = _kernel32.OpenProcess(PROCESS_ASSIGN_ACCESS, False, pid)
        if not process:
            return False

        try:
            assigned = _kernel32.AssignProcessToJobObject(self._handle, process)
            return bool(assigned)
        finally:
            _close_handle(process)

    def _clear(self) -> None:
        self._handle = None


class _KillOnCloseJob:
    def __init__(self):
        self._handle = None
        self._job = _NoopJob()

    def __enter__(self):
        if not supported() or _kernel32 is None:
            return self._job

        handle = _create_configured_job()
        if not handle:
            return self._job

        self._handle = handle
        self._job = _WindowsJob(handle)
        return self._job

    def __exit__(self, exc_type, exc, tb) -> bool:
        handle = self._handle
        self._handle = None

        if isinstance(self._job, _WindowsJob):
            self._job._clear()

        if handle:
            _terminate_job(handle)
            _close_handle(handle)

        return False


def kill_on_close_job():
    return _KillOnCloseJob()


def _create_configured_job():
    if _kernel32 is None:
        return None

    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    configured = _kernel32.SetInformationJobObject(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not configured:
        _close_handle(handle)
        return None

    return handle


def _terminate_job(handle) -> bool:
    if _kernel32 is None or not handle:
        return False
    return bool(_kernel32.TerminateJobObject(handle, 0))


def _close_handle(handle) -> bool:
    if _kernel32 is None or not handle:
        return False
    return bool(_kernel32.CloseHandle(handle))
