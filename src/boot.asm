bits 16
org 0x7c00

COM1        equ 0x3f8
BUFFER      equ 0x0600
BUFFER_SIZE equ 32

start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7c00
    sti
    cld
    xor bp, bp

    call serial_init
    mov si, banner
    call print_string

shell:
    mov si, prompt
    call print_string
    mov di, BUFFER
    xor cx, cx

.read:
    call serial_read
    test bp, bp
    jz .line_ending
    xor bp, bp
    cmp al, 10
    je .read
.line_ending:
    cmp al, 13
    je .carriage_return
    cmp al, 10
    je .execute
    cmp al, 8
    je .backspace
    cmp al, 127
    je .backspace
    cmp al, ' '
    jb .read
    cmp cx, BUFFER_SIZE - 1
    jae .read

    stosb
    inc cx
    call serial_write
    jmp .read

.backspace:
    test cx, cx
    jz .read
    dec di
    dec cx
    mov si, erase
    call print_string
    jmp .read

.carriage_return:
    inc bp
.execute:
    mov byte [di], 0
    mov si, newline
    call print_string
    test cx, cx
    jz shell

    mov si, BUFFER
    mov di, cmd_help
    call strings_equal
    jc command_help

    mov si, BUFFER
    mov di, cmd_clear
    call strings_equal
    jc command_clear

    mov si, BUFFER
    mov di, cmd_reboot
    call strings_equal
    jc command_reboot

    mov si, BUFFER
    mov di, cmd_halt
    call strings_equal
    jc command_halt

    mov si, BUFFER
    mov di, cmd_echo
    call prefix_equal
    jc command_echo

    mov si, unknown
    call print_string
    jmp shell

command_help:
    mov si, help_text
    call print_string
    jmp shell

command_clear:
    mov si, clear_sequence
    call print_string
    jmp shell

command_echo:
    call print_string
    mov si, newline
    call print_string
    jmp shell

command_reboot:
    mov si, rebooting
    call print_string
    int 0x19
    jmp shell

command_halt:
    mov si, halted
    call print_string
    cli
.hang:
    hlt
    jmp .hang

; Carry is set when the zero-terminated strings at DS:SI and ES:DI match.
strings_equal:
    lodsb
    scasb
    jne .different
    test al, al
    jnz strings_equal
    stc
    ret
.different:
    clc
    ret

; Carry is set when DS:SI starts with the zero-terminated prefix at ES:DI.
; On success, SI points to the first byte after the prefix.
prefix_equal:
    mov al, [di]
    inc di
    test al, al
    jz .matched
    cmp al, [si]
    jne .different
    inc si
    jmp prefix_equal
.matched:
    stc
    ret
.different:
    clc
    ret

print_string:
    lodsb
    test al, al
    jz .done
    call serial_write
    jmp print_string
.done:
    ret

serial_init:
    mov dx, COM1 + 1
    xor al, al
    out dx, al
    mov dx, COM1 + 3
    mov al, 0x80
    out dx, al
    mov dx, COM1
    mov al, 3
    out dx, al
    mov dx, COM1 + 1
    xor al, al
    out dx, al
    mov dx, COM1 + 3
    mov al, 3
    out dx, al
    mov dx, COM1 + 2
    mov al, 0xc7
    out dx, al
    mov dx, COM1 + 4
    mov al, 0x0b
    out dx, al
    ret

serial_read:
    mov dx, COM1 + 5
.wait:
    in al, dx
    test al, 1
    jz .wait
    mov dx, COM1
    in al, dx
    ret

serial_write:
    push ax
    mov ah, al
    mov dx, COM1 + 5
.wait:
    in al, dx
    test al, 0x20
    jz .wait
    mov dx, COM1
    mov al, ah
    out dx, al
    pop ax
    ret

banner         db 27, '[2J', 27, '[H', 'Nixodria OS', 13, 10, 'Type help.', 13, 10, 0
prompt         db 'nix> ', 0
newline        db 13, 10, 0
erase          db 8, ' ', 8, 0
unknown        db 'Unknown command.', 13, 10, 0
help_text      db 'help clear echo <text> reboot halt', 13, 10, 0
clear_sequence db 27, '[2J', 27, '[H', 0
rebooting      db 'Rebooting...', 13, 10, 0
halted         db 'Halted.', 13, 10, 0
cmd_help       db 'help', 0
cmd_clear      db 'clear', 0
cmd_reboot     db 'reboot', 0
cmd_halt       db 'halt', 0
cmd_echo       db 'echo ', 0

times 510 - ($ - $$) db 0
dw 0xaa55
