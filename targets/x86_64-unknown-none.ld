/* Halis bare-metal x86_64-unknown-none image layout.
 *
 * Stage 93 (v0.112.0-alpha) — the first of the three bare-metal
 * triples. The image is linked with
 *
 *   gcc -mgeneral-regs-only -ffreestanding -nostdlib -nostartfiles
 *       -ffunction-sections -fno-stack-protector
 *       -Wl,--gc-sections,-T,targets/x86_64-unknown-none.ld
 *
 * and carries no libc, no crt0, and no floating point (the bare-metal
 * triples are integer-only: the FPU is not enabled before a kernel
 * saves its state, and -mgeneral-regs-only makes the compiler refuse
 * to emit SSE regardless).
 *
 * Platform loaders may relocate the image; all application metadata is
 * retained in named, non-allocatable diagnostic sections and in the
 * .halis.metadata section used by the runtime image builder (Stage 84).
 */
OUTPUT_FORMAT(elf64-x86-64)
OUTPUT_ARCH(i386:x86-64)
ENTRY(_start)

SECTIONS
{
    /* 1 MiB — the conventional load address of a bare x86-64 image:
     * where a Multiboot2 bootloader places (and jumps into) one, and
     * the first address above the real-mode IVT/BIOS data areas that
     * QEMU's -kernel loader maps. */
    . = 0x100000;

    .text : ALIGN(16)
    {
        /* The naked _start must be the image's first instruction: a
         * bootloader or `-kernel` loader enters at the base address,
         * not at the ELF entry point. -ffunction-sections gives _start
         * its own input section, so KEEP plus explicit placement pins
         * it to the base regardless of link order. */
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
     * is looking for. Multiboot2 scans the first 32 KiB of the image,
     * which .text._start + .rodata comfortably stay inside for the
     * header's sake. */
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
