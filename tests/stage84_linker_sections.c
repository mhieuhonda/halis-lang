/* Stage 84 linker/section integration fixture. */

const volatile unsigned char stage84_metadata[]
    __attribute__((section(".halis.metadata"), used)) = { 0x48, 0x4c, 0x53 };

const volatile unsigned char stage84_sections[]
    __attribute__((section(".halis.sections"), used)) = { 0x53, 0x45, 0x43 };

/* A freestanding image has no libc startup path. */
void _start(void)
{
    for (;;) {
        /* The fixture is linked, not executed by the host test runner. */
    }
}
