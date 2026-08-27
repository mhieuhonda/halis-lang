# ============================================================================
# Makefile — Hieu Louis (HLS)
# ============================================================================
CC      ?= gcc
CFLAGS  ?= -O2
PYTHON  ?= python3
HLC     = src/hlc.hls
BIN     = bin

.PHONY: all stage0 bootstrap test examples clean run check

# Muc tieu chinh: dung chuoi bootstrap day du de tao trinh bien dich native
all: bootstrap

# Chay Stage-0 truc tiep (thong dich HLS)
stage0:
	$(PYTHON) boot/boot.py --help 2>/dev/null || true
	@echo "Vi du: $(PYTHON) boot/boot.py examples/hello.hls"

# BOOTSTRAP DAY DU:
#   1. Stage-0 chay hlc.hls de bien dich chinh no -> hlc.c
#   2. gcc bien dich thanh hlc native
#   3. hlc native tu bien dich lai -> kiem tra xac dinh
bootstrap:
	@mkdir -p $(BIN)
	@echo "[1/4] Stage-0: hlc.hls tu dich chinh no..."
	@$(PYTHON) boot/boot.py $(HLC) $(HLC) $(BIN)/hlc.c
	@echo "[2/4] gcc: bien dich hlc native..."
	@$(CC) $(CFLAGS) -o $(BIN)/hlc $(BIN)/hlc.c -lm
	@echo "[3/4] hlc native tu bien dich lai..."
	@$(BIN)/hlc $(HLC) $(BIN)/hlc2.c
	@echo "[4/4] So sanh 2 lan sinh ma..."
	@diff $(BIN)/hlc.c $(BIN)/hlc2.c && echo "BOOTSTRAP OK: qua trinh tu dich xac dinh"
	@rm -f $(BIN)/hlc2.c
	@echo "Trinh bien dich native: $(BIN)/hlc"

# Bien dich va chay mot chuong trinh HLS: make run F=examples/hello.hls
run:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@$(BIN)/hlc $(F) /tmp/hls_out.c && $(CC) $(CFLAGS) -o /tmp/hls_out /tmp/hls_out.c -lm && /tmp/hls_out

# Chi kiem tra kieu + effects (khong chay)
check:
	$(PYTHON) boot/boot.py --check $(F)

# Bo kiem thu day du
test:
	bash tests/run_tests.sh

examples:
	@for f in examples/hello.hls examples/fibonacci.hls examples/primes.hls; do \
		echo "--- $$f"; $(PYTHON) boot/boot.py $$f; \
	done

clean:
	rm -rf $(BIN) /tmp/hls_out /tmp/hls_out.c
