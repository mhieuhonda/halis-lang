/* Stage 84: a minimal placement script for the linker-coverage tests.
 * `.text.*` is a PREFIX pattern, so every `#[section(".text.<x>")]`
 * the tests use lands in `.text`; `.device` exists so the tests have a
 * section that genuinely is placed.
 */
ENTRY(_start)

SECTIONS
{
    . = 0x100000;

    .text : ALIGN(4)
    {
        KEEP(*(.text .text.*))
    }

    .device : ALIGN(4096)
    {
        KEEP(*(.device))
    }
}
