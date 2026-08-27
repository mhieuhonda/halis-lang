# Đặc tả ngôn ngữ Hieu Louis (HLS) — v0.1.0

> **Hieu Louis** là ngôn ngữ lập trình bảo mật cao, biên dịchnative, được thiết kế theo
> triết lý: **an toàn tuyệt đối theo mặc định, tường minh để kiểm toán, hiệu năng bằng
> biên dịch AOT**. Phiên bản v0.1.0 là hạt nhân tối thiểu nhưng **hoàn chỉnh và chạy thật**:
> mọi phép toán đều được kiểm tra, mọi I/O đều bị theo dõi tĩnh, không có null, không có
> hành vi không xác định (undefined behavior).

- Tệp nguồn: `*.hls`
- Trình biên dịch tự viết: `src/hlc.hls` (HLS → C → native)
- Hạt giống bootstrap: `boot/` (Stage-0, dùng để khởi động chu trình tự dịch)
- Quy ước phiên bản: `MAJOR.MINOR.PATCH`, ngôn ngữ đóng băng khi đạt v1.0 (xem `ROADMAP.md`)

---

## 1. Triết lý thiết kế

1. **An toàn là mặc định, không phải tuỳ chọn.** Số học kiểm tra tràn, mảng kiểm tra
   biên, chia cho 0 dừng an toàn. Trong v0.1 **không tồn tại chế độ tắt kiểm tra** —
   chế độ "nhanh không kiểm tra" chỉ được mở khoá bằng chứng minh hình thức (Giai đoạn 17).
2. **Tường minh để kiểm toán được.** Mọi biến phải khai báo kiểu. Không có kiểu suy luận
   ngầm, không có ép kiểu ngầm, không có biến toàn cục ẩn, không có trạng thái ẩn.
   Một người kiểm toán có thể đọc từng dòng và biết chính xác điều gì xảy ra.
3. **I/O là hiệu ứng, hiệu ứng phải khai báo.** Chỉ cần đọc một dòng lệnh `fn` không có
   `uses IO` thì trình biên dịch **bảo đảm** (bằng phân tích tĩnh) hàm đó thuần tuý —
   không ghi đĩa, không in ra, không đọc mạng môi trường.
4. **Không có null.** Không tồn tại tham chiếu rỗng. Vùng dữ liệu chưa khởi tạo không
   tồn tại (không khai báo biến mà không gán).
5. **Hiệu năng bằng biên dịch AOT.** HLS biên dịch sang C rồi sang mã máy native.
   Không máy ảo, không GC trong hạt nhân v0.1 (mô hình bộ nhớ: xem mục 11).

---

## 2. Quy tắc từ vựng (Lexing)

### 2.1. Mã nguồn
- Tệp nguồn là chuỗi byte UTF-8. Bộ từ vựng của v0.1 hoạt động trên **byte**.
- Chuỗi trong HLS v0.1 là **chuỗi byte** (byte string); API Unicode đầy đủ nằm ở Giai đoạn 6.

### 2.2. Ký tự trắng & dòng mới
- Khoảng trắng, tab, CR, LF đều là ký tự trắng. **Dòng mới không có ý nghĩa ngữ pháp.**
- Quy tắc chống mơ hồ: một *câu lệnh mới* không được bắt đầu bằng `(`, `[`, `.` hoặc
  một toán tử. Nếu gặp các ký hiệu đó ở vị trí bắt đầu câu lệnh, bộ phân tích coi chúng
  là phần tiếp tục của biểu thức phía trước. Vì vậy các câu lệnh dạng
  `(x);` `[1,2];` `-x;` luôn là lỗi cú pháp (chúng vô nghĩa).

### 2.3. Ghi chú (comment)
- `#` đến hết dòng. Không có ghi chú khối trong v0.1.

### 2.4. Định danh
- `[A-Za-z_][A-Za-z0-9_]*`. Quy ước: hàm/biến `snake_case`, struct `PascalCase`.
- Không được trùng từ khoá. Từ khoá (17): `fn let mut return if else while for in
  break continue struct impl import uses true false`.

### 2.5. Từ khoá dự kiến (chưa dùng, báo lỗi nếu gặp): `import`, `secure`, `match`, `enum`, `trait`

### 2.6. Số
- Số nguyên: `[0-9][0-9_]*` (gạch dưới cho dễ đọc: `1_000_000`). Kiểu `int` — bù hai,
  64 bit có dấu. Giới hạn: −9 223 372 036 854 775 808 … 9 223 372 036 854 775 807.
  Hằng `-9223372036854775808` (INT64_MIN) là hợp lệ (bộ phân tích gộp dấu trừ);
  hằng dương vượt INT64_MAX là lỗi biên dịch.
- Số thực: `[0-9][0-9_]* . [0-9][0-9_]*` (bắt buộc có chữ số hai bên dấu chấm).
  Kiểu `float` — IEEE 754 dual (64 bit). Không có ký hiệu khoa học trong v0.1.

### 2.7. Chuỗi
- `"..."`, thoát: `\n` `\t` `\\` `\"`. Escape khác là lỗi cú pháp.
- Chuỗi thô chứa dấu xuống dòng là lỗi. Chuỗi rỗng `""` hợp lệ.

### 2.8. Toán tử & ký hiệu
```
->  ==  !=  <=  >=  <  >  =  +  -  *  /  %  !  &&  ||
(  )  {  }  [  ]  ,  :  .
```
`&` và `|` đơn lẻ là lỗi từ vựng. Không có toán tử bit trong v0.1 (mỗi toán tử bit sẽ
được đưa vào với ngữ nghĩa kiểm tra tràn riêng — Giai đoạn 7).

---

## 3. Kiểu dữ liệu

| Kiểu      | Ý nghĩa                                   | Biểu diễn C (backend) |
|-----------|-------------------------------------------|-----------------------|
| `int`     | số nguyên 64 bit có dấu, **kiểm tra tràn**| `int64_t`             |
| `float`   | số thực 64 bit IEEE 754                   | `double`              |
| `bool`    | `true` / `false`                          | `bool`                |
| `str`     | chuỗi byte, độ dài tường minh             | `hl_str*`             |
| `list[T]` | mảng động các phần tử kiểu `T`            | `hl_list*`            |
| `map[str, T]` | bảng băm khoá là `str`, giá trị `T`  | `hl_map*`             |
| `void`    | chỉ dùng làm kiểu trả về (rỗng)           | —                     |
| `Name`    | struct do người dùng định nghĩa (tham chiếu) | `Name*`            |

Quy tắc:
- **Không có ép kiểu ngầm.** `int → float` phải dùng `x.to_float()`, ngược lại `x.to_int()`.
- Kiểu struct mang **ngữ nghĩa tham chiếu** (như con trỏ có kiểm tra); gán struct là
  gán tham chiếu. Ngữ dịch chuyển (move) & sở hữu xuất hiện ở Giai đoạn 8.
- `map` trong v0.1 chỉ có khoá `str` (hỗ trợ khoá tổng quát: Giai đoạn 7).
- So sánh `==`/`!=` chỉ áp dụng cho: `int`, `float`, `bool`, `str`. `list`, `map`,
  struct **không** so sánh được bằng `==` trong v0.1.
- So sánh thứ tự `< <= > >=` áp dụng cho: `int`, `float` (số học) và `str` (theo byte,
  như `memcmp`).

---

## 4. Cấu trúc chương trình

Một tệp `.hls` là chuỗi các khai báo **top-level** (thứ tự tuỳ ý, tham chiếu chéo được):

```
program     := (structdef | impl | fndef)*
structdef   := "struct" Ident "{" field ("," field)* ","? "}"
field       := Ident ":" type
impl        := "impl" Ident "{" fndef* "}"
fndef       := "fn" Ident "(" params? ")" ("->" type)? ("uses" "IO")? block
params      := param ("," param)* ","?
param       := "mut"? Ident ":" type
type        := "int" | "float" | "bool" | "str" | "void"
             | "list" "[" type "]"
             | "map" "[" "str" "," type "]"
             | Ident                      # tên struct
block       := "{" stmt* "}"
```

- **Hàm `main`** bắt buộc: `fn main() -> int` hoặc `fn main()`; không có tham số.
  Giá trị trả về là mã thoát của tiến trình; `main` rỗng trả về 0.
- Không có biến toàn cục, không có hằng toàn cục, không có import trong v0.1
  (mọi hàm builtin sẵn có; hệ thống module: Giai đoạn 6).
- Tên trùng lặp (hàm–hàm, struct–struct, phương thức trùng trong một `impl`) là lỗi.
- Struct phải có ít nhất 1 trường. Trường struct không được có giá trị mặc định (v0.1);
  khởi tạo struct luôn liệt kê đủ mọi trường.

---

## 5. Câu lệnh (statement)

```
stmt := let | assign | "if" ... | "while" ... | "for" ... | "return" ...
      | "break" | "continue" | callstmt

let     := "let" "mut"? Ident ":" type "=" expr
assign  := lvalue "=" expr
lvalue  := Ident (("." Ident) | ("[" expr "]"))*
if      := "if" expr block ("else" (if | block))?
while   := "while" expr block
for     := "for" Ident ":" type "in" expr block
return  := "return" expr?
callstmt:= call-expression          # câu lệnh biểu thức phải là lời gọi hàm/phương thức
```

Quy tắc:
- `let` khai báo một liên kết (binding). **`mut` quản trị việc GÁN LẠI LIÊN KẾT**: chỉ có
  `let mut x` (hoặc tham số `mut x`) mới được viết `x = giá_trị_mới`.
- Gán trường (`p.x = v`) và gán chỉ số (`xs[i] = v`) là thay đổi **nội dung** của dữ liệu
  qua tham chiếu — được phép trên mọi liên kết (nhất quán với `xs.push(v)`).
- **Cấm che khuất tên (no shadowing):** khai báo tên đã nhìn thấy ở phạm vi bao là lỗi.
  Các phạm vi anh em (hai vòng lặp khác nhau cùng đặt tên `i`) thì hợp lệ.
- `if`/`while`: điều kiện **phải là `bool`** — không có "truthiness". Trong vị trí điều
  kiện và biểu thức lặp `for` (vị trí đứng ngay trước khối `{`), literal struct phải bọc
  trong cặp ngoặc để tách khỏi khối lệnh.
- `for x: T in expr`: `expr` phải là `list[T]`. Độ dài danh sách được **chụp lại một lần**
  khi vào vòng lặp; phần tử thêm vào trong lúc lặp không được duyệt. Biến lặp `x` là
  bất biến và chỉ tồn tại trong thân vòng lặp.
- `return` không giá trị chỉ dùng trong hàm trả về `void`. Với hàm có kiểu trả về,
  **mọi đường đi đều phải return** (phân tích luồng bảo thủ; `while` không được tính là
  đường return).
- `break`/`continue` chỉ hợp lệ trong thân vòng lặp.
- Câu lệnh biểu thức phải là **lời gọi** (hàm hoặc phương thức). `x + 1;` là lỗi
  "biểu thức không có tác dụng".

---

## 6. Biểu thức & độ ưu tiên

Từ thấp đến cao:

| Độ ưu tiên | Toán tử            | Ghi chú |
|-----------|--------------------|---------|
| 1 (thấp)  | `\|\|`             | ngắn mạch |
| 2         | `&&`               | ngắn mạch |
| 3         | `==` `!=`          | theo kiểu (mục 3) |
| 4         | `<` `<=` `>` `>=`  | int/float/str |
| 5         | `+` `-`            | `+` với str là nối chuỗi |
| 6         | `*` `/` `%`        | int: kiểm tra; float: IEEE |
| 7         | `!` `-` (đơn)     | phủ định / đảo dấu kiểm tra |
| 8 (cao)   | `.` `[` `(`        | hậu tố: trường, chỉ số, lời gọi |

Toán hạng:
- Literal: `int`, `float`, `true`, `false`, `str`.
- `Ident` — biến/tham số (kiểu từ khai báo).
- `(expr)` — nhóm.
- `[e1, e2, ...]` — literal danh sách. Kiểu phần tử suy từ ngữ cảnh (kiểu khai báo ở
  `let`, kiểu tham số, kiểu trả về, phần tử của literal bao quanh). Literal rỗng `[]`
  bắt buộc phải có ngữ cảnh kiểu. Tất cả phần tử phải cùng kiểu chính xác.
- `Name { f1: e1, f2: e2, ... }` — literal struct: **đầy đủ mọi trường, đúng thứ tự
  khai báo** (tên trường viết tường minh để dễ kiểm toán). Chỉ dùng được ở nơi ngữ cảnh
  cho phép (không dùng trực tiếp làm điều kiện `if`/`while`).
- Lời gọi hàm `f(a, b)`, lời gọi phương thức `x.m(a, b)`, truy cập trường `x.f`,
  truy cập chỉ số `xs[i]` (chỉ `list`; `i` phải là `int`, kiểm tra biên khi chạy).
- Toán hạng của `&&`/`||` phải là `bool`.

---

## 7. Ngữ nghĩa số học — "mọi phép toán đều được kiểm tra"

| Phép toán | Ngữ nghĩa |
|-----------|-----------|
| `a + b` (int) | cộng 64-bit; tràn → `panic "tran so nguyen"` |
| `a - b`, `a * b` (int) | tương tự — tràn là panic |
| `-a` (int) | `-INT64_MIN` là panic |
| `a / b` (int) | `b == 0` → panic; `INT64_MIN / -1` → panic (tràn) |
| `a % b` (int) | dấu phần dư theo **số bị chia** (như C); kiểm tra như phép chia |
| `a / b` (float) | IEEE 754 (chia 0 cho `inf`/`nan` — không panic) |
| `xs[i]` | `0 <= i < len` — ngoài phạm vi → `panic "truy cap mang ngoai pham vi"` |
| `s.byte_at(i)` | kiểm tra biên như trên |
| `s.slice(a, b)` | yêu cầu `0 <= a <= b <= len` — vi phạm là panic |

Số học `float` theo IEEE 754, không có kiểm tra. In số float dùng định dạng `%.6f`.

---

## 8. Hàm builtin (mức toàn cục)

| Hàm | Kiểu | Hiệu ứng | Ghi chú |
|-----|------|----------|---------|
| `print(s: str)` | `void` | IO | in không xuống dòng |
| `println(s: str)` | `void` | IO | in có xuống dòng |
| `panic(msg: str)` | không trả về | — | dừng chương trình, mã 101 |
| `exit(code: int)` | không trả về | IO | thoát với mã `code` |
| `str(x)` | `str` | — | `x ∈ {int, float, bool, str}` |
| `int(s: str)` | `int` | — | lỗi nếu chuỗi không phải số nguyên hợp lệ |
| `len(x)` | `int` | — | `str` (số byte), `list`, `map` |
| `range(a: int, b: int)` | `list[int]` | — | `[a, b)` — `a >= b` → rỗng |
| `map_new()` | `map[str, T]` | — | kiểu `T` lấy từ ngữ cảnh khai báo |
| `read_file(path: str)` | `str` | IO | đọc toàn bộ tệp; lỗi I/O → panic |
| `write_file(path: str, content: str)` | `void` | IO | ghi toàn bộ tệp; lỗi → panic |
| `args()` | `list[str]` | IO | tham số dòng lệnh; `args()[0]` là chương trình |
| `clock_ms()` | `int` | IO | mili-giây (đồng hồ đơn điệu) |
| `chr(i: int)` | `str` | — | chuỗi 1 byte; `i` ngoài 0..255 → panic |

`int(s)`: cho phép dấu trừ ở đầu, chỉ chấp nhận chữ số 0–9, giá trị phải nằm trong
phạm vi `int` 64-bit, ngược lại panic "khong the doi chuoi thanh int".

## 8b. Phương thức builtin

**str:** `len() -> int`, `byte_at(i: int) -> int`, `slice(a: int, b: int) -> str`,
`find(sub: str) -> int` (−1 nếu không thấy), `contains(sub: str) -> bool`,
`starts_with(p: str) -> bool`, `ends_with(p: str) -> bool`,
`split(sep: str) -> list[str]` (sep rỗng → panic), `trim() -> str` (cắt byte ≤ 0x20 hai đầu),
`to_int() -> int`, `to_float() -> float` (chuỗi không hợp lệ → panic), `to_str() -> str`.

**int:** `to_str() -> str`, `to_float() -> float`, `abs() -> int` (`abs(INT64_MIN)` → panic).

**float:** `to_str() -> str` (`%.6f`), `to_int() -> int` (cắt về 0), `abs() -> float`.

**bool:** `to_str() -> str`.

**list[T]:** `len() -> int`, `push(v: T)`, `get(i: int) -> T` (kiểm tra biên),
`set(i: int, v: T)`, `pop() -> T` (rỗng → panic).

**map[str, T]:** `len() -> int`, `set(k: str, v: T)`, `get_or(k: str, dflt: T) -> T`,
`has(k: str) -> bool`, `keys() -> list[str]` (**thứ tự chèn**).

**struct:** phương thức do người dùng định nghĩa qua `impl`. Phương thức bắt buộc có
tham số đầu tên `self` (kiểu struct đó): `fn get_x(self: Point) -> int { ... }`.
Muốn thay đổi trường: khai báo `mut self: Point`.

---

## 9. Hệ thống hiệu ứng (effects) — trái tim bảo mật của v0.1

- Hiệu ứng duy nhất trong v0.1: **IO** (in/đọc/ghi tệp, tham số dòng lệnh, thoát, đồng hồ).
- Một hàm gọi (trực tiếp hoặc gián tiếp qua chuỗi lời gọi tĩnh) bất kỳ hàm/Phương thức
  builtin nào mang hiệu ứng IO **phải khai báo** `uses IO`.
- Phân tích là **bất động điểm trên đồ thị lời gọi tĩnh** (mọi lời gọi đều tĩnh trong v0.1).
- Vi phạm → lỗi biên dịch, chỉ rõ hàm và chuỗi lời gọi vi phạm.
- Hệ quả: mọi hàm không có `uses IO` được **bảo đảm thuần tuý** (không thể có tác dụng
  I/O). Đây là cơ sở cho tối ưu hoá & kiểm chứng ở các giai đoạn sau.

Ví dụ:

```hls
fn doi(x: int) -> int {          # THUẦN TUÝ — bảo đảm bởi trình biên dịch
    return x * 2
}

fn chao(ten: str) -> int uses IO {
    println("Xin chao " + ten)   # IO phải được khai báo
    return 0
}
```

---

## 10. Mô hình bộ nhớ v0.1 (trung thực & có chủ đích)

- v0.1 dùng **cấp phát vùng (arena)**: mọi chuỗi/danh sách/bản đồ/struct được cấp phát
  và **không thu hồi** trong thời gian sống của tiến trình. Chương trình ngắn (CLI,
  trình biên dịch) không bao giờ gặp vấn đề.
- Đây là quyết định có chủ đích để hạt nhân v0.1 nhỏ, kiểm chứng được, không có
  use-after-free, không có double-free **về mặt cấu trúc** (không có `free`!).
- Sở hữu/borrow-checker và thu hồi bộ nhớ chính xác: **Giai đoạn 8** của ROADMAP.
- Đệ quy sâu: v0.1 chưa có kiểm tra tràn stack (Giai đoạn 11).

## 11. Lỗi & panic

- Lỗi biên dịch (kiểu, hiệu ứng, cú pháp): dừng ở thời điểm biên dịch, có số dòng.
- `panic(msg)`: in `panic: <msg>` (kèm vị trí khi chạy trên Stage-0) ra stderr,
  thoát mã **101**. v0.1 không có cơ chế bắt panic (bắt lỗi có kiểm soát: `Result`
  ở Giai đoạn 7).

## 12. Những gì v0.1 cố ý KHÔNG có

| Tính năng | Giai đoạn |
|-----------|-----------|
| `enum`, `Option`, `Result`, generics, suy luận kiểu | 7 |
| Toán tử bit (`&` `\|` `^` `<<` `>>`) với ngữ nghĩa kiểm tra | 7 |
| Sở hữu (ownership) & borrow checking | 8 |
| Effects chi tiết (`Net`, `Fs`, `Clock`, `Rand`), capability, taint | 9–10 |
| IR SSA + tối ưu hoá | 11 |
| Backend LLVM trực tiếp | 12 |
| Hệ thống module/import, thư viện chuẩn dạng gói | 6 |
| `match`, closure, con trỏ hàm, async | 7, 16 |
| Bắt panic / `Result` | 7 |

---

## 13. Chương trình mẫu đầy đủ

```hls
# primes.hls — sàng Eratosthenes, thể hiện kiểu, vòng lặp, danh sách
fn sang(n: int) -> list[int] {
    let flags: list[bool] = []
    let i: int = 0
    while i < n {
        flags.push(i >= 2)
        i = i + 1
    }
    let kq: list[int] = []
    let p: int = 2
    while p < n {
        if flags.get(p) {
            kq.push(p)
            let boi: int = p * p
            while boi < n {
                flags.set(boi, false)
                boi = boi + p
            }
        }
        p = p + 1
    }
    return kq
}

fn main() -> int uses IO {
    let cac_so: list[int] = sang(100)
    let i: int = 0
    while i < cac_so.len() {
        print(cac_so.get(i).to_str() + " ")
        i = i + 1
    }
    println("")
    return 0
}
```

## 14. Bảng tương thích ngữ nghĩa Stage-0 vs native

Hai bản triển khai (trình thông dịch tham chiếu `boot/` và trình biên dịch tự viết
`src/hlc.hls`) phải cho **kết quả đầu ra giống hệt nhau** trên cùng một chương trình
(kiểm thử vi sai — differential testing, xem `tests/run_tests.sh`). Điểm khác biệt
duy nhất được phép: thông báo panic trên Stage-0 kèm vị trí dòng, còn bản native thì
không (thông tin gỡ lỗi: Giai đoạn 11).
