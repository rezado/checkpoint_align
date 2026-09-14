    .section .text
    .globl _start
    .option rvc
_start:
    c.nop
    c.li t0, 2
    .option push
    .option norvc
    addi t1, zero, 1
    beq t0, t1, skipped
    .option pop
    c.addi t0, -1
    beq t0, t1, target
skipped:
    c.nop
target:
    c.nop
    c.li a0, 0
    .word 0x0000006b
