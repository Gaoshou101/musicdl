"""Small libseccomp wrapper used by the Python plugin child."""
from __future__ import annotations
import ctypes, errno, os
_DENIED = ("open openat openat2 creat socket socketpair connect bind listen accept accept4 clone clone3 fork vfork execve execveat ptrace process_vm_readv process_vm_writev kill tkill tgkill pidfd_open pidfd_getfd pidfd_send_signal setsid setpgid mount umount2 pivot_root chroot unshare setns keyctl add_key request_key bpf perf_event_open userfaultfd").split()
def install() -> bool:
    if os.name != "posix": return False
    try: lib = ctypes.CDLL("libseccomp.so.2")
    except OSError as exc: raise RuntimeError("libseccomp unavailable") from exc
    lib.seccomp_init.argtypes=[ctypes.c_uint32]; lib.seccomp_init.restype=ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]; lib.seccomp_syscall_resolve_name.restype=ctypes.c_int
    lib.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]; lib.seccomp_rule_add.restype=ctypes.c_int
    lib.seccomp_load.argtypes=[ctypes.c_void_p]; lib.seccomp_load.restype=ctypes.c_int
    lib.seccomp_release.argtypes=[ctypes.c_void_p]
    ctx=lib.seccomp_init(0x7FFF0000)
    if not ctx: raise RuntimeError("seccomp init failed")
    try:
        for name in _DENIED:
            number=lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0: continue
            if lib.seccomp_rule_add(ctx,0x00050000|errno.EPERM,number,0) != 0: raise RuntimeError("seccomp setup")
        if lib.seccomp_load(ctx) != 0: raise RuntimeError("seccomp load")
        return True
    finally: lib.seccomp_release(ctx)
