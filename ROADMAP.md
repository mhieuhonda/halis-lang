# ROADMAP — Hieu Louis (HLS)

> Lộ trình 20 giai đoạn đưa Hieu Louis từ hạt nhân tự dịch v0.1 thành một ngôn ngữ
> lập trình **bảo mật cực mạnh, hiệu năng cao, hoàn chỉnh v1.0** — toàn bộ chuỗi công cụ
> được viết bằng chính Hieu Louis.

**Chú giải trạng thái:** ✅ hoàn thành · 🔄 đang thực hiện · ⬜ chưa bắt đầu
**Nguyên tắc xuyên suốt:** mỗi giai đoạn chỉ được đóng khi đạt **100% tiêu chí nghiệm thu**
và bộ kiểm thử vi sai (thông dịch ↔ native) vẫn xanh.

---

## TỔNG QUAN

| # | Giai đoạn | Trạng thái | Thời lượng ước tính |
|---|-----------|:----------:|:-------------------:|
| 1 | Đặc tả & thiết kế hạt nhân | ✅ | (xong) |
| 2 | Hạt giống bootstrap Stage-0 | ✅ | (xong) |
| 3 | Trình biên dịch tự viết — front-end | ✅ | (xong) |
| 4 | Backend HLS → C + runtime C | ✅ | (xong) |
| 5 | Tự dịch hoàn toàn (fixed-point) | ✅ | (xong) |
| 6 | Hệ thống module & thư viện chuẩn | ⬜ | 6–8 tuần |
| 7 | Hệ thống kiểu nâng cao: enum, Option/Result, generics | ⬜ | 8–12 tuần |
| 8 | Sở hữu & borrow checking (kết thúc arena) | ⬜ | 10–14 tuần |
| 9 | Hệ thống effects chi tiết + capability | ⬜ | 6–8 tuần |
| 10 | Taint tracking & sandbox | ⬜ | 8–10 tuần |
| 11 | IR SSA + tối ưu hoá | ⬜ | 10–14 tuần |
| 12 | Backend LLVM native | ⬜ | 10–14 tuần |
| 13 | Trình quản lý gói `hls-pkg` | ⬜ | 6–8 tuần |
| 14 | Bộ công cụ: LSP, formatter, linter | ⬜ | 6–8 tuần |
| 15 | FFI an toàn với C | ⬜ | 4–6 tuần |
| 16 | Đồng thời & async (data-race freedom) | ⬜ | 12–16 tuần |
| 17 | Kiểm chứng hình thức & hợp đồng | ⬜ | 10–14 tuần |
| 18 | Hệ sinh thái kiểm thử & fuzzing | ⬜ | 4–6 tuần |
| 19 | Tài liệu, sách, playground | ⬜ | 6 tuần |
| 20 | HLS v1.0 — đóng băng API, LTS, bootstrap thuần HLS | ⬜ | 4 tuần |

Tổng thời lượng dự kiến: ~24–30 tháng (đội nhỏ 2–4 người toàn thời gian).

---

## GIAI ĐOẠN 1 — Đặc tả & thiết kế hạt nhân ✅

**Mục tiêu:** định hình triết lý và "hiến pháp" của ngôn ngữ.

**Công việc:**
- Triết lý: an toàn mặc định, tường minh để kiểm toán, I/O là hiệu ứng, không null,
  hiệu năng bằng AOT.
- Đặc tả đầy đủ v0.1: từ vựng, kiểu, câu lệnh, biểu thức, builtin, effects, ngữ nghĩa
  kiểm tra, mô hình bộ nhớ, mô hình lỗi (`SPEC.md`).
- Quyết định có chủ đích những gì v0.1 KHÔNG có (enum, generics, ownership...) để hạt
  nhân nhỏ và kiểm chứng được.

**Tiêu chí nghiệm thu:** đặc tả đủ chặt để hai bản triển khai độc lập (thông dịch &
biên dịch) cho cùng kết quả trên mọi chương trình.

**Kết quả:** `SPEC.md` v0.1.1 hoàn chỉnh.

## GIAI ĐOẠN 2 — Hạt giống bootstrap Stage-0 ✅

**Mục tiêu:** trình thông dịch tham chiếu chạy được HLS ngay, để làm mốc so sánh ngữ nghĩa.

**Công việc:**
- `boot/`: lexer → parser → kiểm tra kiểu → phân tích effects → evaluator (~1.400 dòng
  Python thuần, không phụ thuộc ngoài).
- Ngữ nghĩa byte-chính-xác: chuỗi là byte, số học int64 kiểm tra tràn, chia 0 dừng an
  toàn, map giữ thứ tự chèn, `%.6f` cho float.
- CLI: `boot.py [--check] file.hls [args...]`.

**Tiêu chí nghiệm thu:** chạy được tập kiểm thử ok/fail với thông báo lỗi tiếng Việt
chính xác kèm số dòng.

**Kết quả:** 14 ok + 22 fail đạt 100%.

## GIAI ĐOẠN 3 — Trình biên dịch tự viết: front-end ✅

**Mục tiêu:** lexer + parser + checker của `hlc` được viết 100% bằng HLS.

**Công việc:**
- Bộ AST dạng "pool chỉ số" (không cần con trỏ/null — hợp triết lý ngôn ngữ).
- Mọi trạng thái truyền tường minh qua `Ctx` — không có biến toàn cục ẩn.
- Kiểm tra kiểu đầy đủ + phân tích effects bất động điểm — **bắt chước 100% thông báo
  lỗi của Stage-0**.

**Tiêu chí nghiệm thu:** 22/22 chương trình lỗi bị từ chối với đúng thông điệp.

**Kết quả:** `src/hlc.hls` phần lex/parse/check (~1.800 dòng HLS).

## GIAI ĐOẠN 4 — Backend HLS → C + runtime C ✅

**Mục tiêu:** sinh mã C biên dịch được bằng gcc/clang, ngữ nghĩa trùng khớp Stage-0.

**Công việc:**
- Runtime C nhúng: chuỗi kiểm tra biên, danh sách box, map băm giữ thứ tự chèn,
  số học `__builtin_*_overflow`, I/O, `panic` mã 101.
- Sinh mã: kiểu HLS → kiểu C, boxing theo kiểu tĩnh, literal danh sách thành hàm
  helper, unique tên biến cục bộ, checked arithmetic.
- Mô hình cấp phát arena v0.1 (không `free` → không thể use-after-free về cấu trúc).

**Tiêu chí nghiệm thu:** 14/14 kiểm thử vi sai (thông dịch ↔ native) — stdout và mã
thoát giống hệt nhau, kể cả panic.

**Kết quả:** runtime 425 dòng C nhúng + codegen ~700 dòng HLS.

## GIAI ĐOẠN 5 — Tự dịch hoàn toàn (fixed-point) ✅

**Mục tiêu:** bằng chứng tối thượng của self-hosting.

**Chuỗi nghiệm thu:**
```
boot.py chạy hlc.hls  →  hlc.c  (lần 1)  →  gcc  →  hlc (native)
hlc native chạy hlc.hls  →  hlc.c  (lần 2)
diff hlc.c(lần 1) hlc.c(lần 2)  →  GIỐNG HỆT NHAU
```

**Kết quả:** `make bootstrap` xác nhận quá trình tự dịch **xác định** (deterministic);
56/56 kiểm thử tổng PASS. Từ đây, mọi thay đổi của ngôn ngữ đều được thực hiện
bằng chính ngôn ngữ đó.

## GIAI ĐOẠN 6 — Hệ thống module & thư viện chuẩn ⬜

**Mục tiêu:** tổ chức mã quy mô lớn mà vẫn kiểm toán được.

**Công việc:**
- Cú pháp `import` với đường dẫn mô-đun, biên dịch theo biểu đồ phụ thuộc.
- Thư viện chuẩn dạng gói: `std.io`, `std.str`, `std.fs`, `std.math` (toán bit kiểm tra
  tràn: `<<`/`>>` kiểm tra phạm vi, `&`/`|`/`^` cho int), `std.time`, `std.env`.
- Hiệu ứng chi tiết hơn cho từng nhóm: đọc/ghi tệp tách khỏi in màn hình.
- Unicode: chuỗi UTF-8 với API rune rõ ràng (byte string vẫn giữ cho hệ thống).

**Nghiệm thu:** `hlc` tự dịch được khi nguồn chia nhiều tệp; thư viện chuẩn có kiểm thử
vi sai riêng.

**Rủi ro:** thiết kế module sai sớm rất tốn sửa về sau → quyết định bằng văn bản
thiết kế (RFC) trước khi code.

## GIAI ĐOẠN 7 — Hệ thống kiểu nâng cao ⬜

**Mục tiêu:** tính minh bạch của kiểu đặt bảng + sự an toàn của kiểu đại diện.

**Công việc:**
- `enum` + `match` cảm tính đầy đủ (exhaustiveness checking).
- `Option[T]`/`Result[T, E]` trong thư viện chuẩn; `?` lan truyền lỗi; **bỏ `panic`
  cho lỗi dự kiến được** — panic chỉ còn cho lỗi lập trình.
- Generics đơn hình hoá (monomorphization) — mỗi bản dựng kiểu sinh mã riêng, hiệu
  năng như code viết tay.
- Suy luận kiểu cục bộ (gợi ý kiểu tại literal) — kiểu vẫn bắt buộc ở ranh giới hàm.
- Struct có giá trị mặc định cho trường.

**Nghiệm thu:** viết lại `hlc` dùng Option/Result cho mọi thao tác I/O; loại bỏ toàn
bộ panic dự kiến trong compiler.

## GIAI ĐOẠN 8 — Sở hữu & borrow checking ⬜

**Mục tiêu:** memory safety KHÔNG GC và kết thúc mô hình arena.

**Công việc:**
- Ngữ dịch chuyển (move) mặc định; mượn (borrow) có kiểm tra: một mượn biến đổi HOẶC
  nhiều mượn chỉ đọc.
- `free` chính xác khi ra khỏi phạm vi; kiểm chứng không use-after-free/double-free
  bằng chính hệ thống kiểu.
- Vùng sống (lifetime) tối giản: không cú pháp lifetime — suy luận toàn bộ, chỉ báo
  lỗi khi không suy luận được.
- Runtime C mới thay arena: alloca stack + malloc heap có giờ giải phóng tĩnh.

**Nghiệm thu:** chương trình đột biến bộ nhớ (web server chạy 24h) không tăng RSS;
đo kiểm tra Valgrind/ASan sạch.

**Rủi ro cao nhất của cả roadmap** — dự phòng 30% thời lượng; có thể hạ mức
"ref-counting + vòng phân tích sở hữu" nếu borrow-check đầy đủ quá tốn.

## GIAI ĐOẠN 9 — Hệ thống effects chi tiết + capability ⬜

**Mục tiêu:** mỗi hiệu ứng được khai báo riêng và kiểm chứng tĩnh.

**Công việc:**
- Tách effects: `uses IO`, `uses Net`, `uses Fs`, `uses Clock`, `uses Rand`, `uses Proc`.
- Capability token: mở tệp/network phải cầm capability do `main` cấp — không thể
  "lén" đọc tệp trong thư viện sâu.
- Hàm thuần được đánh dấu & bảo đảm tĩnh → eligible cho memoization/biên dịch lúc chạy.
- Từ chối (deny) mặc định ở mức biên dịch khi thiếu khai báo.

**Nghiệm thu:** một chương trình không khai báo `uses Net` không THỂ gọi socket dù
qua 5 lớp hàm — lỗi biên dịch chỉ rõ chuỗi lời gọi.

## GIAI ĐOẠN 10 — Taint tracking & sandbox ⬜

**Mục tiêu:** chống lỗ hổng đầu vào (injection, XSS, path traversal) ngay tại kiểu.

**Công việc:**
- Kiểu `tainted[T]`: dữ liệu từ đầu vào tự động `tainted`; chỉ được dùng sau `sanitize`.
- Bộ lọc chuẩn hoá cho SQL/HTML/đường dẫn/lệnh; khớp với库 sentinel chuẩn.
- Chế độ sandbox biên dịch: chương trình chỉ chạy trong thư mục/cầu nối được cấp.
- Báo cáo phân tích taint trong trình biên dịch (`hlc --audit`).

**Nghiệm thu:** cố tình dùng đầu vào người dùng trong câu SQL không qua sanitize →
lỗi biên dịch với đường lan truyền taint.

## GIAI ĐOẠN 11 — IR SSA + tối ưu hoá ⬜

**Mục tiêu:** hiệu năng ngang hệ C/Rust ở mức -O2.

**Công việc:**
- IR mức trung dạng SSA (HLIR) viết bằng HLS; HLS→HLIR→C.
- Tối ưu: inlining, constant folding, DCE, copy propagation, escape analysis, loop-
  invariant code motion, strength reduction.
- Chế độ `-O fast` bỏ kiểm tra **chỉ khi chứng minh được** giá trị an toàn (biên ngoài
  phạm vi không thể, phép cộng không tràn) — hoặc khi người dùng ký nhận rủi ro.
- Thông tin vị trí trong panic (file:dòng) nhờ debug info IR.

**Nghiệm thu:** benchmark chuẩn (sieve, json parse, matrix) đạt ≥ 95% hiệu năng `gcc -O2`
trên mã C tương đương; kiểm thử vi sai vẫn 100% sau tối ưu.

## GIAI ĐOẠN 12 — Backend LLVM native ⬜

**Mục tiêu:** bỏ trung gian C, sinh mã máy trực tiếp.

**Công việc:**
- HLIR → LLVM IR (qua C++ binding hoặc sinh văn bản `.ll`).
- Đa nền tảng: x86-64, AArch64; cross-compile (`--target aarch64-linux`).
- Hỗ trợ stack probe (đệ quy sâu không còn segfault), hot/cold attribute,
  PGO (profile-guided optimization).
- Backend C giữ làm fallback và cho nền phức tạp.

**Nghiệm thu:** boot bootstrap thrice-clean: HLS→LLVM→native→tự dịch chính nó, so
khớp output với backend C.

## GIAI ĐOẠN 13 — Trình quản lý gói `hls-pkg` ⬜

**Mục tiêu:** tái sử dụng mã nguồn có kiểm chứng nguồn gốc.

**Công việc:**
- `hls-pkg.toml` + khoá nội dung (content-addressed lockfile): mỗi gói định danh bằng
  băm SHA-256 của nội dung + bảng hiệu ứng của gói.
- Cài đặt các hiệu ứng của gói: gói thư viện thuần KHÔNG THỂ khai báo `uses Net`.
- Registry phi tập trung (git-based) + bản ghi minh bạch.
- `hls-pkg audit`: in tổng các capability/effects của toàn bộ cây phụ thuộc.

**Nghiệm thu:** cài một gói của bên thứ ba, xem báo cáo hiệu ứng, build tái lập được
bit-for-bit từ lockfile.

## GIAI ĐOẠN 14 — Bộ công cụ: LSP, formatter, linter ⬜

**Mục tiêu:** trải nghiệm nhà phát triển hạng nhất.

**Công việc:**
- `hls-lsp`: máy chủ ngôn ngữ (định nghĩa, hoàn thành, đổi tên, chẩn đoán kiểu/effects
  thời gian thực).
- `hlfmt`: formatter quyết định (như gofmt) — hết tranh cãi style.
- `hllint`: quy tắc an toàn (phát hiện bỏ qua Result, unwrap trống, hiệu ứng lan
  rộng không cần thiết).
- Cả ba viết bằng HLS, phát hành dạng binary native.

**Nghiệm thu:** plugin VS Code + Neovim; formatter idempotent (chạy 2 lần = 1 lần).

## GIAI ĐOẠN 15 — FFI an toàn với C ⬜

**Mục tiêu:** tái sử dụng hệ sinh thái C mà không phá特区 an toàn.

**Công việc:**
- `extern "C"` với bảng kiểu tường minh; biên dịch sinh header kiểm tra tương thích ABI.
- Quy tắc sở hữu qua biên giới: dữ liệu truyền vào FFI bị đóng băng (freeze) hoặc sao
  chép; kết quả về phải qua lớp kiểm tra null/biên.
- `bindgen`: sinh khai báo HLS từ header C kèm chú thích hiệu ứng thủ công.
- Hàng rào: mọi lời gọi FFI tự động mang hiệu ứng `IO` (an toàn mặc định).

**Nghiệm thu:** gọi `libcurl` từ HLS qua lớp bindgen; ASan không phát hiện lỗi ở mã
glue.

## GIAI ĐOẠN 16 — Đồng thời & async (data-race freedom) ⬜

**Mục tiêu:** tận dụng đa lõi mà không có data race — bằng hệ thống kiểu.

**Công việc:**
- Trait `Send`/`Sync` tương đương (kiểu có thể chuyển lõi / chia sẻ an toàn) áp lên
  hệ thống sở hữu của Giai đoạn 8.
- `spawn` tạo tác vụ; kênh truyền thông điệp (channel) làm nguyên thuỷ chính.
- `async/await` với bộ định thời任务的 (work-stealing scheduler) viết bằng HLS.
- Actor model cho trạng thái chia sẻ; API `select` cho kênh.

**Nghiệm thu:** chương trình chia sẻ biến không qua kênh → lỗi biên dịch; benchmark
đồng thời (web server) scale tuyến tính tới 8 lõi.

## GIAI ĐOẠN 17 — Kiểm chứng hình thức & hợp đồng ⬜

**Mục tiêu:** "bảo mật cực mạnh" được chứng minh, không chỉ tuyên bố.

**Công việc:**
- Hợp đồng: `requires`/`ensures` trên hàm; kiểm tra tĩnh cho tập con (SMT solver
  z3 qua cầu nối sinh ra từ HLS).
- Mô hình kiểm tra (model checking) cho trạng thái hữu hạn: enum trạng thái máy.
- Chế độ `-O fast` mở khoá bằng chứng minh: bỏ kiểm tra tràn khi chứng minh được
  phạm vi số học.
- Bộ quy tắc suy luận tự động cho vòng lặp (loop invariant gợi ý).

**Nghiệm thu:** một mô-đun crypto cốt lõi (ví dụ HMAC) được chứng minh hoàn toàn
bằng hợp đồng HLS, không cần panic kiểm tra nào.

## GIAI ĐOẠN 18 — Hệ sinh thái kiểm thử & fuzzing ⬜

**Mục việc:**
- `hltest`: unit test trong ngôn ngữ (`test` khối, `assert_eq`), chạy song song.
- Property-based testing (tương tự quickcheck) tích hợp sẵn.
- `hls-fuzz`: fuzzing mức AST — sinh chương trình HLS ngẫu nhiên, chạy differential
  thông dịch ↔ biên dịch, tự thu gọn chương trình gây khác biệt.
- Theo dõi độ bao phủ (coverage) từ HLIR.

**Nghiệm thu:** fuzzer chạy 1 giờ không tìm thấy khác biệt ngữ nghĩa nào giữa hai
bản triển khai; CI hàng ngày.

## GIAI ĐOẠN 19 — Tài liệu, sách, playground ⬜

**Công việc:**
- "The Hieu Louis Book" — giáo trình tiếng Việt + tiếng Anh, từ nhập môn đến sở hữu/
  hiệu ứng/kiểm chứng.
- Trang web + playground chạy native trong trình duyệt (WebAssembly backend).
- Ví dụ thật: web server, CLI tool, chương trình phân tích dữ liệu.
- Chuỗi hướng dẫn "viết trình biên dịch bằng HLS" — dùng chính hlc làm giáo án.

**Nghiệm thu:** người mới cài đặt đến "hello world native" trong < 10 phút, không
rời tài liệu chính thức.

## GIAI ĐOẠN 20 — HLS v1.0 — đóng băng API, LTS, bootstrap thuần HLS ⬜

**Mục tiêu:** phát hành ổn định dài hạn.

**Công việc:**
- Đóng băng cú pháp + thư viện chuẩn (semver: đổi chỉ trong major).
- **Loại bỏ hoàn toàn `boot/`** — bootstrap chỉ còn HLS: mỗi bản phát hành được dựng
  bằng binary hlc của bản trước (bootstrap chain tái lập được bit-for-bit).
- Audit bảo mật độc lập (bên thứ ba) toàn bộ runtime + chuỗi bootstrap.
- Chính sách hỗ trợ: 3 năm vá lỗi cho v1.x.

**Nghiệm thu:** dựng v1.0 từ hai đường độc lập (từ binary phát hành trước + từ boot
Stage-0) cho cùng một binary — reproducible build.

---

## NGUYÊN TẮC ƯU TIÊN KHI XUNG ĐỘT

1. **An toàn > hiệu năng > tiện dụng.** Không bao giờ thêm "chế độ nhanh tắt kiểm tra"
   không có chứng minh (chỉ mở bằng hợp đồng — Giai đoạn 17).
2. **Hạt nhân nhỏ, kiểm chứng được.** Ưa mở rộng bằng thư viện chuẩn hơn là thêm cú pháp.
3. **Mọi tính năng phải tự dịch được.** hlc luôn là chương trình HLS lớn nhất và là
   khách hàng đầu tiên của mọi tính năng mới (dogfooding bắt buộc).
4. **Không phá ngữ nghĩa hiện có.** Thay đổi hành vi chỉ được qua phiên bản major với
   công cụ migraion tự động.
5. **Hai bản triển khai, một sự thật.** Kiểm thử vi sai là cửa ải cuối của mọi PR —
   khác biệt nào giữa thông dịch và biên dịch đều là lỗi, không có ngoại lệ.
