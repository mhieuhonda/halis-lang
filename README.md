<div align="center">

# Hieu Louis

**Ngôn ngữ lập trình bảo mật cao — tự dịch — biên dịch native**

`hlc` được viết 100% bằng chính Hieu Louis. Trình biên dịch tự dịch chính mình,
hai lần sinh mã **giống hệt từng byte**.

[Đặc tả](SPEC.md) · [Lộ trình 20 giai đoạn](ROADMAP.md) · [Bảo mật](SECURITY.md)

</div>

---

## Tại sao lại có Hieu Louis?

Hieu Louis (HLS) ra đời từ một niềm tin: **an toàn không phải là tuỳ chọn, và hiệu
năng không phải cái giá của an toàn**.

```hls
# Ham khong co 'uses IO' duoc BAO DAM thuan tuy boi trinh bien dich
fn tinh_tong(n: int) -> int {
    let mut t: int = 0
    let i: int = 0
    while i < n {
        t = t + i * i
        i = i + 1
    }
    return t
}

fn main() -> int uses IO {
    println("tong = " + tinh_tong(100).to_str())
    return 0
}
```

Ba lời bảo đảm cốt lõi của v0.1:

1. **I/O là hiệu ứng phải khai báo.** Quên `uses IO` mà in ra màn hình? Lỗi biên
   dịch — kể cả khi lời gọi gián tiếp qua 5 tầng hàm.
2. **Mọi phép toán đều được kiểm tra.** Tràn số nguyên, chia 0, truy cập mảng
   ngoài phạm vi — đều dừng an toàn, không có undefined behavior. Không tồn tại
   chế độ tắt kiểm tra trong v0.1.
3. **Không có null.** Không có biến chưa khởi tạo, không có trạng thái ẩn, không
   có biến toàn cục. Mọi thứ tường minh để kiểm toán được.

## Tự dịch (self-hosting) — bằng chứng, không phải lời hứa

```
                    ┌────────────────────────────────────────────┐
                    │                                            │
  src/hlc.hls ──► boot/ (Stage-0, hạt giống) ──► hlc.c (lần 1)  │
  (compiler bằng      chỉ dùng MỘT LẦN để        │               │
   HLS, ~3000 dòng)   khởi động chu trình         ▼               │
                                           gcc -O2               │
                                                │                 │
                                                ▼                 │
                                        bin/hlc  (native) ───────┤
                                                │   tự dịch lại   │
                                                ▼                 │
                                        hlc.c (lần 2)             │
                                                │                 │
                                 diff lần 1 và lần 2 = 0 byte ───┘
```

`make bootstrap` thực hiện toàn bộ chuỗi trên và xác nhận tính **xác định**
(deterministic) của quá trình tự dịch. Từ Giai đoạn 5 trở đi, ngôn ngữ phát triển
bằng chính nó.

## Bắt đầu nhanh

Yêu cầu: Python 3.8+ (chỉ cho hạt giống Stage-0), gcc hoặc clang.

```bash
# 1. Chạy ngay qua Stage-0 (thông dịch)
python3 boot/boot.py examples/hello.hls

# 2. Dựng trình biên dịch native bằng chuỗi bootstrap
make bootstrap
#    → bin/hlc  (trình biên dịch HLS viết bằng HLS, biên dịch native)

# 3. Biên dịch chương trình của bạn thành binary native
make run F=examples/primes.hls

# 4. Chạy toàn bộ bộ kiểm thử (56 test: kiểu, effects, vi sai, bootstrap)
make test
```

## Ví dụ ngôn ngữ

```hls
# Struct + phương thức — struct mang ngữ nghĩa tham chiếu
struct Diem {
    x: int,
    y: int
}

impl Diem {
    fn dist2(self: Diem) -> int {
        return self.x * self.x + self.y * self.y
    }
    fn dich(mut self: Diem, dx: int) -> void {
        self.x = self.x + dx
    }
}

fn main() -> int uses IO {
    let p: Diem = Diem { x: 3, y: 4 }
    p.dich(1)
    println("dist2 = " + p.dist2().to_str())     # dist2 = 32

    # Danh sách + map (giữ thứ tự chèn)
    let tu_dien: map[str, int] = map_new()
    tu_dien.set("hieu", 1)
    tu_dien.set("louis", 2)

    # Chuỗi là chuỗi byte, đầy đủ thao tác
    let s: str = "  Hieu Louis  "
    println("[" + s.trim() + "]")

    # Vòng lặp for-in: độ dài chụp một lần
    for i: int in range(0, 5) {
        print(i.to_str() + " ")
    }
    println("")
    return 0
}
```

Xem thêm: [examples/](examples/) — gồm `secure_demo.hls` minh hoạ panic an toàn
khi tràn số, và `wordcount.hls` đọc tệp thật.

## Kiến trúc kho mã

```
hieu-louis-lang/
├── SPEC.md              # Hiến pháp ngôn ngữ (đặc tả đầy đủ v0.1)
├── ROADMAP.md           # Lộ trình 20 giai đoạn tới v1.0
├── SECURITY.md          # Mô hình đe doạ & chính sách bảo mật
├── boot/                # Stage-0: hạt giống bootstrap (Python thuần, ~1.400 dòng)
│   ├── lexer.py         #   từ vựng
│   ├── parser.py        #   cú pháp → AST
│   ├── checker.py       #   kiểm tra kiểu + phân tích effects
│   ├── interp.py        #   evaluator (ngữ nghĩa tham chiếu)
│   └── boot.py          #   CLI
├── src/
│   └── hlc.hls          # ★ TRÌNH BIÊN DỊCH viết 100% bằng HLS (~3.000 dòng)
│                        #   lexer → parser → checker → codegen C → tự dịch
├── examples/            # hello, fibonacci, primes, wordcount, secure_demo
├── tests/
│   ├── ok/              #   14 chương trình hợp lệ (kèm panic an toàn)
│   ├── fail/            #   22 chương trình PHẢI bị từ chối (kiểu/effects)
│   ├── snapshots/       #   kết quả kỳ vọng
│   └── run_tests.sh     #   56 kiểm thử: ok/fail/vi sai/bootstrap fixed-point
├── Makefile             # bootstrap · test · run · examples
└── bin/                 # (sinh ra) hlc native
```

## Triết lý thiết kế (rút gọn)

| Nguyên tắc | Thể hiện |
|------------|----------|
| An toàn là mặc định | Số học kiểm tra, biên mảng kiểm tra — không có chế độ tắt |
| Tường minh để kiểm toán | Kiểu bắt buộc, no shadowing, không ép kiểu ngầm, không trạng thái ẩn |
| I/O là hiệu ứng | `uses IO` kiểm chứng tĩnh, bất động điểm trên đồ thị lời gọi |
| Không null | Không tồn tại tham chiếu rỗng hay biến chưa khởi tạo |
| Hiệu năng bằng AOT | HLS → C → mã máy; generics tương lai sẽ đơn hình hoá |
| Hạt nhân nhỏ | Mọi thứ khác mở rộng bằng thư viện chuẩn, không phình cú pháp |

Chi tiết đầy đủ: [SPEC.md](SPEC.md) · Lộ trình chi tiết từng giai đoạn:
[ROADMAP.md](ROADMAP.md).

## Trạng thái

**v0.1.0 — hoàn thành Giai đoạn 1–5/20** (xem ROADMAP):

- ✅ Đặc tả hạt nhân hoàn chỉnh
- ✅ Stage-0 tham chiếu (thông dịch, có kiểm tra kiểu + effects)
- ✅ Trình biên dịch tự viết `hlc.hls` (front-end + backend C)
- ✅ Tự dịch fixed-point xác định; 56/56 kiểm thử PASS
- ⬜ Module/thư viện chuẩn, enum/generics, ownership, LLVM, concurrency...

## Đóng góp

Mọi đóng góp phải giữ nguyên ba lời bảo đảm cốt lõi và vượt qua `make test`
(56 kiểm thử, gồm kiểm thử vi sai hai bản triển khai). Mọi tính năng mới trước tiên
phải được dùng trong chính `hlc` — trình biên dịch luôn là khách hàng đầu tiên
của ngôn ngữ.

## Giấy phép

[MIT](LICENSE) © 2026 mhieuhonda
