# Chính sách bảo mật — Hieu Louis (HLS)

## Mô hình bảo mật của ngôn ngữ (v0.1)

Hieu Louis được thiết kế để **an toàn là trạng thái mặc định**. Ba lớp bảo vệ:

### Lớp 1 — Thời điểm biên dịch (tĩnh)
- **Hệ thống hiệu ứng:** hàm không khai báo `uses IO` được *bảo đảm* không thể
  thực hiện I/O (in, đọc/ghi tệp, tham số dòng lệnh, thoát, đồng hồ). Phân tích
  bất động điểm trên đồ thị lời gọi tĩnh — không có đường lách qua gọi gián tiếp.
- **Kiểu tĩnh tuyệt đối:** không ép kiểu ngầm, không suy luận lỏng, no shadowing,
  điều kiện phải là `bool`, mọi liên kết khai báo kiểu.
- **Không null / không chưa khởi tạo:** mọi biến gán ngay khi khai báo.

### Lớp 2 — Thời điểm chạy (động)
- Số học `int` 64-bit kiểm tra tràn: `+ - * / %` và phủ định đều kiểm tra.
- Truy cập mảng/chuỗi kiểm tra biên; chia cho 0 dừng an toàn.
- Lỗi chạy là `panic` có kiểm soát: thoát mã 101, không có undefined behavior.

### Lớp 3 — Cấu trúc (kiến trúc)
- Mô hình cấp phát arena v0.1: **không tồn tại lệnh free** → không thể
  use-after-free / double-free về mặt cấu trúc. (Ownership chính xác: Giai đoạn 8.)
- Trình biên dịch không đọc/ghi gì ngoài tệp vào/ra được chỉ định rõ.

## Những gì v0.1 CHƯA bảo vệ được (trung thực)
- Chưa có taint tracking / sandbox (Giai đoạn 10).
- Chưa có capability tokens chi tiết (Giai đoạn 9).
- Đệ quy rất sâu có thể tràn stack native (Giai đoạn 11: stack probes).
- Chưa ký số học của chuỗi công cụ (Giai đoạn 13: content-addressed packages).

## Báo cáo lỗ hổng
Tìm thấy lỗi khiến hai bản triển khai (Stage-0 vs native) cho kết quả khác nhau,
hoặc sinh mã C không an toàn? Đó là lỗi nghiêm trọng. Vui lòng mở issue với nhãn
`security` kèm chương trình tái hiện nhỏ nhất.

## Phạm vi
Chính sách này áp dụng cho chính công cụ (`boot/`, `src/hlc.hls`, runtime sinh ra).
Các chương trình người dùng viết bằng HLS chịu trách nhiệm theo mô hình trên.
