/* Halis bare-metal aarch64-unknown-none image layout.
 *
 * Stage 94 (v0.113.0-alpha) — the second bare-metal triple. The image
 * is linked with
 *
 *   <aarch64 cc> -mgeneral-regs-only -ffreestanding -nostdlib
 *       -nostartfiles -ffunction-sections -fno-stack-protector
 *       -Wl,--gc-sections,-T,targets/aarch64-unknown-none.ld
 *
 * and carries no libc, no crt0, and no floating point (the bare-metal
 * triples are integer-only: the FPU is not enabled before a kernel
 * saves its state, and -mgeneral-regs-only makes the compiler refuse
 * to emit NEON/FP regardless).
 *
 * Platform loaders may relocate the image; all application metadata is
 * retained in named, non-allocatable diagnostic sections and in the
 * .halis.metadata section used by the runtime image builder (Stage 84).
 */
OUTPUT_FORMAT(elf64-littleaarch64)
OUTPUT_ARCH(aarch64)
ENTRY(_start)

SECTIONS
{
    /* 0x40000000 — the DRAM base of QEMU's `virt` machine (and the
     * usual load address of a bare AArch64 image on it): with
     * `-bios none -kernel image.elf` the firmware loader places the
     * segments here and the CPU fetches the first instruction from
     * the image base. */
    . = 0x40000000;

    .text : ALIGN(16)
    {
        /* The naked _start must be the image's first instruction: a
         * loader that jumps to the base address (rather than to the
         * ELF entry point) fetches exactly these bytes.
         * -ffunction-sections gives _start its own input section, so
         * KEEP plus explicit placement pins it regardless of link
         * order. */
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
     * is looking for. */
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
