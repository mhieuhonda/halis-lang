/* Halis bare-metal riscv64-unknown-none image layout.
 *
 * Stage 95 (v0.114.0-alpha) — the third bare-metal triple, completing
 * the roadmap's Phase VI trio. The image is linked with
 *
 *   <riscv64 cc> -march=rv64imac -mabi=lp64 -ffreestanding -nostdlib
 *       -nostartfiles -ffunction-sections -fno-stack-protector
 *       -Wl,--gc-sections,-T,targets/riscv64-unknown-none.ld
 *
 * and carries no libc, no crt0, and no floating point: rv64imac has no
 * float registers at all (a kernel must not touch the FPU before it
 * saves the FP state), so the ISA itself is the integer-only backstop
 * behind the compiler's --target discipline.
 *
 * Platform loaders may relocate the image; all application metadata is
 * retained in named, non-allocatable diagnostic sections and in the
 * .halis.metadata section used by the runtime image builder (Stage 84).
 */
OUTPUT_FORMAT(elf64-littleriscv)
OUTPUT_ARCH(riscv)
ENTRY(_start)

SECTIONS
{
    /* 0x80000000 — the DRAM base of QEMU's riscv64 `virt` machine: with
     * `-bios none -kernel image.elf` the loader places the segments
     * here and the hart fetches its first instruction from the image
     * base (the virt board's reset vector, masked ROM, enters there). */
    . = 0x80000000;

    .text : ALIGN(16)
    {
        /* The naked _start must be the image's first instruction: the
         * `-bios none` entry jumps at the DRAM base regardless of the
         * ELF entry point. -ffunction-sections gives _start its own
         * input section, so KEEP plus explicit placement pins it
         * regardless of link order. */
        KEEP(*(.text._start))
        *(.text .text.*)
    }

    .rodata : ALIGN(16)
    {
        *(.rodata .rodata.*)
    }

    /* Stage 85: the boot-protocol headers the C backend emits for a
     * `#![boot_header(...)]` crate. Both are KEEP-ed: they are reached
     * by a firmware scanning the image, never by a call, so without
     * KEEP --gc-sections would drop exactly the bytes the bootloader
     * is looking for. (Multiboot2 is x86 firmware, but the Limine
     * protocol boots RISC-V too — the keeps cost nothing.) */
    .multiboot_header : ALIGN(8)
    {
        KEEP(*(.multiboot_header))
    }

    .limine_requests : ALIGN(8)
    {
        KEEP(*(.limine_requests))
    }

    .halis_metadata : ALIGN(8)
    {
        __halis_metadata_start = .;
        KEEP(*(.halis.metadata))
        __halis_metadata_end = .;
    }

    .halis_sections : ALIGN(8)
    {
        __halis_sections_start = .;
        KEEP(*(.halis.sections))
        __halis_sections_end = .;
    }

    .data : ALIGN(16)
    {
        *(.data .data.*)
        *(.sdata .sdata.*)
        *(.got .got.plt)
    }

    .bss (NOLOAD) : ALIGN(16)
    {
        __bss_start = .;
        *(.bss .bss.*)
        *(.sbss .sbss.*)
        *(COMMON)
        __bss_end = .;
    }

    _end = .;

    /DISCARD/ :
    {
        *(.comment)
        *(.eh_frame)
        *(.note.GNU-stack)
    }
}
