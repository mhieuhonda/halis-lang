"""Bộ kiểm tra kiểu & hiệu ứng Stage-0 cho HLS. Chuẩn theo SPEC.md mục 3-9.

Sản phẩm phụ: chú thích (annotation) lên AST để evaluator chạy nhanh:
  - moi bieu thuc co e['t'] = kieu (hoac 'never')
  - e['rc'] = ('user', key) | ('builtin', ten) voi loi goi ham
  - e['rm'] = ('user', key) | ('builtin', op) voi loi goi phuong thuc
  - program['edges'] = {fn_key: tap(calieu)} cho phan tich effects
"""
from .lexer import HLError

INT64_MAX = 9223372036854775807


def is_list(t):
    return t.startswith("list[")


def list_elem(t):
    return t[5:-1]


def is_map(t):
    return t.startswith("map[str, ")


def map_val(t):
    return t[9:-1]


BUILTIN_FNS = {
    "print", "println", "panic", "exit", "str", "int", "len", "range",
    "map_new", "read_file", "write_file", "args", "clock_ms", "chr",
}
IO_BUILTINS = {"print", "println", "read_file", "write_file", "exit", "args", "clock_ms"}

STR_M = {
    "len": ([], "int"), "byte_at": (["int"], "int"),
    "slice": (["int", "int"], "str"), "find": (["str"], "int"),
    "contains": (["str"], "bool"), "starts_with": (["str"], "bool"),
    "ends_with": (["str"], "bool"), "split": (["str"], "list[str]"),
    "trim": ([], "str"), "to_int": ([], "int"), "to_str": ([], "str"),
    "to_float": ([], "float"),
}
INT_M = {"to_str": "str", "to_float": "float", "abs": "int"}
FLOAT_M = {"to_str": "str", "to_int": "int", "abs": "float"}
BOOL_M = {"to_str": "str"}


class Checker:
    def __init__(self, program):
        self.p = program
        self.structs = program["structs"]
        self.fns = program["fns"]
        self.edges = {}
        self.methods = {}  # struct -> {meth: key}
        self.cur_fn = None

    # ---------- tiện ích ----------
    def err(self, msg, node):
        raise HLError(msg, node.get("line", 0), 0)

    def type_exists(self, t, node):
        if t in ("int", "float", "bool", "str"):
            return True
        if is_list(t):
            return self.type_exists(list_elem(t), node)
        if is_map(t):
            return self.type_exists(map_val(t), node)
        return t in self.structs

    def require_type(self, t, node, what):
        if t == "void":
            self.err("khong the dung 'void' lam %s" % what, node)
        if not self.type_exists(t, node):
            self.err("kieu khong ton tai: %s" % t, node)

    # ---------- vòng đời ----------
    def check(self):
        # 1. gom phương thức theo struct
        for key, fn in self.fns.items():
            if fn["struct"] is not None:
                m = self.methods.setdefault(fn["struct"], {})
                m[fn["name"]] = key
        # 2. kiểm tra khai báo
        for name, st in self.structs.items():
            for fname, ftype in st["fields"]:
                self.require_type(ftype, st, "kieu truong struct")
        for key, fn in self.fns.items():
            if fn["struct"] is None:
                if fn["name"] in BUILTIN_FNS:
                    self.err("khong the dinh nghia lai ham builtin: %s" % fn["name"], fn)
            else:
                if fn["struct"] not in self.structs:
                    self.err("impl cho struct khong ton tai: %s" % fn["struct"], fn)
                if not fn["params"]:
                    self.err("phuong thuc phai co tham so 'self' dau tien", fn)
                sname, stype, _ = fn["params"][0]
                if sname != "self" or stype != fn["struct"]:
                    self.err("phuong thuc phai co tham so dau la 'self: %s'"
                             % fn["struct"], fn)
            for pn, pt, _ in fn["params"]:
                self.require_type(pt, fn, "kieu tham so")
            if fn["ret"] != "void" and not self.type_exists(fn["ret"], fn):
                self.err("kieu tra ve khong ton tai: %s" % fn["ret"], fn)
            self.edges[key] = set()
        if "main" not in self.fns:
            self.err("thieu ham main", {"line": 1})
        mainf = self.fns["main"]
        if mainf["struct"] is not None:
            self.err("main khong duoc la phuong thuc", mainf)
        if mainf["params"]:
            self.err("main khong duoc co tham so", mainf)
        if mainf["ret"] not in ("int", "void"):
            self.err("main phai tra ve 'int' hoac khong co kieu tra ve", mainf)
        # 3. kiểm tra thân hàm
        for key, fn in self.fns.items():
            self.check_fn(key, fn)
        # 4. phân tích hiệu ứng (bất động điểm trên đồ thị lời gọi)
        self.check_effects()

    # ---------- môi trường ----------
    def new_env(self, fn):
        env = [{}]
        if fn["struct"] is not None:
            sname, stype, smut = fn["params"][0]
            env[0][sname] = [stype, smut]
            params = fn["params"][1:]
        else:
            params = fn["params"]
        for pn, pt, pm in params:
            if pn in env[0]:
                self.err("tham so trung ten: %s" % pn, fn)
            env[0][pn] = [pt, pm]
        return env

    def lookup(self, env, name):
        for scope in reversed(env):
            if name in scope:
                return scope[name]
        return None

    def check_fn(self, key, fn):
        self.cur_fn = key
        env = self.new_env(fn)
        self.check_stmts(fn["body"], env, fn, False)
        if fn["ret"] != "void" and not self.all_return(fn["body"]):
            self.err("ham '%s' khong tra ve tren moi duong di" % fn["name"], fn)

    def all_return(self, stmts):
        if not stmts:
            return False
        last = stmts[-1]
        if last["k"] == "return":
            return True
        if last["k"] == "expr" and last["e"].get("t") == "never":
            return True
        if last["k"] == "if" and last["els"] is not None:
            return self.all_return(last["then"]) and self.all_return(last["els"])
        return False

    def check_stmts(self, stmts, env, fn, in_loop):
        for s in stmts:
            self.check_stmt(s, env, fn, in_loop)

    def child(self, env):
        env.append({})
        return env

    # ---------- câu lệnh ----------
    def check_stmt(self, s, env, fn, in_loop):
        k = s["k"]
        if k == "let":
            self.require_type(s["t"], s, "kieu bien")
            if self.lookup(env, s["name"]) is not None:
                self.err("che khuat ten khong duoc phep: %s" % s["name"], s)
            vt = self.check_expr(s["value"], env, s["t"])
            if vt == "never":
                self.err("khong the gan bieu thuc khong bao gio tra ve", s)
            if vt != s["t"]:
                self.err("kieu khong khop: khai bao %s nhung nhan %s"
                         % (s["t"], vt), s)
            env[-1][s["name"]] = [s["t"], s["mut"]]
        elif k == "assign":
            self.check_assign(s, env)
        elif k == "if":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("dieu kien if phai la bool, nhan %s" % ct, s)
            self.child(env)
            self.check_stmts(s["then"], env, fn, in_loop)
            env.pop()
            if s["els"] is not None:
                self.child(env)
                self.check_stmts(s["els"], env, fn, in_loop)
                env.pop()
        elif k == "while":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("dieu kien while phai la bool, nhan %s" % ct, s)
            self.child(env)
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
        elif k == "for":
            it = self.check_expr(s["iter"], env, None)
            if not is_list(it):
                self.err("bieu thuc for-in phai la danh sach, nhan %s" % it, s)
            elem = list_elem(it)
            if s["vtype"] != elem:
                self.err("kieu bien lap %s khong khop phan tu %s"
                         % (s["vtype"], elem), s)
            self.child(env)
            env[-1][s["var"]] = [elem, False]
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
        elif k == "return":
            if fn["ret"] == "void":
                if s["value"] is not None:
                    self.err("ham tra ve void khong duoc return gia tri", s)
            else:
                if s["value"] is None:
                    self.err("ham tra ve %s phai return gia tri" % fn["ret"], s)
                vt = self.check_expr(s["value"], env, fn["ret"])
                if vt != fn["ret"] and vt != "never":
                    self.err("kieu return khong khop: can %s, nhan %s"
                             % (fn["ret"], vt), s)
        elif k == "break":
            if not in_loop:
                self.err("break chi dung trong vong lap", s)
        elif k == "continue":
            if not in_loop:
                self.err("continue chi dung trong vong lap", s)
        elif k == "expr":
            self.check_expr(s["e"], env, None)
        else:
            self.err("cau lenh khong xac dinh: %s" % k, s)

    def check_assign(self, s, env):
        tgt = s["target"]
        # tìm liên kết gốc
        root = tgt
        while root["k"] in ("field", "index"):
            root = root["target"]
        binding = self.lookup(env, root["name"])
        if binding is None:
            self.err("bien khong ton tai: %s" % root["name"], s)
        # 'mut' chỉ quản trị VIỆC GÁN LẠI LIÊN KẾT (name = v).
        # Gán trường / chỉ số là thay đổi NỘI DUNG qua tham chiếu — không cần mut.
        if tgt["k"] == "ident" and not binding[1]:
            self.err("khong the gan lai bien khong kha bien: %s" % root["name"], s)
        tt = self.check_lvalue(tgt, env)
        vt = self.check_expr(s["value"], env, tt)
        if vt == "never":
            self.err("khong the gan bieu thuc khong bao gio tra ve", s)
        if vt != tt:
            self.err("kieu khong khop khi gan: can %s, nhan %s" % (tt, vt), s)

    def check_lvalue(self, e, env):
        if e["k"] == "ident":
            b = self.lookup(env, e["name"])
            return b[0]
        if e["k"] == "field":
            bt = self.check_expr(e["target"], env, None)
            if bt not in self.structs:
                self.err("khong the truy cap truong tren kieu %s" % bt, e)
            for fname, ftype in self.structs[bt]["fields"]:
                if fname == e["name"]:
                    return ftype
            self.err("struct %s khong co truong %s" % (bt, e["name"]), e)
        if e["k"] == "index":
            tt = self.check_expr(e["target"], env, None)
            if not is_list(tt):
                self.err("khong the dung chi so tren kieu %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("chi so phai la int, nhan %s" % it, e)
            return list_elem(tt)
        self.err("ve trai khong hop le", e)

    # ---------- biểu thức ----------
    def check_expr(self, e, env, expected):
        k = e["k"]
        if k == "int":
            if e["v"] > INT64_MAX:
                self.err("so nguyen qua lon (vuot int64)", e)
            e["t"] = "int"
        elif k == "float":
            e["t"] = "float"
        elif k == "bool":
            e["t"] = "bool"
        elif k == "str":
            e["t"] = "str"
        elif k == "ident":
            b = self.lookup(env, e["name"])
            if b is None:
                self.err("bien khong ton tai: %s" % e["name"], e)
            e["t"] = b[0]
        elif k == "bin":
            e["t"] = self.check_bin(e, env)
        elif k == "un":
            vt = self.check_expr(e["e"], env, None)
            if e["op"] == "!":
                if vt != "bool":
                    self.err("toan tu ! can bool, nhan %s" % vt, e)
                e["t"] = "bool"
            else:
                if vt not in ("int", "float"):
                    self.err("toan tu - can int/float, nhan %s" % vt, e)
                e["t"] = vt
        elif k == "index":
            tt = self.check_expr(e["target"], env, None)
            if tt == "never":
                self.err("gia tri khong the dung trong bieu thuc", e)
            if not is_list(tt):
                self.err("khong the dung chi so tren kieu %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("chi so phai la int, nhan %s" % it, e)
            e["t"] = list_elem(tt)
        elif k == "field":
            tt = self.check_expr(e["target"], env, None)
            if tt == "never":
                self.err("gia tri khong the dung trong bieu thuc", e)
            if tt not in self.structs:
                self.err("khong the truy cap truong tren kieu %s" % tt, e)
            e["t"] = None
            for fname, ftype in self.structs[tt]["fields"]:
                if fname == e["name"]:
                    e["t"] = ftype
                    break
            if e["t"] is None:
                self.err("struct %s khong co truong %s" % (tt, e["name"]), e)
        elif k == "call":
            e["t"] = self.check_call(e, env, expected)
        elif k == "method":
            e["t"] = self.check_method(e, env)
        elif k == "listlit":
            e["t"] = self.check_listlit(e, env, expected)
        elif k == "structlit":
            e["t"] = self.check_structlit(e, env)
        elif k == "mapnew":
            if expected is None or not is_map(expected):
                self.err("map_new() can kieu 'map[str, T]' duoc khai bao o ngu canh", e)
            e["t"] = expected
        else:
            self.err("bieu thuc khong xac dinh: %s" % k, e)
        return e["t"]

    def check_bin(self, e, env):
        lt = self.check_expr(e["l"], env, None)
        rt = self.check_expr(e["r"], env, None)
        if lt == "never" or rt == "never":
            self.err("gia tri khong the dung trong bieu thuc", e)
        op = e["op"]
        if op in ("||", "&&"):
            if lt != "bool" or rt != "bool":
                self.err("toan tu %s can bool, nhan %s va %s" % (op, lt, rt), e)
            return "bool"
        if op in ("==", "!="):
            if lt != rt or lt not in ("int", "float", "bool", "str"):
                self.err("khong the so sanh == giua %s va %s" % (lt, rt), e)
            return "bool"
        if op in ("<", "<=", ">", ">="):
            if lt != rt or lt not in ("int", "float", "str"):
                self.err("khong the so sanh thu tu giua %s va %s" % (lt, rt), e)
            return "bool"
        if op == "+":
            if lt == "int" and rt == "int":
                return "int"
            if lt == "float" and rt == "float":
                return "float"
            if lt == "str" and rt == "str":
                return "str"
            self.err("toan tu + khong ho tro %s va %s" % (lt, rt), e)
        if op in ("-", "*", "/", "%"):
            if lt == "int" and rt == "int":
                return "int"
            if lt == "float" and rt == "float":
                return "float"
            self.err("toan tu %s khong ho tro %s va %s" % (op, lt, rt), e)
        self.err("toan tu khong xac dinh: %s" % op, e)

    def check_listlit(self, e, env, expected):
        elem = None
        if expected is not None and is_list(expected):
            elem = list_elem(expected)
        if elem is None:
            if not e["items"]:
                self.err("literal danh sach rong can kieu duoc khai bao o ngu canh", e)
            elem = self.check_expr(e["items"][0], env, None)
            if elem in ("void", "never"):
                self.err("phan tu danh sach khong the co kieu %s" % elem, e)
        for it in e["items"]:
            vt = self.check_expr(it, env, elem)
            if vt != elem:
                self.err("phan tu danh sach khong khop: can %s, nhan %s"
                         % (elem, vt), it)
        return "list[%s]" % elem

    def check_structlit(self, e, env):
        name = e["name"]
        if name not in self.structs:
            self.err("struct khong ton tai: %s" % name, e)
        fields = self.structs[name]["fields"]
        if len(e["fields"]) != len(fields):
            self.err("literal struct %s can dung %d truong, nhan %d"
                     % (name, len(fields), len(e["fields"])), e)
        for (lfname, lfe), (fname, ftype) in zip(e["fields"], fields):
            if lfname != fname:
                self.err("thu tu truong khong dung: mong doi '%s', nhan '%s'"
                         % (fname, lfname), e)
            vt = self.check_expr(lfe, env, ftype)
            if vt != ftype:
                self.err("kiep truong %s khong khop: can %s, nhan %s"
                         % (fname, ftype, vt), e)
        return name

    def check_call(self, e, env, expected):
        name = e["name"]
        args = e["args"]
        if name in BUILTIN_FNS:
            e["rc"] = ("builtin", name)
            return self.check_builtin_call(name, e, env, expected)
        if name in self.fns:
            fn = self.fns[name]
            if fn["struct"] is not None:
                self.err("phuong thuc phai duoc goi qua doi tuong: %s" % name, e)
            e["rc"] = ("user", name)
            self.edges[self.cur_fn].add(name)
            if len(args) != len(fn["params"]):
                self.err("ham %s can %d doi so, nhan %d"
                         % (name, len(fn["params"]), len(args)), e)
            for a, (pn, pt, _) in zip(args, fn["params"]):
                at = self.check_expr(a, env, pt)
                if at != pt:
                    self.err("doi so '%s' cua %s can %s, nhan %s"
                             % (pn, name, pt, at), a)
            return fn["ret"]
        self.err("ham khong ton tai: %s" % name, e)

    def check_builtin_call(self, name, e, env, expected):
        args = e["args"]

        def need(n):
            if len(args) != n:
                self.err("%s can %d doi so, nhan %d" % (name, n, len(args)), e)

        def argt(i, want):
            at = self.check_expr(args[i], env, want if want is not None else None)
            if at == "never":
                self.err("gia tri khong the dung lam doi so", e)
            if want is not None and at != want:
                self.err("%s can doi so %s, nhan %s" % (name, want, at), e)
            return at

        if name in ("print", "println"):
            need(1)
            argt(0, "str")
            self.edges[self.cur_fn].add("b:print")
            return "void"
        if name == "panic":
            need(1)
            argt(0, "str")
            return "never"
        if name == "exit":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:exit")
            return "never"
        if name == "str":
            need(1)
            at = argt(0, None)
            if at not in ("int", "float", "bool", "str"):
                self.err("str() khong ho tro kieu %s" % at, e)
            return "str"
        if name == "int":
            need(1)
            argt(0, "str")
            return "int"
        if name == "len":
            need(1)
            at = argt(0, None)
            if at not in ("str",) and not is_list(at) and not is_map(at):
                self.err("len() khong ho tro kieu %s" % at, e)
            return "int"
        if name == "range":
            need(2)
            argt(0, "int")
            argt(1, "int")
            return "list[int]"
        if name == "map_new":
            need(0)
            if expected is None or not is_map(expected):
                self.err("map_new() can kieu 'map[str, T]' duoc khai bao o ngu canh", e)
            return expected
        if name == "read_file":
            need(1)
            argt(0, "str")
            self.edges[self.cur_fn].add("b:read_file")
            return "str"
        if name == "write_file":
            need(2)
            argt(0, "str")
            argt(1, "str")
            self.edges[self.cur_fn].add("b:write_file")
            return "void"
        if name == "args":
            need(0)
            self.edges[self.cur_fn].add("b:args")
            return "list[str]"
        if name == "chr":
            need(1)
            argt(0, "int")
            return "str"
        if name == "clock_ms":
            need(0)
            self.edges[self.cur_fn].add("b:clock_ms")
            return "int"
        self.err("ham builtin khong xac dinh: %s" % name, e)

    def check_method(self, e, env):
        tt = self.check_expr(e["target"], env, None)
        if tt == "never":
            self.err("gia tri khong the dung trong bieu thuc", e)
        name = e["name"]
        args = e["args"]
        if tt in self.structs:
            m = self.methods.get(tt, {})
            if name not in m:
                self.err("struct %s khong co phuong thuc %s" % (tt, name), e)
            key = m[name]
            e["rm"] = ("user", key)
            fn = self.fns[key]
            params = fn["params"][1:]
            if len(args) != len(params):
                self.err("%s.%s can %d doi so, nhan %d"
                         % (tt, name, len(params), len(args)), e)
            for a, (pn, pt, _) in zip(args, params):
                at = self.check_expr(a, env, pt)
                if at != pt:
                    self.err("doi so '%s' cua %s.%s can %s, nhan %s"
                             % (pn, tt, name, pt, at), a)
            return fn["ret"]
        if tt == "str":
            if name not in STR_M:
                self.err("str khong co phuong thuc %s" % name, e)
            ptypes, ret = STR_M[name]
            e["rm"] = ("builtin", "str." + name)
        elif tt == "int":
            if name not in INT_M:
                self.err("int khong co phuong thuc %s" % name, e)
            ptypes, ret = ([], INT_M[name]) if isinstance(INT_M[name], str) else INT_M[name]
            e["rm"] = ("builtin", "int." + name)
        elif tt == "float":
            if name not in FLOAT_M:
                self.err("float khong co phuong thuc %s" % name, e)
            ptypes, ret = ([], FLOAT_M[name])
            e["rm"] = ("builtin", "float." + name)
        elif tt == "bool":
            if name not in BOOL_M:
                self.err("bool khong co phuong thuc %s" % name, e)
            ptypes, ret = ([], BOOL_M[name])
            e["rm"] = ("builtin", "bool." + name)
        elif is_list(tt):
            elem = list_elem(tt)
            tbl = {
                "len": ([], "int"),
                "push": ([elem], "void"),
                "get": (["int"], elem),
                "set": (["int", elem], "void"),
                "pop": ([], elem),
            }
            if name not in tbl:
                self.err("list khong co phuong thuc %s" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "list." + name)
        elif is_map(tt):
            vt = map_val(tt)
            tbl = {
                "len": ([], "int"),
                "set": (["str", vt], "void"),
                "get_or": (["str", vt], vt),
                "has": (["str"], "bool"),
                "keys": ([], "list[str]"),
            }
            if name not in tbl:
                self.err("map khong co phuong thuc %s" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "map." + name)
        else:
            self.err("khong the goi phuong thuc tren kieu %s" % tt, e)
        if len(args) != len(ptypes):
            self.err("%s.%s can %d doi so, nhan %d"
                     % (tt, name, len(ptypes), len(args)), e)
        for a, pt in zip(args, ptypes):
            at = self.check_expr(a, env, pt)
            if at != pt:
                self.err("doi so cua %s.%s can %s, nhan %s" % (tt, name, pt, at), a)
        return ret

    # ---------- hiệu ứng ----------
    def check_effects(self):
        eff = {}
        for key, fn in self.fns.items():
            eff[key] = fn["uses_io"]
        changed = True
        while changed:
            changed = False
            for key, callees in self.edges.items():
                if eff[key]:
                    continue
                for c in callees:
                    if c.startswith("b:"):
                        eff[key] = True
                        changed = True
                        break
                    if eff.get(c):
                        eff[key] = True
                        changed = True
                        break
        for key, fn in self.fns.items():
            if eff[key] and not fn["uses_io"]:
                for c in self.edges.get(key, ()):  # tìm một cạnh vi phạm để báo cáo
                    if c.startswith("b:") or eff.get(c):
                        self.err("ham '%s' goi '%s' (IO) nhung khong khai bao 'uses IO'"
                                 % (fn["name"], c[2:] if c.startswith("b:") else c), fn)


def check(program):
    Checker(program).check()
