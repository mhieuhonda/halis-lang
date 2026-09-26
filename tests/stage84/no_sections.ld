/* Stage 84: a script with no SECTIONS block. A Halis image must describe
 * its output section layout explicitly — otherwise the placement is
 * gcc's default, not the author's. */
ENTRY(_start)
