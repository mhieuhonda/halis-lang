"""emit_runtime - verbatim segment of the original tools/hlwasm.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hwasm_common import (
    BLOCK_VOID, HLError, I32, I64, OP_BLOCK, OP_BR, OP_BR_IF, OP_CALL,
    OP_ELSE, OP_END, OP_F64_CONVERT_I64_S, OP_I32_ADD, OP_I32_AND, OP_I32_CONST, OP_I32_EQ, OP_I32_GE_S,
    OP_I32_LE_S, OP_I32_LOAD, OP_I32_LOAD8_U, OP_I32_LT_S, OP_I32_NE, OP_I32_STORE, OP_I32_STORE8, OP_I32_SUB,
    OP_I32_WRAP_I64, OP_I64_ADD, OP_I64_AND, OP_I64_CONST, OP_I64_DIV_S, OP_I64_DIV_U, OP_I64_EQ, OP_I64_EQZ,
    OP_I64_EXTEND_I32_S, OP_I64_LT_S, OP_I64_MUL, OP_I64_NE, OP_I64_REM_S, OP_I64_REM_U, OP_I64_SUB, OP_I64_TRUNC_F64_S,
    OP_I64_XOR, OP_IF, OP_LOCAL_GET, OP_LOCAL_SET, OP_LOCAL_TEE, OP_LOOP, OP_RETURN, OP_UNREACHABLE,
    sleb, uleb,
)

class WasmEmitterRuntime(object):
    def _emit_hl_alloc(self):
        # locals: 0 = n (param), 1 = temp for old heap ptr (i32)
        # Stack trace:
        #   i32.const HEAP_PTR_ADDR  i32.load   -> [heap_ptr]
        #   local.set 1                            -> []
        #   i32.const HEAP_PTR_ADDR                -> [addr]
        #   local.get 1  local.get 0  i32.add     -> [addr, heap_ptr+n]
        #   i32.store                              -> []
        #   local.get 1                            -> [heap_ptr]  (return)
        body = bytearray()
        body.append(OP_I32_CONST); body += sleb(self.HEAP_PTR_ADDR)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(self.HEAP_PTR_ADDR)
        body.append(OP_LOCAL_GET); body += uleb(1)  # old heap ptr
        body.append(OP_LOCAL_GET); body += uleb(0)  # n (param)
        body.append(OP_I32_ADD)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)  # return old heap ptr
        self.mod.add_code([(1, I32)], bytes(body))

    # ---- helper: hl_str_concat(a: i32, b: i32) -> c: i32 ----
    # Allocate 4 + a.len + b.len bytes; write len; copy a.data then b.data.
    # Uses memory.copy (bulk-memory 1.0) for the byte copies — cleaner
    # and smaller than a manual loop.
    def _emit_hl_str_concat(self, idx: int, idx_alloc: int):
        # locals:
        #   0 = a (param)
        #   1 = b (param)
        #   2 = a_len (i32)
        #   3 = b_len (i32)
        #   4 = total (i32)
        #   5 = result_ptr (i32)
        body = bytearray()
        # a_len = i32.load(a)
        body.append(OP_LOCAL_GET); body += uleb(0)  # a
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # b_len = i32.load(b)
        body.append(OP_LOCAL_GET); body += uleb(1)  # b
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # total = a_len + b_len
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # result_ptr = hl_alloc(4 + total)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_SET); body += uleb(5)
        # store total at result_ptr[0]
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        # memory.copy: copy a.data (at a+4) to result_ptr+4, length a_len.
        #   dst = result_ptr + 4
        #   src = a + 4
        #   n   = a_len
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # dst
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # src
        body.append(OP_LOCAL_GET); body += uleb(2)  # n = a_len
        body.append(0xFC); body += uleb(0x0A); body.append(0x00); body.append(0x00)  # memory.copy
        # memory.copy: copy b.data (at b+4) to result_ptr+4+a_len, length b_len.
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_GET); body += uleb(2)  # + a_len
        body.append(OP_I32_ADD)  # dst = result_ptr + 4 + a_len
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # src = b + 4
        body.append(OP_LOCAL_GET); body += uleb(3)  # n = b_len
        body.append(0xFC); body += uleb(0x0A); body.append(0x00); body.append(0x00)  # memory.copy
        # return result_ptr
        body.append(OP_LOCAL_GET); body += uleb(5)
        self.mod.add_code(
            [(1, I32), (1, I32), (1, I32), (1, I32)],
            bytes(body))

    # ---- helper: hl_int_to_str(n: i64) -> str ----
    # Handle n == 0, n < 0. Allocate up to 21 bytes (sign + 19 digits + NUL).
    def _emit_hl_int_to_str(self, idx: int, idx_alloc: int):
        # locals:
        #   0 = n (param, i64)
        #   1 = buf_ptr (i32) — 24-byte scratch buffer
        #   2 = pos (i32) — write position from the END of the buffer
        #   3 = negative (i32)
        #   4 = tmp (i64) — for division
        #   5 = result_ptr (i32)
        #   6 = len (i32)
        # Deep-scan-15 fix (MEDIUM severity): the previous implementation
        # used signed `i64.div_s` / `i64.rem_s` in the digit-extraction
        # loop. After `if negative: n = 0 - n`, the wraparound on
        # INT64_MIN leaves n unchanged (0 - INT64_MIN = INT64_MIN in
        # two's complement). The signed modulo then yields -8 (a
        # negative digit), producing a stream of garbage characters
        # instead of the correct "-9223372036854775808". Fix: after the
        # sign-flip, treat the value as UNSIGNED (i64.div_u / i64.rem_u)
        # — the bit pattern after the (wrapped) `0 - n` is the correct
        # absolute value when reinterpreted as unsigned, and unsigned
        # div/rem on a value < 2^63 gives identical results to signed
        # for the original non-negative inputs.
        body = bytearray()
        # Allocate 24 bytes for the scratch buffer.
        body.append(OP_I32_CONST); body += sleb(24)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_SET); body += uleb(1)  # buf_ptr
        # pos = 24 (write from the end, decrementing)
        body.append(OP_I32_CONST); body += sleb(24)
        body.append(OP_LOCAL_SET); body += uleb(2)  # pos
        # negative = (n < 0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_I64_LT_S)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # if negative: n = -n  (compute 0 - n; don't leave n on stack)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_SUB)
        body.append(OP_LOCAL_SET); body += uleb(0)
        body.append(OP_END)  # if
        # Handle n == 0 specially.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_EQZ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        # pos = 23; buf[23] = '0' (0x30)
        body.append(OP_I32_CONST); body += sleb(23)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(23)
        body.append(OP_I32_ADD)
        body.append(OP_I32_CONST); body += sleb(0x30)
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        body.append(OP_END)  # if
        # Loop: while n != 0: digit = n % 10; n /= 10; pos--; buf[pos] = digit + '0'
        # Deep-scan-15: UNSIGNED div/rem (i64.div_u / i64.rem_u) so the
        # post-wraparound value of INT64_MIN (bit pattern 0x8000...0)
        # is interpreted as 9223372036854775808, giving the correct
        # 19 digits "9223372036854775808" with the '-' prepended.
        body.append(OP_BLOCK); body.append(BLOCK_VOID)
        body.append(OP_LOOP); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_EQZ)
        body.append(OP_BR_IF); body += uleb(1)  # break
        # tmp = n % 10  (UNSIGNED — see deep-scan-15 fix above)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(10)
        body.append(OP_I64_REM_U)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # n = n / 10  (UNSIGNED)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(10)
        body.append(OP_I64_DIV_U)
        body.append(OP_LOCAL_SET); body += uleb(0)
        # pos--
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(-1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # buf[pos] = digit + '0'
        body.append(OP_LOCAL_GET); body += uleb(1)  # buf_ptr
        body.append(OP_LOCAL_GET); body += uleb(2)  # pos
        body.append(OP_I32_ADD)                       # buf_ptr + pos
        body.append(OP_LOCAL_GET); body += uleb(4)  # tmp (digit, i64)
        # tmp is already i64 (n % 10). Add 0x30 as i64, then wrap to i32.
        body.append(OP_I64_CONST); body += sleb(0x30)
        body.append(OP_I64_ADD)                       # digit + 0x30 (i64)
        body.append(OP_I32_WRAP_I64)                   # back to i32
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        body.append(OP_BR); body += uleb(0)  # loop
        body.append(OP_END)  # loop
        body.append(OP_END)  # block
        # If negative: pos--; buf[pos] = '-'
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(-1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_ADD)
        body.append(OP_I32_CONST); body += sleb(0x2D)  # '-'
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        body.append(OP_END)  # if
        # len = 24 - pos
        body.append(OP_I32_CONST); body += sleb(24)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_SUB)
        body.append(OP_LOCAL_SET); body += uleb(6)  # len
        # result_ptr = hl_alloc(4 + len)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_LOCAL_GET); body += uleb(6)
        body.append(OP_I32_ADD)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_SET); body += uleb(5)
        # store len at result_ptr[0]
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_LOCAL_GET); body += uleb(6)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        # Copy buf[pos .. pos+len) to result_ptr[4 .. 4+len)
        # Use memory.copy (0x0A) which is bulk-memory 1.0.
        #   dst = result_ptr + 4
        #   src = buf_ptr + pos
        #   n   = len
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # dst
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_ADD)  # src
        body.append(OP_LOCAL_GET); body += uleb(6)  # n
        # memory.copy opcode = 0x0A 0x00 0x00 (src dst memidx)
        # Actually the operand order on the STACK is: dst, src, n.
        # The opcode bytes are: 0xFC 0x0A 0x00 0x00.
        body.append(0xFC); body += uleb(0x0A); body.append(0x00); body.append(0x00)
        # return result_ptr
        body.append(OP_LOCAL_GET); body += uleb(5)
        # Locals (indices 1-6): buf_ptr(i32), pos(i32), negative(i32),
        # tmp(i64), result_ptr(i32), len(i32).
        self.mod.add_code(
            [(1, I32), (1, I32), (1, I32), (1, I64), (1, I32), (1, I32)],
            bytes(body))

    # ---- helper: hl_bool_to_str(b: i32) -> str ----
    # Returns pointer to "true" or "false" literal (pre-allocated in the pool).
    def _emit_hl_bool_to_str(self, idx: int):
        true_off = self._intern_str(b"true")
        false_off = self._intern_str(b"false")
        body = bytearray()
        # if b: return true_off else return false_off
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_IF); body.append(I32)
        body.append(OP_I32_CONST); body += sleb(true_off)
        body.append(OP_ELSE)
        body.append(OP_I32_CONST); body += sleb(false_off)
        body.append(OP_END)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_float_to_str(f: f64) -> str ----
    # Calls the JS helper hl_js_f64_to_str (which allocates in wasm memory
    # via the exported hl_alloc and returns the pointer).
    def _emit_hl_float_to_str(self, idx: int):
        # The JS import is at function index 2 (after hl_js_println=0,
        # hl_js_print=1). We recorded it in self.func_index.
        js_idx = self.func_index.get("hl_js_f64_to_str")
        if js_idx is None:
            # Should not happen — _declare_js_imports always adds it.
            raise HLError("internal: hl_js_f64_to_str import not declared", 0, 0)
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_CALL); body += uleb(js_idx)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_str_eq(a: i32, b: i32) -> i32 ----
    # Uses direct `return` for the early-exit paths (cleaner than br to a
    # result block, which would require the branch value on the stack
    # below the br_if condition).
    def _emit_hl_str_eq(self, idx: int):
        # locals: 0=a, 1=b, 2=a_len, 3=b_len, 4=i
        body = bytearray()
        # a_len = i32.load(a)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # b_len = i32.load(b)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # if a_len != b_len: return 0
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I32_NE)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_RETURN)
        body.append(OP_END)  # if
        # i = 0
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # loop:
        #   if i >= a_len: return 1 (all matched)
        #   if a[4+i] != b[4+i]: return 0 (mismatch)
        #   i++
        #   br 0
        body.append(OP_BLOCK); body.append(BLOCK_VOID)
        body.append(OP_LOOP); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_GE_S)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_RETURN)
        body.append(OP_END)  # if
        # load a[4+i]
        # Stage 24 fix: address is (a + i) + 4. The previous code pushed
        # a, i, 4 and called i32.add once, which computed (i + 4) —
        # the 'a' was left on the stack unused and the load read from
        # the wrong address. Use the load's offset immediate instead
        # (cleaner + smaller code): push (a + i), then load with offset=4.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(4)
        # load b[4+i]
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(4)
        body.append(OP_I32_NE)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_RETURN)
        body.append(OP_END)  # if
        # i++
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(4)
        body.append(OP_BR); body += uleb(0)
        body.append(OP_END)  # loop
        body.append(OP_END)  # block
        # Should never reach here (loop always returns), but wasm needs
        # a value on the stack for the function's return type.
        body.append(OP_I32_CONST); body += sleb(1)
        self.mod.add_code([(1, I32), (1, I32), (1, I32)], bytes(body))

    # ---- helper: hl_str_len(s: i32) -> i32 ----
    def _emit_hl_str_len(self, idx: int):
        # Stage 24 fix: hl_str_len returns i64 (HLS int), not i32.
        # The length is stored as a 32-bit int at offset 0 of the string
        # header; we load it as i32 then sign-extend to i64.
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_I64_EXTEND_I32_S)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_str_byte_at(s: i32, i: i64) -> i64 ----
    def _emit_hl_str_byte_at(self, idx: int):
        # locals: 0=s, 1=i
        # return (i64) s.data[i]  = i32.load8_u(s + 4 + i)
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        # We have s (i32) and i (i64) on the stack. Need to compute s + 4 + i
        # as i32. Use i32.wrap_i64 on i, then i32.add.
        body.append(OP_I32_WRAP_I64)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_I64_EXTEND_I32_S)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_chr_to_str(n: i64) -> str ----
    # Allocate a 5-byte string (len=1, data=byte, padding).
    def _emit_hl_chr_to_str(self, idx: int, idx_alloc: int):
        body = bytearray()
        # result_ptr = hl_alloc(8)  (4 len + 1 byte + 3 pad)
        body.append(OP_I32_CONST); body += sleb(8)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_TEE); body += uleb(1)  # local 1 = result_ptr
        # store len=1
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        # store byte at result_ptr[4]
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_GET); body += uleb(0)  # n (i64)
        body.append(OP_I32_WRAP_I64)
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        # return result_ptr
        body.append(OP_LOCAL_GET); body += uleb(1)
        # local 0 = n (i64), local 1 = result_ptr (i32)
        self.mod.add_code([(1, I32)], bytes(body))

    # ---- helper: hl_int_abs(n: i64) -> i64 ----
    # abs(n) = n < 0 ? -n : n
    # Deep-scan-15 fix (MEDIUM severity): for n == INT64_MIN, the
    # `0 - n` wraparound yields INT64_MIN itself (two's complement
    # overflow). The C runtime (`hl_abs_i64` in src/hlc.hls) panics
    # with "integer overflow" on this input; the interpreter raises
    # HLPanic. The wasm path must TRAP to match (silent wrong answer
    # is worse than a visible trap). Use `i64.eq` + `if ... unreachable`
    # to trap on INT64_MIN, then the standard `0 - n` for the rest.
    def _emit_hl_int_abs(self, idx: int):
        body = bytearray()
        # if n == INT64_MIN: unreachable (trap, matching hl_die("integer overflow"))
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(-9223372036854775808)
        body.append(OP_I64_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_UNREACHABLE)
        body.append(OP_END)
        # Condition: n < 0 (produces i32)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_I64_LT_S)
        # if (result i64): 0 - n  else  n
        body.append(OP_IF); body.append(I64)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_SUB)  # 0 - n
        body.append(OP_ELSE)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_END)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_str_to_int(s: i32) -> i64 ----
    # Parses a decimal integer from the str's data. Skips leading whitespace;
    # handles optional leading '-'. Stops at the first non-digit byte.
    def _emit_hl_str_to_int(self, idx: int):
        # locals: 0 = s (param), 1 = len, 2 = i, 3 = result (i64),
        #         4 = negative (i32), 5 = byte/digit scratch (i32)
        # Deep-scan-15 fix (HIGH severity): the previous implementation
        # reused local 4 (the sign flag) as a scratch slot via
        # `OP_LOCAL_TEE 4` inside the digit loop. The `tee` WRITES
        # local 4 with the byte/digit value, which silently corrupts
        # the sign flag — the subsequent `OP_DROP` only removes the
        # stack copy, NOT the local write. So for inputs ending in
        # '0' (e.g. "-10", "-100"), local 4 ended at 0 (false) and
        # the `if negative: result = -result` branch was skipped,
        # silently parsing "-10" → 10. Fix: add a dedicated 5th
        # local for the digit scratch so the sign flag is preserved.
        body = bytearray()
        # len = i32.load(s)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(1)
        # i = 0, result = 0, negative = 0, digit = 0
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(3)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(4)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(5)
        # if i < len and s.data[4] == '-': negative = 1; i++
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_LT_S)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_I32_CONST); body += sleb(0x2D)  # '-'
        body.append(OP_I32_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_LOCAL_SET); body += uleb(4)  # negative = 1
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)  # i++
        body.append(OP_END)  # if '-'
        body.append(OP_END)  # if i < len
        # Loop: while i < len and '0' <= s.data[4+i] <= '9':
        #   result = result * 10 + (byte - '0')
        body.append(OP_BLOCK); body.append(BLOCK_VOID)
        body.append(OP_LOOP); body.append(BLOCK_VOID)
        # if i >= len: br 1 (break)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_GE_S)
        body.append(OP_BR_IF); body += uleb(1)
        # byte = s.data[4+i]   → local 5 (digit scratch; does NOT clobber sign)
        # Deep-scan-20 fix (HIGH): the load address was computed as a bare
        # `i + 4` — the pushed `s` stayed stranded under it on the stack
        # (silently discarded by the loop's br 0), so the helper parsed
        # ABSOLUTE linear memory instead of the string. "123".to_int()
        # returned 0. Compute s + (4 + i) like every other str helper.
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_LOCAL_TEE); body += uleb(5)
        # Range check: 0 <= byte - 0x30 < 10  →  push (byte >= '0') & (byte <= '9')
        # Deep-scan-30 fix (silent wrong result): a NON-digit used to be
        # silently skipped (the loop kept going and parsed the digits
        # after it: "12a34" -> 1234, "a42" -> 42). The C runtime
        # hl_die()s on a non-digit and the boot interpreter raises
        # HLPanic — trap here too, per the backend's established
        # convention (a visible halt beats a silent wrong answer).
        body.append(OP_I32_CONST); body += sleb(0x30)
        body.append(OP_I32_GE_S)
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(0x39)
        body.append(OP_I32_LE_S)
        body.append(OP_I32_AND)
        body.append(OP_IF); body.append(BLOCK_VOID)
        # digit = byte - '0'  (i32), then extend to i64
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(0x30)
        body.append(OP_I32_SUB)
        body.append(OP_I64_EXTEND_I32_S)  # digit (i64)
        # result = result * 10 + digit
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I64_CONST); body += sleb(10)
        body.append(OP_I64_MUL)
        body.append(OP_I64_ADD)
        body.append(OP_LOCAL_SET); body += uleb(3)
        body.append(OP_ELSE)
        body.append(OP_UNREACHABLE)  # panic: non-digit (parity with C/interp)
        body.append(OP_END)
        # i++
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_BR); body += uleb(0)
        body.append(OP_END)  # loop
        body.append(OP_END)  # block
        # if negative: result = -result
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I64_SUB)
        body.append(OP_LOCAL_SET); body += uleb(3)
        body.append(OP_END)
        # return result
        body.append(OP_LOCAL_GET); body += uleb(3)
        # locals: 1=len (i32), 2=i (i32), 3=result (i64),
        #         4=negative (i32), 5=digit scratch (i32).
        self.mod.add_code(
            [(1, I32), (1, I32), (1, I64), (1, I32), (1, I32)], bytes(body))

    # ---- helper: hl_str_to_float(s: i32) -> f64 ----
    # Delegates to a JS helper (the JS string-to-float is well-specified
    # and reimplementing it in wasm is error-prone). The JS helper reads
    # the string from memory and returns the f64.
    def _emit_hl_str_to_float(self, idx: int):
        # Add a JS import for str-to-float. This is a 4th standard import.
        # Actually, to keep the import set stable, let me just call the
        # existing hl_js_f64_to_str in reverse... no, that doesn't work.
        # For the alpha, raise a clean error if str.to_float() is used.
        # The import isn't declared, so this body is never reached — but
        # we still need a valid body for the function.
        body = bytearray()
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_float_to_int(f: f64) -> i64 ----
    # Stage 24 (v0.43.0-alpha): truncate a float to int (floor toward
    # zero, matching the C runtime's float-to-int conversion). Uses
    # the wasm i64.trunc_f64_s instruction (opcode 0xAA).
    def _emit_hl_float_to_int(self, idx: int):
        body = bytearray()
        # local 0 is the f64 arg; emit i64.trunc_f64_s.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_TRUNC_F64_S)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_int_to_float(n: i64) -> f64 ----
    # Stage 24: convert int to float (wasm f64.convert_i64_s, opcode 0xBB).
    # Implements int.to_float().
    def _emit_hl_int_to_float(self, idx: int):
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_F64_CONVERT_I64_S)
        self.mod.add_code([], bytes(body))

    # ---- helpers: hl_panic / hl_abort / hl_exit ----
    # All three call the JS halt (hl_js_abort) and then `unreachable`.
    # For panic, the message is ignored for now (the JS halt can read it
    # from memory if desired). For abort/exit, the code is passed.
    def _emit_hl_panic(self, idx: int):
        # The JS import hl_js_abort takes (code: i32). For panic, we
        # pass 101 (matching the interpreter's exit code).
        body = bytearray()
        # We don't have hl_js_abort imported as such — but hl_js_println
        # is available; for a clean panic we just print "panic: <msg>"
        # and then unreachable. But to keep the alpha simple, just
        # unreachable.
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    def _emit_hl_abort(self, idx: int):
        body = bytearray()
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    def _emit_hl_exit(self, idx: int):
        # Deep-scan-20 fix (HIGH): hl_exit used to be an empty body —
        # `exit(42)` was silently swallowed and execution continued
        # past the exit call (diverging from every other backend).
        # A wasm trap is the closest alpha semantics to "halt now"; the
        # Node glue can catch the trap and exit with the code.
        body = bytearray()
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    # ---- helpers: hl_checked_add / hl_checked_sub / hl_checked_mul /
    #               hl_checked_rem — overflow-checked int64 arithmetic ----
    # Deep-scan-25 fix (soundness): the SPEC mandates checked int64
    # arithmetic ("every operation is checked"); the C runtime panics
    # and the boot interpreter raises HLPanic, but the wasm backend
    # used bare i64.add/sub/mul, which WRAP silently. These helpers
    # TRAP on the overflow event — the established wasm convention
    # (see _emit_hl_int_abs): a visible halt beats a silent wrong
    # answer. Locals: 0 = a (param), 1 = b (param), 2 = r, 3 = scratch.
    # Overflow identities (two's complement):
    #   add overflows iff ((a ^ r) & (b ^ r)) < 0
    #   sub overflows iff ((a ^ b) & (a ^ r)) < 0
    #   mul overflows iff b != 0 and b != -1 and r / b != a
    #     (b == 0 can't overflow; b == -1 only overflows for INT64_MIN,
    #     which is special-cased; i64.div_s below then can't trap)
    #   INT64_MIN % -1 overflows: wasm DEFINES it as 0, HLS panics.
    def _emit_hl_checked_add(self, idx: int):
        body = bytearray()
        # r = a + b
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # scratch = (a ^ r) & (b ^ r)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I64_XOR)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I64_XOR)
        body.append(OP_I64_AND)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # if scratch < 0: unreachable (overflow)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_I64_LT_S)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_UNREACHABLE)
        body.append(OP_END)
        # return r
        body.append(OP_LOCAL_GET); body += uleb(2)
        self.mod.add_code([(2, I64)], bytes(body))

    def _emit_hl_checked_sub(self, idx: int):
        body = bytearray()
        # r = a - b
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_SUB)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # scratch = (a ^ b) & (a ^ r)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_XOR)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I64_XOR)
        body.append(OP_I64_AND)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # if scratch < 0: unreachable (overflow)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_I64_LT_S)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_UNREACHABLE)
        body.append(OP_END)
        # return r
        body.append(OP_LOCAL_GET); body += uleb(2)
        self.mod.add_code([(2, I64)], bytes(body))

    def _emit_hl_checked_mul(self, idx: int):
        body = bytearray()
        # if b == 0: return 0 (a * 0 never overflows)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_EQZ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_RETURN)
        body.append(OP_END)
        # if b == -1:
        #   if a == INT64_MIN: unreachable (overflow)
        #   else: return 0 - a
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_CONST); body += sleb(-1)
        body.append(OP_I64_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(-9223372036854775808)
        body.append(OP_I64_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_UNREACHABLE)
        body.append(OP_END)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_SUB)
        body.append(OP_RETURN)
        body.append(OP_END)
        # r = a * b
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_MUL)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # if r / b != a: unreachable (overflow). b is neither 0 (early
        # return) nor -1 (special-cased), so div_s cannot trap here.
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_DIV_S)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_NE)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_UNREACHABLE)
        body.append(OP_END)
        # return r
        body.append(OP_LOCAL_GET); body += uleb(2)
        self.mod.add_code([(2, I64)], bytes(body))

    def _emit_hl_checked_rem(self, idx: int):
        body = bytearray()
        # HLS panics on INT64_MIN % -1 (C UB); wasm defines it as 0.
        # if a == INT64_MIN and b == -1: unreachable.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(-9223372036854775808)
        body.append(OP_I64_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_CONST); body += sleb(-1)
        body.append(OP_I64_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_UNREACHABLE)
        body.append(OP_END)
        body.append(OP_END)
        # a % b (i64.rem_s traps on b == 0, matching the HLS
        # division-by-zero panic-as-trap convention).
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I64_REM_S)
        self.mod.add_code([], bytes(body))

    # ---------- user function emission ----------



__all__ = [
]
