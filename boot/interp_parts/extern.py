"""Interp mixin (extern) - verbatim segment of the original
boot/interp.py Interp class (lines 860..1013), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    HLPanic, ctypes, threading,
)

class InterpExtern(object):
    # ---------- extern (Stage 15) ----------
    _libc = None
    # Deep-scan-10 fix: per-signature extern wrapper cache. Previously
    # call_extern mutated the SHARED CDLL symbol's argtypes/restype on
    # every call — two tasks calling different extern functions (or the
    # same symbol with different declared signatures) raced the ABI,
    # corrupting marshalling. A CFUNCTYPE prototype bound to a fresh
    # _FuncPtr leaves the shared symbol untouched; the cache is guarded
    # by a lock because tasks call externs from multiple threads.
    _extern_cache = {}
    _extern_lock = threading.Lock()
    # ctypes restype per declared HLS return type (deep-scan-10: the
    # per-signature wrapper needs this mapping up front).
    _EXTERN_RESTYPES = {
        "int": ctypes.c_int64,
        "float": ctypes.c_double,
        "bool": ctypes.c_bool,
        "str": ctypes.c_char_p,
        "void": None,
        # any other type (list/map/struct/enum/tainted/...) -> opaque ptr
    }

    def _get_libc(self):
        """Lazily load libc for extern calls."""
        if self._libc is None:
            try:
                # `None` loads the default C library (libc on Linux,
                # msvcrt on Windows, libSystem on macOS).
                Interp._libc = ctypes.CDLL(None)
            except OSError as ex:
                raise HLPanic("cannot load libc for extern call: %s" % ex,
                              getattr(self, "line", 0)) from None
        return self._libc

    def call_extern(self, fn, args):
        """Call a C function via ctypes (Stage 15-alpha).

        The function signature is taken from the HLS declaration:
          - int -> c_int64
          - float -> c_double
          - bool -> c_bool
          - str -> c_char_p (passed as a null-terminated C string;
            HLS bytes are passed as-is; the caller is responsible for
            ensuring no embedded NUL bytes)
          - void -> no return
          - any other type (list/map/struct/enum/tainted/ptr) -> ptr
            (treated as an opaque pointer; the caller must ensure
            ABI compatibility)

        For the alpha, only int/str args are fully supported. Float
        and bool work via automatic ctypes conversion. Opaque pointers
        are NOT derefenced by the interpreter — they're passed as
        raw addresses.
        """
        libc = self._get_libc()
        name = fn["name"]
        ret = fn["ret"]
        try:
            c_fn = getattr(libc, name)
        except AttributeError:
            raise HLPanic("extern function not found in libc: %s" % name,
                          getattr(self, "line", 0)) from None
        # Set up the argument types.
        c_argtypes = []
        c_args = []
        for (pn, pt, _), v in zip(fn["params"], args, strict=True):
            if pt == "int":
                c_argtypes.append(ctypes.c_int64)
                c_args.append(int(v))
            elif pt == "float":
                c_argtypes.append(ctypes.c_double)
                c_args.append(float(v))
            elif pt == "bool":
                c_argtypes.append(ctypes.c_bool)
                c_args.append(bool(v))
            elif pt == "str":
                # HLS str is bytes. Pass as a null-terminated C string.
                # Deep-scan-19 fix (LOW, defence-in-depth): reject
                # embedded NUL bytes. C functions interpret the string
                # only up to the first NUL — passing "ls\0; rm -rf /"
                # to system() would execute only "ls" (silent
                # truncation). The caller is responsible for ensuring
                # no embedded NULs; this guard surfaces the bug cleanly.
                c_argtypes.append(ctypes.c_char_p)
                if isinstance(v, bytes):
                    if b"\x00" in v:
                        raise HLPanic("extern str argument contains embedded NUL byte "
                                      "(C would truncate at the NUL)", getattr(self, "line", 0))
                    c_args.append(v)
                else:
                    encoded = str(v).encode("utf-8")
                    if b"\x00" in encoded:
                        raise HLPanic("extern str argument contains embedded NUL byte "
                                      "(C would truncate at the NUL)", getattr(self, "line", 0))
                    c_args.append(encoded)
            else:
                # Deep-scan fix (C8): the previous code passed `id(v)` for
                # list/map/struct args. That's a raw CPython heap address,
                # which the C function would dereference as garbage — a
                # soundness hole. Now we panic with a clean error: opaque
                # pointer args are NOT supported (they require a real
                # ABI/marshalling layer that Stage 15-alpha doesn't have).
                # The user must declare extern fns with primitive types only
                # (int, float, bool, str) and marshal complex types via str.
                raise HLPanic(
                    "extern call to '%s': argument of type %s is not "
                    "supported (only int, float, bool, str args are "
                    "allowed in extern FFI; use a string-encoded form "
                    "for complex data)" % (name, pt),
                    getattr(self, "line", 0))
        # Deep-scan-10 fix: build (or reuse) a per-signature wrapper instead
        # of mutating the shared CDLL symbol — see the class comment on
        # _extern_cache. The key is (name, argtypes, restype) so every
        # declared signature gets its own immutable prototype.
        cache_key = (name, tuple(c_argtypes), ret)
        with Interp._extern_lock:
            c_fn = Interp._extern_cache.get(cache_key)
            if c_fn is None:
                proto = ctypes.CFUNCTYPE(
                    Interp._EXTERN_RESTYPES.get(ret, ctypes.c_void_p),
                    *c_argtypes)
                try:
                    c_fn = proto((name, libc))
                except AttributeError:
                    raise HLPanic(
                        "extern function not found in libc: %s" % name,
                        getattr(self, "line", 0)) from None
                Interp._extern_cache[cache_key] = c_fn
        # Call.
        try:
            result = c_fn(*c_args)
        except Exception as ex:
            raise HLPanic("extern call to '%s' failed: %s" % (name, ex),
                          getattr(self, "line", 0)) from None
        # Convert the return value back to HLS runtime values.
        if ret == "int":
            return int(result) if result is not None else 0
        if ret == "float":
            return float(result) if result is not None else 0.0
        if ret == "bool":
            return bool(result) if result is not None else False
        if ret == "str":
            # c_char_p returns bytes (null-terminated).
            if result is None:
                return b""
            if isinstance(result, bytes):
                return result
            return bytes(result)
        if ret == "void":
            return None
        # Opaque pointer -> int (the raw address).
        return int(result) if result is not None else 0

    # ---------- statements ----------
