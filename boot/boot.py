#!/usr/bin/env python3
"""Stage-0 bootstrap cho Hieu Louis (HLS).

Đây là HẠT GIỐNG (seed) để khởi động chu trình tự dịch của Hieu Louis:
  1. boot.py chạy được mã HLS trực tiếp (thông dịch, có kiểm tra kiểu + effects).
  2. Dùng boot.py để chạy trình biên dịch `src/hlc.hls` (viết bằng HLS).
  3. Từ đó trở đi, chu trình biên dịch native tự duy trì.

Cách dùng:
  python3 boot/boot.py [--check] <tep.hls> [doi so chuong trinh...]
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boot.lexer import tokenize, HLError          # noqa: E402
from boot.parser import Parser                     # noqa: E402
from boot.checker import check                     # noqa: E402
from boot.interp import Interp                     # noqa: E402


def run_cli():
    args = sys.argv[1:]
    check_only = False
    if "--check" in args:
        check_only = True
        args.remove("--check")
    if not args:
        sys.stderr.write("cach dung: boot.py [--check] <tep.hls> [doi so chuong trinh...]\n")
        return 2
    path = args[0]
    prog_args = [a.encode("utf-8") for a in args]
    try:
        with open(path, "rb") as f:
            src = f.read()
    except OSError:
        sys.stderr.write("loi: khong the mo tep %s\n" % path)
        return 2
    try:
        toks = tokenize(src)
        program = Parser(toks).parse_program()
        check(program)
    except HLError as ex:
        sys.stderr.write("loi bien dich: %s\n" % ex)
        return 1
    if check_only:
        sys.stdout.write("OK: kieu va effects hop le\n")
        return 0
    interp = Interp(program, prog_args, sys.stdout.buffer)
    return interp.run()


def main():
    try:
        return run_cli()
    except SystemExit as ex:
        sys.stdout.buffer.flush()
        return ex.code if ex.code is not None else 0


if __name__ == "__main__":
    _result = {}

    def _runner():
        _result["code"] = main()

    sys.setrecursionlimit(1000000)
    _stack = 512 * 1024 * 1024
    while _stack >= 8 * 1024 * 1024:
        try:
            threading.stack_size(_stack)
            break
        except (ValueError, RuntimeError, OverflowError):
            _stack //= 2
    _t = threading.Thread(target=_runner)
    _t.start()
    _t.join()
    sys.stdout.buffer.flush()
    sys.exit(_result.get("code", 1))
