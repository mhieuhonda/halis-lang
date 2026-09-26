/* Halis freestanding image layout.
 *
 * Platform loaders may relocate the image; all application metadata is
 * retained in named, non-allocatable diagnostic sections and in the
 * .halis.metadata section used by the runtime image builder.
 */
ENTRY(_start)

SECTIONS
{
    . = 0x100000;

    .text : ALIGN(4)
    {
        KEEP(*(.text .text.*))
    }

    .rodata : ALIGN(4)
    {
        *(.rodata .rodata.*)
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

    .data : ALIGN(8)
    {
        *(.data .data.*)
        *(.sdata .sdata.*)
        *(.got .got.plt)
    }

    .bss (NOLOAD) : ALIGN(8)
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
