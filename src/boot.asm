bits 16
org 0x7c00

COM1            equ 0x3f8
ESCAPE_KEY      equ 27
SHELL_BUFFER    equ 0x0600
SHELL_CAPACITY  equ 32
STORAGE_HEADER_A equ 0x9400
STORAGE_HEADER_B equ 0x9600
FILES_HEADER    equ 0x9800
HEADER_BACKUP   equ 0x9a00
EDITOR_BUFFER   equ 0x9c00
EDITOR_CAPACITY equ 2048
FILES_DATA      equ 0xa400
FILE_BACKUP     equ 0xe400
KERNEL_SECTORS  equ 10
STORAGE_LBA_A   equ KERNEL_SECTORS + 1
STORAGE_SLOT_SECTORS equ 33
STORAGE_LBA_B   equ STORAGE_LBA_A + STORAGE_SLOT_SECTORS
STORAGE_SECTORS equ STORAGE_SLOT_SECTORS * 2
IMAGE_SECTORS   equ 2880
STORAGE_MAGIC   equ 0x3358494e        ; "NIX3" in little-endian order
LEGACY_STORAGE_MAGIC equ 0x3258494e   ; "NIX2" in little-endian order
FILE_COUNT_MAX  equ 8
FILE_NAME_SIZE  equ 13
FILE_ENTRY_SIZE equ 18
FILE_ENTRIES    equ 8
FILE_LENGTH     equ 14
FILE_CHECKSUM   equ 16
FILES_HEADER_CRC equ FILE_ENTRIES + FILE_COUNT_MAX * FILE_ENTRY_SIZE
FILES_DATA_SECTORS equ FILE_COUNT_MAX * 4
PRINT_MODULE_LBA equ STORAGE_LBA_B + STORAGE_SLOT_SECTORS
PRINT_MODULE_SECTORS equ 32
PRINT_MODULE_ADDRESS equ 0x1000
PRINT_MODULE_SIGNATURE equ PRINT_MODULE_ADDRESS + 3
PRINT_MODULE_CHECKSUM equ PRINT_MODULE_ADDRESS + 12
BASIC_MODULE_LBA equ PRINT_MODULE_LBA + PRINT_MODULE_SECTORS
BASIC_MODULE_SECTORS equ 16
BASIC_MODULE_ADDRESS equ 0x1000
BASIC_MODULE_SIGNATURE equ BASIC_MODULE_ADDRESS + 3
BASIC_MODULE_CHECKSUM equ BASIC_MODULE_ADDRESS + 12
PACKAGE_CATALOG_LBA equ BASIC_MODULE_LBA + BASIC_MODULE_SECTORS
PACKAGE_SLOT_SECTORS equ 5
PACKAGE_COUNT_MAX equ 8
PACKAGE_HEADER  equ STORAGE_HEADER_A
PACKAGE_LENGTH  equ PACKAGE_HEADER + 8
PACKAGE_CHECKSUM equ PACKAGE_HEADER + 10
PACKAGE_HEADER_CHECKSUM equ PACKAGE_HEADER + 12
PACKAGE_NAME    equ PACKAGE_HEADER + 16

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
    mov di, EDITOR_BUFFER
    mov byte [di], 0

    call storage_load
    xor bp, bp

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
    mov di, cmd_edit_file
    call prefix_equal
    jc command_edit_file

    mov si, SHELL_BUFFER
    mov di, cmd_run_file
    call prefix_equal
    jc command_run_file

    mov si, SHELL_BUFFER
    mov di, cmd_files
    call strings_equal
    jc command_files

    mov si, SHELL_BUFFER
    mov di, cmd_pkg_list
    call strings_equal
    jc command_pkg_list

    mov si, SHELL_BUFFER
    mov di, cmd_pkg_install
    call prefix_equal
    jc command_pkg_install

    mov si, SHELL_BUFFER
    mov di, cmd_pkg_remove
    call prefix_equal
    jc command_pkg_remove

    mov si, SHELL_BUFFER
    mov di, cmd_print_file
    call prefix_equal
    jc command_print_file

    mov si, SHELL_BUFFER
    mov di, cmd_printer
    call strings_equal
    jc command_printer

    mov si, SHELL_BUFFER
    mov di, cmd_printer_set
    call prefix_equal
    jc command_printer_set

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
    mov si, default_filename
    jmp command_edit_name

command_edit_file:
    ; prefix_equal leaves SI at the requested filename.
command_edit_name:
    call filename_normalize
    jc .valid
    mov si, invalid_filename
    call print_string
    jmp shell
.valid:
    call file_open
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
    cmp al, 19
    je .save
    cmp al, 18
    je .run
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

.save:
    call file_save
    mov byte [editor_status], 1
    jnc .saved
    mov byte [editor_status], 2
    cmp byte [save_error], 1
    jne .saved
    inc byte [editor_status]
.saved:
    call editor_redraw
    jmp .read

.run:
    call basic_run
    jc .package_exit
    call editor_redraw
    ; If Enter dismissed the output screen, ignore a following LF without
    ; changing the document. Any other key clears this state normally.
    mov bp, 1
    jmp .read

.package_exit:
    mov bx, cx
    jmp shell

.full:
    mov al, 7
    call serial_write
    jmp .read

.exit:
    mov bx, cx
    mov si, newline
    call print_string
    jmp shell

command_run_file:
    call filename_normalize
    jc .valid
    mov si, invalid_filename
    call print_string
    jmp shell
.valid:
    call file_open
    jnc .missing
    mov cx, bx
    call basic_run
    ; Ignore the LF that commonly follows an Enter used to dismiss BASIC.
    mov bp, 1
    jmp shell
.missing:
    mov si, file_not_found
    call print_string
    jmp shell

editor_redraw:
    mov si, clear_sequence
    call print_string
    mov si, editor_header
    call print_string
    mov si, current_filename
    call print_string
    mov si, editor_controls
    call print_string
    cmp byte [editor_status], 1
    jne .not_saved
    mov si, saved
    jmp .status
.not_saved:
    cmp byte [editor_status], 2
    jne .not_failed
    mov si, save_failed
    jmp .status
.not_failed:
    cmp byte [editor_status], 3
    jne .no_status
    mov si, storage_full
.status:
    call print_string
.no_status:
    mov si, newline
    call print_string
    mov si, EDITOR_BUFFER
    call print_string
    mov byte [editor_status], 0
    ret

command_files:
    cmp byte [FILES_HEADER + 6], 0
    jne .list
    mov si, no_files
    call print_string
    jmp shell
.list:
    mov si, files_heading
    call print_string
    xor ch, ch
    mov cl, [FILES_HEADER + 6]
    mov di, FILES_HEADER + FILE_ENTRIES
.next:
    mov si, di
    call print_string
    mov si, newline
    call print_string
    add di, FILE_ENTRY_SIZE
    loop .next
    jmp shell

command_pkg_list:
    mov byte [package_index], 0
    mov byte [package_count], 0
    mov di, HEADER_BACKUP
.scan:
    mov al, [package_index]
    call package_load_header
    jc .catalog_error
    test al, al
    jz .advance
    mov al, [package_count]
    cmp al, [package_index]
    jne .catalog_error
    mov si, PACKAGE_NAME
    mov cx, FILE_NAME_SIZE
    rep movsb
    inc byte [package_count]
.advance:
    inc byte [package_index]
    cmp byte [package_index], PACKAGE_COUNT_MAX
    jb .scan
.ready:
    cmp byte [package_count], 0
    je .none
    mov si, packages_heading
    call print_string
    xor ch, ch
    mov cl, [package_count]
    mov di, HEADER_BACKUP
.print:
    mov si, di
    call print_string
    mov si, newline
    call print_string
    add di, FILE_NAME_SIZE
    loop .print
    jmp shell
.none:
    mov si, no_packages
    call print_string
    jmp shell
.catalog_error:
    mov si, package_catalog_error
    call print_string
    jmp shell

command_pkg_install:
    call filename_normalize
    jc .valid_name
    mov si, invalid_filename
    call print_string
    jmp shell
.valid_name:
    call package_find
    jc .found_package
    test al, al
    jnz .catalog_error
    mov si, package_not_found
    call print_string
    jmp shell
.found_package:
    call file_find
    jnc .load
    mov si, package_already_installed
    call print_string
    jmp shell
.load:
    mov ax, [PACKAGE_LENGTH]
    mov [package_source_length], ax
    mov ax, [PACKAGE_CHECKSUM]
    mov [package_source_checksum], ax
    xor ah, ah
    mov al, [package_index]
    mov bl, PACKAGE_SLOT_SECTORS
    mul bl
    add al, PACKAGE_CATALOG_LBA + 1
    mov cl, al
    mov bx, EDITOR_BUFFER
    mov si, PACKAGE_SLOT_SECTORS - 1
    call storage_read_payload
    jc .failed
    mov si, EDITOR_BUFFER
    mov cx, [package_source_length]
    call storage_checksum
    cmp dx, [package_source_checksum]
    jne .failed
    mov di, EDITOR_BUFFER
    add di, [package_source_length]
    mov byte [di], 0
    mov byte [current_file_index], 0xff
    mov cx, [package_source_length]
    call file_save
    jnc .installed
    cmp byte [save_error], 1
    je .full
.failed:
    mov si, package_install_failed
    call print_string
    jmp shell
.full:
    mov si, storage_full
    call print_string
    jmp shell
.installed:
    mov si, package_installed
    call print_string
    jmp shell
.catalog_error:
    mov si, package_catalog_error
    call print_string
    jmp shell

command_pkg_remove:
    call filename_normalize
    jc .valid_name
    mov si, invalid_filename
    call print_string
    jmp shell
.valid_name:
    call file_find
    jc .installed
    mov si, package_not_installed
    call print_string
    jmp shell
.installed:
    mov [remove_index], al
    call file_remove
    jnc .removed
    mov si, package_remove_failed
    call print_string
    jmp shell
.removed:
    mov si, package_removed
    call print_string
    jmp shell

; Load the native network/print module only when it is needed, then give it the
; selected file directly from the verified in-memory snapshot. Its code occupies
; 1000h-4fffh and scratch space stays below 7000h, clear of the 7c00h stack.
command_print_file:
    call filename_normalize
    jc .valid_name
    mov si, invalid_filename
    call print_string
    jmp shell
.valid_name:
    call file_find
    jc .found
    mov si, file_not_found
    call print_string
    jmp shell
.found:
    mov [print_file_index], al
    mov ax, [di + FILE_LENGTH]
    mov [print_file_length], ax

    mov bx, PRINT_MODULE_ADDRESS
    mov cl, PRINT_MODULE_LBA
    mov si, PRINT_MODULE_SECTORS
    call storage_read_payload
    jnc .loaded
    mov si, print_module_error
    call print_string
    jmp shell
.loaded:
    cmp dword [PRINT_MODULE_SIGNATURE], 0x5058494e ; "NIXP"
    jne .bad_module
    cmp dword [PRINT_MODULE_SIGNATURE + 4], 0x544e4952 ; "RINT"
    jne .bad_module
    cmp byte [PRINT_MODULE_SIGNATURE + 8], '1'
    jne .bad_module
    mov ax, [PRINT_MODULE_CHECKSUM]
    mov [print_module_checksum], ax
    mov word [PRINT_MODULE_CHECKSUM], 0
    mov si, PRINT_MODULE_ADDRESS
    mov cx, PRINT_MODULE_SECTORS * 512
    call storage_checksum
    mov ax, [print_module_checksum]
    mov [PRINT_MODULE_CHECKSUM], ax
    cmp dx, ax
    jne .bad_module

    xor ah, ah
    mov al, [print_file_index]
    shl ax, 11
    mov si, FILES_DATA
    add si, ax
    mov cx, [print_file_length]
    mov di, current_filename
    mov bx, printer_ip
    mov dx, PRINT_MODULE_ADDRESS
    mov al, 3                  ; a stray RET remains fail-closed
    stc
    call dx
    jc .failed_status
    test al, al
    jz .queued
.failed_status:
    cmp al, 1
    je .unavailable
    cmp al, 2
    je .rejected
    mov si, print_failed
    jmp .report
.queued:
    mov si, print_queued
    jmp .report
.unavailable:
    mov si, printer_unavailable
    jmp .report
.rejected:
    mov si, printer_rejected
.report:
    call print_string
    jmp shell
.bad_module:
    mov si, print_module_error
    call print_string
    jmp shell

command_printer:
    mov si, printer_heading
    call print_string
    mov si, printer_ip
    call print_ipv4
    mov si, newline
    call print_string
    jmp shell

command_printer_set:
    call parse_ipv4
    jc .configured
    mov si, invalid_address
    call print_string
    jmp shell
.configured:
    mov ax, [printer_candidate]
    mov [printer_ip], ax
    mov ax, [printer_candidate + 2]
    mov [printer_ip + 2], ax
    mov si, printer_configured
    call print_string
    jmp shell

; Carry is set only for exactly four decimal octets in the range 0-255.
; Parsing into a temporary keeps the active target unchanged on bad input.
parse_ipv4:
    push ax
    push bx
    push cx
    push dx
    push di
    mov di, printer_candidate
    mov cx, 4
.octet:
    xor bx, bx
    xor dx, dx
.digit:
    mov al, [si]
    cmp al, '0'
    jb .octet_end
    cmp al, '9'
    ja .octet_end
    cmp dh, 3
    jae .invalid
    mov ax, bx
    shl bx, 1
    shl ax, 3
    add bx, ax
    xor ah, ah
    mov al, [si]
    sub al, '0'
    add bx, ax
    cmp bx, 255
    ja .invalid
    inc si
    inc dh
    jmp .digit
.octet_end:
    test dh, dh
    jz .invalid
    mov [di], bl
    inc di
    dec cx
    jz .last
    cmp byte [si], '.'
    jne .invalid
    inc si
    jmp .octet
.last:
    cmp byte [si], 0
    jne .invalid
    stc
    jmp .restore
.invalid:
    clc
.restore:
    pop di
    pop dx
    pop cx
    pop bx
    pop ax
    ret

print_ipv4:
    mov cx, 4
.octet:
    push cx
    xor ax, ax
    mov al, [si]
    inc si
    call print_unsigned
    pop cx
    dec cx
    jz .done
    mov al, '.'
    call serial_write
    jmp .octet
.done:
    ret

print_unsigned:
    test ax, ax
    jnz .divide
    mov al, '0'
    call serial_write
    ret
.divide:
    xor cx, cx
    mov bx, 10
.next_digit:
    xor dx, dx
    div bx
    push dx
    inc cx
    test ax, ax
    jnz .next_digit
.write_digit:
    pop ax
    add al, '0'
    call serial_write
    loop .write_digit
    ret

; Normalize a short filename from DS:SI into current_filename. Names are
; case-insensitive and contain 1-12 letters, digits, dots, underscores, or
; hyphens. Carry is clear for an invalid or overlong name.
filename_normalize:
    push ax
    push cx
    push di
    push si
    xor ax, ax
    mov di, current_filename
    mov cx, FILE_NAME_SIZE
    rep stosb
    pop si
    xor cx, cx
    mov di, current_filename
.next:
    lodsb
    test al, al
    jz .done
    cmp cx, FILE_NAME_SIZE - 1
    jae .invalid
    cmp al, 'a'
    jb .not_lower
    cmp al, 'z'
    ja .not_lower
    sub al, 'a' - 'A'
.not_lower:
    cmp al, 'A'
    jb .not_letter
    cmp al, 'Z'
    jbe .store
.not_letter:
    cmp al, '0'
    jb .punctuation
    cmp al, '9'
    jbe .store
.punctuation:
    cmp al, '.'
    je .store
    cmp al, '_'
    je .store
    cmp al, '-'
    jne .invalid
.store:
    stosb
    inc cx
    jmp .next
.done:
    test cx, cx
    jz .invalid
    mov byte [di], 0
    stc
    jmp .restore
.invalid:
    clc
.restore:
    pop di
    pop cx
    pop ax
    ret

; Find current_filename in the compact directory. Carry is set with AL holding
; its zero-based slot and DI pointing at the entry.
file_find:
    mov byte [find_index], 0
    mov di, FILES_HEADER + FILE_ENTRIES
.next:
    mov al, [find_index]
    cmp al, [FILES_HEADER + 6]
    jae .missing
    mov bx, di
    mov si, current_filename
    call strings_equal
    jc .found
    mov di, bx
    add di, FILE_ENTRY_SIZE
    inc byte [find_index]
    jmp .next
.found:
    mov di, bx
    mov al, [find_index]
    stc
    ret
.missing:
    clc
    ret

; Open a saved file into the editor or start a blank unsaved buffer. BX returns
; the length and carry distinguishes an existing file from a new one.
file_open:
    call file_find
    jnc .new
    mov [current_file_index], al
    mov bx, [di + FILE_LENGTH]
    xor ah, ah
    shl ax, 11
    mov si, FILES_DATA
    add si, ax
    mov cx, bx
    mov di, EDITOR_BUFFER
    rep movsb
    mov byte [di], 0
    stc
    ret
.new:
    mov byte [current_file_index], 0xff
    xor bx, bx
    mov byte [EDITOR_BUFFER], 0
    clc
    ret

; Read and validate package slot AL. AL returns zero for the first empty slot or
; one for a valid package; carry reports an unreadable or malformed header.
package_load_header:
    push bx
    push cx
    push dx
    push si
    push di
    mov [package_index], al
    xor ah, ah
    mov bl, PACKAGE_SLOT_SECTORS
    mul bl
    add ax, PACKAGE_CATALOG_LBA
    mov cl, al
    mov bx, PACKAGE_HEADER
    mov al, 1
    call disk_read
    jc .invalid
    cmp byte [PACKAGE_HEADER], 0
    jne .present
    mov dl, cl
    mov si, PACKAGE_HEADER
    mov cx, 512
.empty_header:
    lodsb
    test al, al
    jnz .invalid
    loop .empty_header
    mov cl, dl
    inc cl
    mov bx, EDITOR_BUFFER
    mov si, PACKAGE_SLOT_SECTORS - 1
    call storage_read_payload
    jc .invalid
    mov si, EDITOR_BUFFER
    mov cx, EDITOR_CAPACITY
.empty_payload:
    lodsb
    test al, al
    jnz .invalid
    loop .empty_payload
    xor al, al
    clc
    jmp .restore
.present:
    call package_header_valid
    jc .invalid
    mov al, 1
    clc
    jmp .restore
.invalid:
    mov al, 2
    stc
.restore:
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    ret

; Validate the fixed header, its CRC, canonical padded filename, and every
; reserved byte. The source CRC is checked separately after payload loading.
package_header_valid:
    cmp dword [PACKAGE_HEADER], 0x5058494e ; "NIXP"
    jne .invalid
    cmp dword [PACKAGE_HEADER + 4], 0x0031474b ; "KG1", NUL
    jne .invalid
    mov ax, [PACKAGE_HEADER_CHECKSUM]
    mov [package_header_checksum], ax
    mov word [PACKAGE_HEADER_CHECKSUM], 0
    mov si, PACKAGE_HEADER
    mov cx, 512
    call storage_checksum
    mov ax, [package_header_checksum]
    mov [PACKAGE_HEADER_CHECKSUM], ax
    cmp dx, ax
    jne .invalid
    cmp word [PACKAGE_LENGTH], 0
    je .invalid
    cmp word [PACKAGE_LENGTH], EDITOR_CAPACITY - 1
    ja .invalid
    cmp word [PACKAGE_HEADER + 14], 0
    jne .invalid

    mov si, PACKAGE_NAME
    mov cx, FILE_NAME_SIZE
    xor bx, bx
.name:
    lodsb
    dec cx
    test al, al
    jz .padding
    cmp bl, FILE_NAME_SIZE - 1
    jae .invalid
    cmp al, 'A'
    jb .not_letter
    cmp al, 'Z'
    jbe .name_byte
.not_letter:
    cmp al, '0'
    jb .punctuation
    cmp al, '9'
    jbe .name_byte
.punctuation:
    cmp al, '.'
    je .name_byte
    cmp al, '_'
    je .name_byte
    cmp al, '-'
    jne .invalid
.name_byte:
    inc bx
    test cx, cx
    jnz .name
    jmp .invalid
.padding:
    cmp bx, 5
    jb .invalid
    mov di, PACKAGE_NAME
    add di, bx
    cmp dword [di - 4], 0x5341422e ; ".BAS"
    jne .invalid
.padding_byte:
    jcxz .reserved
    lodsb
    test al, al
    jnz .invalid
    dec cx
    jmp .padding_byte
.reserved:
    mov si, PACKAGE_HEADER + 16 + FILE_NAME_SIZE
    mov cx, 512 - 16 - FILE_NAME_SIZE
.reserved_byte:
    lodsb
    test al, al
    jnz .invalid
    loop .reserved_byte
    clc
    ret
.invalid:
    stc
    ret

; Find current_filename in the checked contiguous catalog. Carry means found.
; On a miss AL=0; an invalid catalog returns AL=1 with carry clear.
package_find:
    mov byte [package_index], 0
    mov byte [package_count], 0
    mov byte [package_match], 0xff
.next:
    mov al, [package_index]
    call package_load_header
    jc .catalog_error
    test al, al
    jz .advance
    mov al, [package_count]
    cmp al, [package_index]
    jne .catalog_error
    inc byte [package_count]
    mov si, current_filename
    mov di, PACKAGE_NAME
    call strings_equal
    jnc .advance
    cmp byte [package_match], 0xff
    jne .catalog_error
    mov al, [package_index]
    mov [package_match], al
.advance:
    inc byte [package_index]
    cmp byte [package_index], PACKAGE_COUNT_MAX
    jb .next
    mov al, [package_match]
    cmp al, 0xff
    je .missing
    call package_load_header
    jc .catalog_error
    stc
    ret
.missing:
    xor ax, ax
    clc
    ret
.catalog_error:
    mov al, 1
    clc
    ret
; Initialize an empty in-memory directory and all eight fixed-size file slots.
files_initialize:
    push ax
    push cx
    push di
    xor ax, ax
    mov di, FILES_HEADER
    mov cx, 256
    rep stosw
    mov dword [FILES_HEADER], STORAGE_MAGIC
    mov di, FILES_DATA
    mov cx, FILE_COUNT_MAX * (EDITOR_CAPACITY / 2)
    rep stosw
    mov byte [current_file_index], 0xff
    pop di
    pop cx
    pop ax
    ret

; Read both snapshot headers, try the newest structurally valid snapshot first,
; and fall back to the other copy if any named file has a damaged payload.
storage_load:
    call files_initialize
    mov byte [active_slot], 0xff
    mov word [active_generation], 0
    mov byte [slot_a_valid], 0
    mov byte [slot_b_valid], 0

    mov bx, STORAGE_HEADER_A
    mov cl, STORAGE_LBA_A
    mov al, 1
    call disk_read
    jc .read_b
    mov si, STORAGE_HEADER_A
    call storage_header_valid
    jc .read_b
    inc byte [slot_a_valid]

.read_b:
    mov bx, STORAGE_HEADER_B
    mov cl, STORAGE_LBA_B
    mov al, 1
    call disk_read
    jc .choose
    mov si, STORAGE_HEADER_B
    call storage_header_valid
    jc .choose
    inc byte [slot_b_valid]

.choose:
    cmp byte [slot_a_valid], 0
    je .only_b
    cmp byte [slot_b_valid], 0
    je .only_a
    mov ax, [STORAGE_HEADER_A + 4]
    sub ax, [STORAGE_HEADER_B + 4]
    cmp ax, 0x8000
    jbe .a_first
.b_first:
    call storage_try_b
    jnc .loaded_b
    call storage_try_a
    jnc .loaded_a
    jmp .empty
.a_first:
    call storage_try_a
    jnc .loaded_a
    call storage_try_b
    jnc .loaded_b
    jmp .empty
.only_a:
    call storage_try_a
    jnc .loaded_a
    jmp .empty
.only_b:
    cmp byte [slot_b_valid], 0
    je .empty
    call storage_try_b
    jnc .loaded_b
    jmp .empty
.loaded_a:
    mov byte [active_slot], 0
    mov ax, [STORAGE_HEADER_A + 4]
    jmp .loaded
.loaded_b:
    mov byte [active_slot], 1
    mov ax, [STORAGE_HEADER_B + 4]
.loaded:
    mov [active_generation], ax
    xor bx, bx
    ret
.empty:
    call files_initialize
    xor bx, bx
    ret

storage_try_a:
    mov di, STORAGE_HEADER_A
    mov cl, STORAGE_LBA_A + 1
    jmp storage_try_slot

storage_try_b:
    mov di, STORAGE_HEADER_B
    mov cl, STORAGE_LBA_B + 1

; Load either a NIX3 multi-file snapshot or a legacy NIX2 document. Legacy
; records are imported in memory as UNTITLED.TXT and converted on the next save.
storage_try_slot:
    cmp dword [di], LEGACY_STORAGE_MAGIC
    je .legacy
    mov bx, FILES_DATA
    mov si, FILES_DATA_SECTORS
    call storage_read_payload
    jc .invalid
    call storage_validate_files
    jc .invalid
    mov si, di
    mov di, FILES_HEADER
    mov cx, 256
    rep movsw
    clc
    ret
.legacy:
    mov bx, FILES_DATA
    mov si, 4
    call storage_read_payload
    jc .invalid
    mov cx, [di + 6]
    mov si, FILES_DATA
    call storage_checksum
    cmp dx, [di + 8]
    jne .invalid
    call storage_import_legacy
    clc
    ret
.invalid:
    stc
    ret

; Validate a candidate NIX3 snapshot's entry names, lengths, and per-file CRCs.
; DI remains the candidate header address for the caller.
storage_validate_files:
    mov [validation_header], di
    mov byte [validation_index], 0
.next:
    mov al, [validation_index]
    cmp al, [di + 6]
    jae .valid
    call storage_candidate_entry
    cmp byte [bx], 0
    je .invalid
    push bx
    mov si, bx
    mov cx, FILE_NAME_SIZE
.name:
    lodsb
    test al, al
    jz .name_valid
    loop .name
    pop bx
    jmp .invalid
.name_valid:
    pop bx
    cmp word [bx + FILE_LENGTH], EDITOR_CAPACITY - 1
    ja .invalid
    mov al, [validation_index]
    call storage_data_address
    mov cx, [bx + FILE_LENGTH]
    call storage_checksum
    cmp dx, [bx + FILE_CHECKSUM]
    jne .invalid
    inc byte [validation_index]
    mov di, [validation_header]
    jmp .next
.valid:
    mov di, [validation_header]
    clc
    ret
.invalid:
    mov di, [validation_header]
    stc
    ret

; Return BX pointing at entry AL in the candidate header saved above.
storage_candidate_entry:
    push ax
    xor ah, ah
    mov bl, FILE_ENTRY_SIZE
    mul bl
    mov bx, ax
    add bx, [validation_header]
    add bx, FILE_ENTRIES
    pop ax
    ret

; Return SI pointing at fixed data slot AL.
storage_data_address:
    xor ah, ah
    shl ax, 11
    mov si, FILES_DATA
    add si, ax
    ret

; A header is valid only if its versioned bounds and header CRC match.
storage_header_valid:
    cmp dword [si], LEGACY_STORAGE_MAGIC
    je .legacy
    cmp dword [si], STORAGE_MAGIC
    jne .invalid
    cmp byte [si + 6], FILE_COUNT_MAX
    ja .invalid
    mov di, si
    mov cx, FILES_HEADER_CRC
    call storage_checksum
    cmp dx, [di + FILES_HEADER_CRC]
    jne .invalid
    clc
    ret
.legacy:
    cmp word [si + 6], EDITOR_CAPACITY - 1
    ja .invalid
    mov di, si
    mov cx, 10
    call storage_checksum
    cmp dx, [di + 10]
    jne .invalid
    clc
    ret
.invalid:
    stc
    ret

storage_import_legacy:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    mov ax, [di + 4]
    mov bx, [di + 6]
    mov dx, [di + 8]
    mov di, FILES_HEADER
    xor cx, cx
    mov cx, 256
    xor si, si
    xchg ax, si
    xor ax, ax
    rep stosw
    xchg ax, si
    mov dword [FILES_HEADER], STORAGE_MAGIC
    mov [FILES_HEADER + 4], ax
    mov byte [FILES_HEADER + 6], 1
    mov si, default_filename
    mov di, FILES_HEADER + FILE_ENTRIES
    mov cx, FILE_NAME_SIZE
    rep movsb
    mov [FILES_HEADER + FILE_ENTRIES + FILE_LENGTH], bx
    mov [FILES_HEADER + FILE_ENTRIES + FILE_CHECKSUM], dx
    mov di, FILES_DATA
    add di, bx
    mov cx, FILE_COUNT_MAX * EDITOR_CAPACITY
    sub cx, bx
    xor ax, ax
    rep stosb
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

; Save the editor into its named fixed slot. Directory and data changes are
; rolled back in RAM if the inactive disk snapshot cannot be committed.
file_save:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    push bp
    mov [save_length], cx
    mov byte [save_error], 0
    mov al, [current_file_index]
    mov [save_old_index], al
    mov si, FILES_HEADER
    mov di, HEADER_BACKUP
    mov cx, 256
    rep movsw

    call file_find
    jc .have_slot
    mov al, [FILES_HEADER + 6]
    cmp al, FILE_COUNT_MAX
    jae .full
    mov [save_file_index], al
    inc byte [FILES_HEADER + 6]
    call files_entry_address
    push di
    xor ax, ax
    mov cx, FILE_ENTRY_SIZE
    rep stosb
    pop di
    mov si, current_filename
    mov cx, FILE_NAME_SIZE
    rep movsb
    jmp .backup_data
.have_slot:
    mov [save_file_index], al
.backup_data:
    mov al, [save_file_index]
    call files_data_address
    mov si, di
    mov di, FILE_BACKUP
    mov cx, EDITOR_CAPACITY / 2
    rep movsw

    mov al, [save_file_index]
    call files_data_address
    mov si, EDITOR_BUFFER
    mov cx, [save_length]
    rep movsb
    mov cx, EDITOR_CAPACITY
    sub cx, [save_length]
    xor ax, ax
    rep stosb

    mov al, [save_file_index]
    call files_entry_address
    mov ax, [save_length]
    mov [di + FILE_LENGTH], ax
    mov cx, ax
    mov si, EDITOR_BUFFER
    call storage_checksum
    mov [di + FILE_CHECKSUM], dx
    call storage_save
    jc .disk_failed
    mov al, [save_file_index]
    mov [current_file_index], al
    clc
    jmp .restore
.disk_failed:
    mov byte [save_error], 2
    call file_save_rollback
    stc
    jmp .restore
.full:
    mov byte [save_error], 1
    stc
.restore:
    pop bp
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

file_save_rollback:
    mov si, HEADER_BACKUP
    mov di, FILES_HEADER
    mov cx, 256
    rep movsw
    mov al, [save_file_index]
    call files_data_address
    mov si, FILE_BACKUP
    mov cx, EDITOR_CAPACITY / 2
    rep movsw
    mov al, [save_old_index]
    mov [current_file_index], al
    ret

; Delete remove_index from the compact directory and data array, then commit the
; complete result through the normal alternating-snapshot transaction. Keep the
; removed slot and directory in RAM so a failed write never depends on disk
; reads to restore the live filesystem.
file_remove:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    push bp
    mov si, FILES_HEADER
    mov di, HEADER_BACKUP
    mov cx, 256
    rep movsw
    mov al, [remove_index]
    call files_data_address
    mov si, di
    mov di, FILE_BACKUP
    mov cx, EDITOR_CAPACITY / 2
    rep movsw

    mov al, [FILES_HEADER + 6]
    dec al
    mov [remove_last], al
    mov al, [remove_index]
    mov [remove_cursor], al
.compact:
    mov al, [remove_cursor]
    cmp al, [remove_last]
    jae .clear_last
    inc al
    call files_entry_address
    mov si, di
    mov al, [remove_cursor]
    call files_entry_address
    mov cx, FILE_ENTRY_SIZE
    rep movsb

    mov al, [remove_cursor]
    inc al
    call files_data_address
    mov si, di
    mov al, [remove_cursor]
    call files_data_address
    mov cx, EDITOR_CAPACITY / 2
    rep movsw
    inc byte [remove_cursor]
    jmp .compact
.clear_last:
    mov al, [remove_last]
    call files_entry_address
    xor ax, ax
    mov cx, FILE_ENTRY_SIZE
    rep stosb
    mov al, [remove_last]
    call files_data_address
    xor ax, ax
    mov cx, EDITOR_CAPACITY / 2
    rep stosw
    dec byte [FILES_HEADER + 6]
    call storage_save
    jc .failed
    mov byte [current_file_index], 0xff
    clc
    jmp .restore
.failed:
    mov al, [remove_last]
.rollback_data:
    cmp al, [remove_index]
    jbe .restore_removed
    mov dl, al
    dec al
    call files_data_address
    mov si, di
    mov al, dl
    call files_data_address
    mov cx, EDITOR_CAPACITY / 2
    rep movsw
    mov al, dl
    dec al
    jmp .rollback_data
.restore_removed:
    mov al, [remove_index]
    call files_data_address
    mov si, FILE_BACKUP
    mov cx, EDITOR_CAPACITY / 2
    rep movsw
    mov si, HEADER_BACKUP
    mov di, FILES_HEADER
    mov cx, 256
    rep movsw
    stc
.restore:
    pop bp
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

; Return DI pointing at directory entry AL in the active header.
files_entry_address:
    xor ah, ah
    mov bl, FILE_ENTRY_SIZE
    mul bl
    mov di, FILES_HEADER + FILE_ENTRIES
    add di, ax
    ret

; Return DI pointing at fixed data slot AL.
files_data_address:
    xor ah, ah
    shl ax, 11
    mov di, FILES_DATA
    add di, ax
    ret

; Commit the complete directory and eight data slots into the snapshot opposite
; the active one. The zero header invalidates the target before its data moves;
; the verified NIX3 header is written last as the atomic commit record.
storage_save:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    push bp
    cmp byte [active_slot], 0
    je .target_b
    mov word [save_header], STORAGE_HEADER_A
    mov byte [save_header_sector], STORAGE_LBA_A
    mov byte [save_target], 0
    jmp .invalidate
.target_b:
    mov word [save_header], STORAGE_HEADER_B
    mov byte [save_header_sector], STORAGE_LBA_B
    mov byte [save_target], 1
.invalidate:
    mov di, [save_header]
    xor ax, ax
    mov cx, 256
    rep stosw
    mov bx, [save_header]
    mov cl, [save_header_sector]
    mov al, 1
    call disk_write
    jc .failed

    mov si, FILES_HEADER
    mov di, [save_header]
    mov cx, 256
    rep movsw
    mov dword [di - 512], STORAGE_MAGIC
    mov ax, [active_generation]
    cmp byte [active_slot], 0xff
    je .first_generation
    inc ax
.first_generation:
    mov bx, [save_header]
    mov [bx + 4], ax
    mov si, bx
    mov cx, FILES_HEADER_CRC
    call storage_checksum
    mov [bx + FILES_HEADER_CRC], dx

    mov bx, FILES_DATA
    mov cl, [save_header_sector]
    inc cl
    mov si, FILES_DATA_SECTORS
    call storage_write_payload
    jc .failed
    mov bx, [save_header]
    mov cl, [save_header_sector]
    mov al, 1
    call disk_write
    jc .failed

    mov si, [save_header]
    mov di, FILES_HEADER
    mov cx, 256
    rep movsw
    mov al, [save_target]
    mov [active_slot], al
    mov bx, [save_header]
    mov ax, [bx + 4]
    mov [active_generation], ax
    clc
    jmp .restore
.failed:
    stc
.restore:
    pop bp
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

; Transfer SI payload sectors individually. CL is a zero-based floppy LBA.
storage_read_payload:
.next:
    mov al, 1
    call disk_read
    jc .failed
    add bx, 512
    inc cl
    dec si
    jnz .next
    clc
    ret
.failed:
    stc
    ret

storage_write_payload:
.next:
    mov al, 1
    call disk_write
    jc .failed
    add bx, 512
    inc cl
    dec si
    jnz .next
    clc
    ret
.failed:
    stc
    ret

disk_read:
    mov ah, 0x02
    jmp disk_transfer

disk_write:
    mov ah, 0x03

; Convert zero-based LBA in CL to 1.44MB-floppy CHS (18 sectors, two heads)
; and transfer AL sectors with three retries.
disk_transfer:
    mov [disk_request], ax
    mov [disk_buffer], bx
    mov [disk_sector], cl
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    push bp
    mov bp, 3
.retry:
    xor ah, ah
    mov dl, [boot_drive]
    int 0x13
    xor ax, ax
    mov al, [disk_sector]
    mov bl, 18
    div bl
    mov cl, ah
    inc cl
    xor ah, ah
    mov dh, al
    and dh, 1
    shr al, 1
    mov ch, al
    mov ax, [disk_request]
    mov bx, [disk_buffer]
    mov dl, [boot_drive]
    int 0x13
    jnc .succeeded
    dec bp
    jnz .retry
    stc
    jmp .restore
.succeeded:
    clc
.restore:
    pop bp
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

; Return the CRC-16/CCITT-FALSE checksum of CX bytes at DS:SI in DX without
; changing the caller's document position or length.
storage_checksum:
    push ax
    push cx
    push si
    push bp
    mov dx, 0xffff
.next:
    jcxz .done
    lodsb
    xor dh, al
    mov bp, 8
.bit:
    shl dx, 1
    jnc .no_xor
    xor dx, 0x1021
.no_xor:
    dec bp
    jnz .bit
    loop .next
.done:
    pop bp
    pop si
    pop cx
    pop ax
    ret

; Demand-load the checked BASIC runtime into the same 1000h-4fffh window used
; by the printer module. The editor remains resident at 9c00h, and the module
; ABI preserves the editor's position and document length for redraw.
basic_run:
    mov [basic_source_length], cx
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    push bp

    mov bx, BASIC_MODULE_ADDRESS
    mov cl, BASIC_MODULE_LBA
    mov si, BASIC_MODULE_SECTORS
    call storage_read_payload
    jc .unavailable
    cmp dword [BASIC_MODULE_SIGNATURE], 0x4258494e ; "NIXB"
    jne .unavailable
    cmp dword [BASIC_MODULE_SIGNATURE + 4], 0x43495341 ; "ASIC"
    jne .unavailable
    cmp byte [BASIC_MODULE_SIGNATURE + 8], '1'
    jne .unavailable
    mov ax, [BASIC_MODULE_CHECKSUM]
    mov [basic_module_checksum], ax
    mov word [BASIC_MODULE_CHECKSUM], 0
    mov si, BASIC_MODULE_ADDRESS
    mov cx, BASIC_MODULE_SECTORS * 512
    call storage_checksum
    mov ax, [basic_module_checksum]
    mov [BASIC_MODULE_CHECKSUM], ax
    cmp dx, ax
    jne .unavailable

    mov si, EDITOR_BUFFER
    mov cx, [basic_source_length]
    mov dx, BASIC_MODULE_ADDRESS
    call dx
    jmp .restore
.unavailable:
    mov si, basic_module_error
    call print_string
    call serial_read
    cmp al, ESCAPE_KEY
    je .escaped
    clc
    jmp .restore
.escaped:
    call serial_discard_input
    mov si, newline
    call print_string
    stc
.restore:
    pop bp
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
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

; Consume the remainder of the terminal key sequence that began with Escape.
; Two tick transitions guarantee one complete BIOS tick for its ANSI suffix.
serial_discard_input:
    push ax
    push bx
    push cx
    push dx
    push bp
    mov ah, 0
    int 0x1a
    mov bx, dx
    mov bp, 2
.next:
    mov dx, COM1 + 5
    in al, dx
    test al, 1
    jz .tick
    mov dx, COM1
    in al, dx
.tick:
    mov ah, 0
    int 0x1a
    cmp dx, bx
    je .next
    mov bx, dx
    dec bp
    jnz .next
.drain_ready:
    mov dx, COM1 + 5
    in al, dx
    test al, 1
    jz .done
    mov dx, COM1
    in al, dx
    jmp .drain_ready
.done:
    pop bp
    pop dx
    pop cx
    pop bx
    pop ax
    ret

banner         db 27, '[2J', 27, '[H', 'Nixodria OS', 13, 10, 'Type help.', 13, 10, 0
prompt         db 'nix> ', 0
newline        db 13, 10, 0
erase          db 8, ' ', 8, 0
unknown        db 'Unknown command.', 13, 10, 0
help_text      db 'help files edit [filename] run <filename>', 13, 10
               db 'pkg list pkg install <filename> pkg remove <filename>', 13, 10
               db 'print <filename> printer [IPv4] clear echo <text> reboot halt', 13, 10, 0
editor_header  db 'Nixodria Editor: ', 0
editor_controls db 13, 10, 'Ctrl-S save | Ctrl-R run | Ctrl-X exit | Ctrl-L clear', 13, 10, 0
clear_sequence db 27, '[2J', 27, '[H', 0
saved          db 'Saved.', 13, 10, 0
save_failed    db 'Save failed.', 13, 10, 0
storage_full   db 'Storage full.', 13, 10, 0
invalid_filename db 'Invalid filename.', 13, 10, 0
files_heading  db 'Files:', 13, 10, 0
no_files       db 'No files.', 13, 10, 0
file_not_found db 'File not found.', 13, 10, 0
packages_heading db 'Packages:', 13, 10, 0
no_packages    db 'No packages.', 13, 10, 0
package_installed db 'Package installed.', 13, 10, 0
package_removed db 'Package removed.', 13, 10, 0
package_already_installed db 'Package already installed.', 13, 10, 0
package_not_found db 'Package not found.', 13, 10, 0
package_not_installed db 'Package is not installed.', 13, 10, 0
package_catalog_error db 'Package catalog unavailable.', 13, 10, 0
package_install_failed db 'Package install failed.', 13, 10, 0
package_remove_failed db 'Package remove failed.', 13, 10, 0
printer_heading db 'Printer: ', 0
printer_configured db 'Printer configured.', 13, 10, 0
invalid_address db 'Invalid IPv4 address.', 13, 10, 0
print_queued   db 'Print job queued.', 13, 10, 0
printer_unavailable db 'Printer unavailable.', 13, 10, 0
printer_rejected db 'Printer rejected job.', 13, 10, 0
print_failed   db 'Print failed.', 13, 10, 0
print_module_error db 'Printer module unavailable.', 13, 10, 0
basic_module_error db 27, '[2J', 27, '[H', 'BASIC runtime unavailable. Press any key.', 0
rebooting      db 'Rebooting...', 13, 10, 0
halted         db 'Halted.', 13, 10, 0
cmd_help       db 'help', 0
cmd_edit       db 'edit', 0
cmd_edit_file  db 'edit ', 0
cmd_run_file   db 'run ', 0
cmd_files      db 'files', 0
cmd_pkg_list   db 'pkg list', 0
cmd_pkg_install db 'pkg install ', 0
cmd_pkg_remove db 'pkg remove ', 0
cmd_print_file db 'print ', 0
cmd_printer    db 'printer', 0
cmd_printer_set db 'printer ', 0
cmd_clear      db 'clear', 0
cmd_reboot     db 'reboot', 0
cmd_halt       db 'halt', 0
cmd_echo       db 'echo ', 0
default_filename db 'UNTITLED.TXT', 0
current_filename times FILE_NAME_SIZE db 0
editor_status  db 0
current_file_index db 0xff
find_index     db 0
active_slot    db 0xff
active_generation dw 0
slot_a_valid   db 0
slot_b_valid   db 0
save_header    dw 0
save_length    dw 0
save_header_sector db 0
save_target    db 0
save_error     db 0
save_old_index db 0xff
save_file_index db 0
validation_header dw 0
validation_index db 0
package_index  db 0
package_count  db 0
package_match  db 0xff
package_header_checksum dw 0
package_source_length dw 0
package_source_checksum dw 0
remove_index   db 0
remove_last    db 0
remove_cursor  db 0
disk_request   dw 0
disk_buffer    dw 0
disk_sector    db 0
printer_ip     db 192, 168, 40, 220
printer_candidate times 4 db 0
print_file_index db 0
print_file_length dw 0
print_module_checksum dw 0
basic_module_checksum dw 0
basic_source_length dw 0

; Keep executable code inside the sectors loaded by the first-stage loader.
times (1 + KERNEL_SECTORS) * 512 - ($ - $$) db 0

; Pad the base image. The build overlays the native modules and package catalog
; after both blank recovery snapshots without changing their LBAs.
times IMAGE_SECTORS * 512 - ($ - $$) db 0
