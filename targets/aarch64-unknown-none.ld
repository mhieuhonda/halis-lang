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
    /* 0x40000000 — the DRAM base of QEMU's `virt` machine. The direct
     * `-kernel image.elf` boot (no -bios: 8.2 has no `none` firmware
     * spelling for ARM, it reads it as a ROM filename and dies) loads
     * the ELF segments here and enters the ELF entry — QEMU's own DTB
     * stays out of the image's way in that path. */
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

    /* Ubuntu's cross-gcc passes --build-id, so the link carries a
     * .note.gnu.build-id. On binutils < 2.44 that orphan is placed at
     * the image BASE — before .text — which pushes _start 0x30 past
     * the DRAM base the whole triple is contracted to enter at. An
     * explicit home AFTER .text pins the entry at the base for every
     * linker; 2.44+ already puts it here on its own. */
    .notes : ALIGN(4)
    {
        *(.note.gnu.build-id)
        *(.note.gnu.property)
        *(.note.ABI-tag)
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

    /* The entry's stack. QEMU's virt boards enter the image with
     * SP=0 — only x86-64 firmware hands a naked _start a usable RSP —
     * so the aarch64 entry arms its own stack from __stack_top before
     * `bl hl_boot` can push once. 64 KiB, growing down, above the
     * image; NOLOAD keeps it out of the file and ALIGN(16) keeps the
     * AAPCS64 SP invariant. */
    .stack (NOLOAD) : ALIGN(16)
    {
        __stack_bottom = .;
        . += 0x10000;
        __stack_top = .;
    }

    _end = .;

    /DISCARD/ :
    {
        *(.comment)
        *(.eh_frame)
        *(.note.GNU-stack)
    }
}
