bits 16
org 0x7c00

COM1            equ 0x3f8
SHELL_BUFFER    equ 0x0600
SHELL_CAPACITY  equ 32
EDITOR_BUFFER   equ 0x9000
EDITOR_CAPACITY equ 2048
IMAGE_SECTORS   equ 3
KERNEL_SECTORS  equ IMAGE_SECTORS - 1

; The BIOS loads this first sector at 0000:7c00. Load the small real-mode
; kernel from the following sectors, then transfer control to it.
boot:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7c00
    sti
    cld

    mov [boot_drive], dl
    mov bp, 3
.load:
    xor ah, ah
    mov dl, [boot_drive]
    int 0x13

    xor ax, ax
    mov es, ax
    mov bx, kernel
    mov ah, 0x02
    mov al, KERNEL_SECTORS
    xor ch, ch
    mov cl, 2
    xor dh, dh
    mov dl, [boot_drive]
    int 0x13
    jnc .loaded
    dec bp
    jnz .load
    jmp disk_error
.loaded:
    jmp 0:kernel

disk_error:
    xor bx, bx
    call serial_init
    mov si, disk_error_text
.print:
    lodsb
    test al, al
    jz .halt
    call serial_write
    mov ah, 0x0e
    int 0x10
    jmp .print
.halt:
    cli
    hlt
    jmp .halt

; These routines live in the always-resident loader sector so disk errors can
; be reported on the same headless serial console used by the kernel.
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

boot_drive      db 0
disk_error_text db 'Disk error', 13, 10, 0

times 510 - ($ - $$) db 0
dw 0xaa55

kernel:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7c00
    sti
    cld
    xor bp, bp
    xor bx, bx
    mov di, EDITOR_BUFFER
    mov byte [di], 0

    call serial_init
    mov si, banner
    call print_string

shell:
    mov si, prompt
    call print_string
    mov di, SHELL_BUFFER
    xor cx, cx

.read:
    call serial_read
    cmp al, 10
    jne .not_line_feed
    test bp, bp
    jz .execute
    xor bp, bp
    jmp .read
.not_line_feed:
    xor bp, bp
    cmp al, 13
    je .carriage_return
    cmp al, 8
    je .backspace
    cmp al, 127
    je .backspace
    cmp al, ' '
    jb .read
    cmp cx, SHELL_CAPACITY - 1
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

    mov si, SHELL_BUFFER
    mov di, cmd_help
    call strings_equal
    jc command_help

    mov si, SHELL_BUFFER
    mov di, cmd_edit
    call strings_equal
    jc command_edit

    mov si, SHELL_BUFFER
    mov di, cmd_clear
    call strings_equal
    jc command_clear

    mov si, SHELL_BUFFER
    mov di, cmd_reboot
    call strings_equal
    jc command_reboot

    mov si, SHELL_BUFFER
    mov di, cmd_halt
    call strings_equal
    jc command_halt

    mov si, SHELL_BUFFER
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

command_edit:
    mov cx, bx
    mov di, EDITOR_BUFFER
    add di, cx
    mov byte [di], 0
    call editor_redraw

.read:
    call serial_read
    cmp al, 10
    jne .not_line_feed
    test bp, bp
    jz .newline
    xor bp, bp
    jmp .read
.not_line_feed:
    xor bp, bp
    cmp al, 13
    je .carriage_return
    cmp al, 24
    je .exit
    cmp al, 12
    je .clear
    cmp al, 8
    je .backspace
    cmp al, 127
    je .backspace
    cmp al, ' '
    jb .read
    cmp cx, EDITOR_CAPACITY - 1
    jae .full

    stosb
    inc cx
    mov byte [di], 0
    call serial_write
    jmp .read

.carriage_return:
    inc bp
.newline:
    cmp cx, EDITOR_CAPACITY - 2
    jae .full
    mov al, 13
    stosb
    inc cx
    call serial_write
    mov al, 10
    stosb
    inc cx
    mov byte [di], 0
    call serial_write
    jmp .read

.backspace:
    test cx, cx
    jz .read
    cmp byte [di - 1], 10
    je .remove_newline
    dec di
    dec cx
    mov byte [di], 0
    call editor_redraw
    jmp .read

.remove_newline:
    sub di, 2
    sub cx, 2
    mov byte [di], 0
    call editor_redraw
    jmp .read

.clear:
    xor cx, cx
    mov di, EDITOR_BUFFER
    mov byte [di], 0
    call editor_redraw
    jmp .read

.full:
    mov al, 7
    call serial_write
    jmp .read

.exit:
    mov bx, cx
    mov si, newline
    call print_string
    jmp shell

editor_redraw:
    mov si, clear_sequence
    call print_string
    mov si, editor_header
    call print_string
    mov si, EDITOR_BUFFER
    call print_string
    ret

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

serial_read:
    mov dx, COM1 + 5
.wait:
    in al, dx
    test al, 1
    jz .wait
    mov dx, COM1
    in al, dx
    ret

banner         db 27, '[2J', 27, '[H', 'Nixodria OS', 13, 10, 'Type help.', 13, 10, 0
prompt         db 'nix> ', 0
newline        db 13, 10, 0
erase          db 8, ' ', 8, 0
unknown        db 'Unknown command.', 13, 10, 0
help_text      db 'help edit clear echo <text> reboot halt', 13, 10, 0
editor_header  db 'Nixodria Editor', 13, 10, 'Ctrl-X exit | Ctrl-L clear', 13, 10, 13, 10, 0
clear_sequence db 27, '[2J', 27, '[H', 0
rebooting      db 'Rebooting...', 13, 10, 0
halted         db 'Halted.', 13, 10, 0
cmd_help       db 'help', 0
cmd_edit       db 'edit', 0
cmd_clear      db 'clear', 0
cmd_reboot     db 'reboot', 0
cmd_halt       db 'halt', 0
cmd_echo       db 'echo ', 0

times IMAGE_SECTORS * 512 - ($ - $$) db 0
