bits 16
org 0x1000

; Demand-loaded Nixodria BASIC interpreter.
;
; Entry ABI (near call to 0000:1000):
;   DS = ES = 0
;   SI -> NUL-terminated BASIC source, CX = source length (0..2047)
; Return:
;   BP, SP, DS, and ES are preserved. Other registers are caller-clobbered.
;
; The language has 26 signed-word scalar variables and one DIM-declared
; signed-word array with an inclusive maximum index from 0 through 255. The
; GOSUB stack and expression recursion are deliberately bounded. WAIT is the
; cooperative yield point: a successful positive wait resets the 10,000
; statement watchdog, while a loop that only polls KEY still terminates.

jmp near basic_entry
module_signature db 'NIXBASIC1'
; tools/build_image.py writes the CRC-16/CCITT-FALSE of this complete 8 KiB
; module here after calculating it with these two bytes set to zero.
module_checksum dw 0
%if module_signature - $$ != 3
    %error "BASIC module signature moved"
%endif
%if module_checksum - $$ != 12
    %error "BASIC module checksum moved"
%endif

COM1                    equ 0x3f8
MAX_SOURCE_LENGTH       equ 2047
BASIC_ARRAY             equ 0x3000
BASIC_ARRAY_WORDS       equ 256
GOSUB_LIMIT             equ 16
EXPRESSION_DEPTH_LIMIT  equ 16
WATCHDOG_RELOAD         equ 10001
TICKS_PER_DAY_LOW       equ 0x00b0
TICKS_PER_DAY_HIGH      equ 0x0018

basic_entry:
    push bp
    push ds
    push es
    xor ax, ax
    mov ds, ax
    mov es, ax
    cld

    mov [program_start], si
    mov byte [source_valid], 1
    cmp cx, MAX_SOURCE_LENGTH
    ja .bad_source
    mov ax, si
    add ax, cx
    jc .bad_source
    mov [program_end], ax
    mov bx, ax
    cmp byte [bx], 0
    je .source_checked
.bad_source:
    mov byte [source_valid], 0
.source_checked:

    mov di, basic_vars
    xor ax, ax
    mov cx, 26
    rep stosw
    mov di, BASIC_ARRAY
    mov cx, BASIC_ARRAY_WORDS
    rep stosw
    mov byte [array_name], 0xff
    mov byte [array_max], 0
    mov byte [return_depth], 0
    mov byte [expression_depth], 0
    mov word [basic_steps], WATCHDOG_RELOAD
    mov word [basic_line], 0

    mov si, basic_output
    call print_string
    cmp byte [source_valid], 1
    jne .error
    mov si, [program_start]

.next_line:
    call basic_skip_spaces
    cmp byte [si], 0
    je .finished
    cmp byte [si], 13
    je .blank

    mov word [basic_line], 0
    call basic_parse_uint
    jnc .error
    mov [basic_line], ax
.execute_statement:
    dec word [basic_steps]
    jz .error
    call basic_skip_spaces

    mov di, basic_kw_print
    call basic_keyword
    jc .print
    mov di, basic_kw_let
    call basic_keyword
    jc .let
    mov di, basic_kw_if
    call basic_keyword
    jc .if
    mov di, basic_kw_goto
    call basic_keyword
    jc .goto
    mov di, basic_kw_gosub
    call basic_keyword
    jc .gosub
    mov di, basic_kw_return
    call basic_keyword
    jc .return
    mov di, basic_kw_dim
    call basic_keyword
    jc .dim
    mov di, basic_kw_cls
    call basic_keyword
    jc .cls
    mov di, basic_kw_key
    call basic_keyword
    jc .key
    mov di, basic_kw_wait
    call basic_keyword
    jc .wait
    mov di, basic_kw_timer
    call basic_keyword
    jc .timer
    mov di, basic_kw_rem
    call basic_keyword
    jc .rem
    mov di, basic_kw_end
    call basic_keyword
    jc .end
    ; LET is optional for both scalar and indexed assignments. Keywords are
    ; tested first so a statement such as DIM is never mistaken for scalar D.
    call basic_variable_address
    jnc .error
    jmp .assignment_target_ready

.next_statement:
    call basic_skip_spaces
    cmp byte [si], 0
    je .error
    cmp byte [si], 13
    je .error
    jmp .execute_statement

.continue:
    cmp byte [statement_separator], 0
    jne .next_statement
    jmp .next_line

.blank:
    call basic_next_line
    jmp .next_line

.print:
    call basic_skip_spaces
    cmp byte [si], '"'
    jne .print_number
    inc si
.print_text:
    lodsb
    test al, al
    jz .error
    cmp al, 13
    je .error
    cmp al, '"'
    je .print_text_done
    call serial_write
    jmp .print_text
.print_text_done:
    call basic_print_ending
    jnc .error
    jmp .printed
.print_number:
    call basic_expression
    jnc .error
    push ax
    call basic_print_ending
    pop ax
    jnc .error
    call basic_print_integer
.printed:
    cmp byte [print_suppressed], 0
    jne .continue
    push si
    mov si, newline
    call print_string
    pop si
    jmp .continue

.let:
    call basic_variable_address
    jnc .error
.assignment_target_ready:
    push di
    call basic_skip_spaces
    cmp byte [si], '='
    jne .let_error
    inc si
    call basic_expression
    pop di
    jnc .error
    push ax
    call basic_end_line
    pop ax
    jnc .error
    mov [di], ax
    jmp .continue
.let_error:
    pop di
    jmp .error

.dim:
    cmp byte [array_name], 0xff
    jne .error
    call basic_parse_letter
    jnc .error
    mov [array_candidate], al
    call basic_skip_spaces
    cmp byte [si], '('
    jne .error
    inc si
    call basic_expression
    jnc .error
    test ax, ax
    js .error
    cmp ax, BASIC_ARRAY_WORDS - 1
    ja .error
    mov [array_bound_candidate], al
    call basic_skip_spaces
    cmp byte [si], ')'
    jne .error
    inc si
    call basic_end_line
    jnc .error
    mov al, [array_candidate]
    mov [array_name], al
    mov al, [array_bound_candidate]
    mov [array_max], al
    jmp .continue

.goto:
    call basic_skip_spaces
    call basic_parse_uint
    jnc .error
    mov dx, ax
    call basic_end_line
    jnc .error
    call basic_find_line
    jnc .error
    jmp .next_line

.gosub:
    call basic_skip_spaces
    call basic_parse_uint
    jnc .error
    mov dx, ax
    call basic_end_line
    jnc .error
    cmp byte [return_depth], GOSUB_LIMIT
    jae .error
    xor bx, bx
    mov bl, [return_depth]
    shl bx, 1
    mov [basic_return_stack + bx], si
    mov ax, [basic_line]
    mov [basic_return_line + bx], ax
    mov bl, [return_depth]
    xor bh, bh
    mov al, [statement_separator]
    mov [basic_return_same_line + bx], al
    inc byte [return_depth]
    call basic_find_line
    jnc .error
    jmp .next_line

.return:
    call basic_end_line
    jnc .error
    cmp byte [return_depth], 0
    je .error
    dec byte [return_depth]
    xor bx, bx
    mov bl, [return_depth]
    shl bx, 1
    mov si, [basic_return_stack + bx]
    mov ax, [basic_return_line + bx]
    mov [basic_line], ax
    shr bx, 1
    cmp byte [basic_return_same_line + bx], 0
    jne .next_statement
    jmp .next_line

.if:
    call basic_expression
    jnc .error
    mov [basic_left], ax
    call basic_skip_spaces
    mov al, [si]
    cmp al, '='
    je .have_operator
    cmp al, '<'
    je .have_operator
    cmp al, '>'
    jne .error
.have_operator:
    mov [basic_operator], al
    inc si
    call basic_expression
    jnc .error
    mov dx, ax
    mov ax, [basic_left]
    xor bx, bx
    cmp byte [basic_operator], '='
    je .if_equal
    cmp byte [basic_operator], '<'
    je .if_less
    cmp ax, dx
    jle .condition_ready
    jmp .condition_true
.if_less:
    cmp ax, dx
    jge .condition_ready
    jmp .condition_true
.if_equal:
    cmp ax, dx
    jne .condition_ready
.condition_true:
    inc bx
.condition_ready:
    mov di, basic_kw_then
    call basic_keyword
    jnc .error
    call basic_skip_spaces
    call basic_parse_uint
    jnc .error
    mov dx, ax
    call basic_end_line
    jnc .error
    test bx, bx
    jz .continue
    call basic_find_line
    jnc .error
    jmp .next_line

.cls:
    call basic_end_line
    jnc .error
    push si
    mov si, clear_sequence
    call print_string
    pop si
    jmp .continue

.key:
    call basic_scalar_address
    jnc .error
    call basic_end_line
    jnc .error
    call serial_try_read
    jnc .no_key
    xor ah, ah
    jmp .key_store
.no_key:
    xor ax, ax
.key_store:
    ; A received byte is zero-extended; no-ready is the numeric value zero.
    mov [di], ax
    jmp .continue

.wait:
    call basic_expression
    jnc .error
    test ax, ax
    jle .error
    mov [wait_duration], ax
    call basic_end_line
    jnc .error
    call basic_wait
    mov word [basic_steps], WATCHDOG_RELOAD
    jmp .continue

.timer:
    call basic_scalar_address
    jnc .error
    call basic_end_line
    jnc .error
    push si
    push di
    mov ah, 0
    int 0x1a
    pop di
    pop si
    mov [di], dx
    jmp .continue

.rem:
    call basic_next_line
    jmp .next_line

.end:
    call basic_end_line
    jnc .error
.finished:
    mov si, basic_finished
    jmp .pause
.error:
    mov si, basic_error
    call print_string
    mov ax, [basic_line]
    call basic_print_unsigned
    mov si, basic_error_end
.pause:
    call print_string
    call serial_read

    pop es
    pop ds
    pop bp
    ret

; Carry is set when the lowercase keyword at DI matches SI case-insensitively.
; SI advances past the keyword only when the following byte is not part of an
; identifier, so statements and the MOD operator use the same matching rule.
basic_keyword:
    push bx
    mov bx, si
.compare:
    mov al, [di]
    test al, al
    jz .boundary
    mov ah, [si]
    or ah, 0x20
    cmp ah, al
    jne .different
    inc si
    inc di
    jmp .compare
.boundary:
    mov al, [si]
    mov ah, al
    or ah, 0x20
    cmp ah, 'a'
    jb .not_letter
    cmp ah, 'z'
    jbe .different
.not_letter:
    cmp al, '0'
    jb .matched
    cmp al, '9'
    jbe .different
    cmp al, '_'
    je .different
.matched:
    stc
    pop bx
    ret
.different:
    mov si, bx
    clc
    pop bx
    ret

basic_skip_spaces:
    cmp byte [si], ' '
    jne .done
    inc si
    jmp basic_skip_spaces
.done:
    ret

; Parse an unsigned decimal at SI into AX. Carry is clear on malformed input
; or a value outside the unsigned 16-bit range.
basic_parse_uint:
    cmp byte [si], '0'
    jb .invalid
    cmp byte [si], '9'
    ja .invalid
    xor ax, ax
.digit:
    cmp byte [si], '0'
    jb .valid
    cmp byte [si], '9'
    ja .valid
    mov cx, 10
    mul cx
    test dx, dx
    jnz .invalid
    xor dx, dx
    mov dl, [si]
    sub dl, '0'
    add ax, dx
    jc .invalid
    inc si
    jmp .digit
.valid:
    stc
    ret
.invalid:
    clc
    ret

; Standard arithmetic precedence: signed factors, then *, /, and MOD, then +
; and -. Bitwise AND is deliberately lower precedence than all arithmetic.
; Addition, subtraction, and multiplication retain the existing 16-bit wrap
; behavior. Division truncates toward zero; zero divisors and -32768 / -1 are
; runtime errors.
basic_expression:
    cmp byte [expression_depth], EXPRESSION_DEPTH_LIMIT
    jae .too_deep
    inc byte [expression_depth]
    call basic_sum
    jnc .invalid
    mov dx, ax
.and_operator:
    call basic_skip_spaces
    mov di, basic_kw_and
    call basic_keyword
    jnc .valid
    push dx
    call basic_sum
    mov bx, ax
    pop dx
    jnc .invalid
    and dx, bx
    jmp .and_operator
.valid:
    mov ax, dx
    dec byte [expression_depth]
    stc
    ret
.invalid:
    dec byte [expression_depth]
.too_deep:
    clc
    ret

basic_sum:
    call basic_term
    jnc .invalid
    mov dx, ax
.operator:
    call basic_skip_spaces
    cmp byte [si], '+'
    je .add
    cmp byte [si], '-'
    je .subtract
    mov ax, dx
    stc
    ret
.add:
    inc si
    push dx
    call basic_term
    pop dx
    jnc .invalid
    add dx, ax
    jmp .operator
.subtract:
    inc si
    push dx
    call basic_term
    pop dx
    jnc .invalid
    sub dx, ax
    jmp .operator
.invalid:
    clc
    ret

basic_term:
    call basic_factor
    jnc .invalid
    mov dx, ax
.operator:
    call basic_skip_spaces
    cmp byte [si], '*'
    je .multiply
    cmp byte [si], '/'
    je .divide
    mov di, basic_kw_mod
    call basic_keyword
    jc .modulo
    mov ax, dx
    stc
    ret
.multiply:
    inc si
    push dx
    call basic_factor
    mov bx, ax
    pop dx
    jnc .invalid
    mov ax, dx
    imul bx
    mov dx, ax
    jmp .operator
.divide:
    inc si
    push dx
    call basic_factor
    mov bx, ax
    pop dx
    jnc .invalid
    call basic_signed_divide
    jnc .invalid
    mov dx, ax
    jmp .operator
.modulo:
    push dx
    call basic_factor
    mov bx, ax
    pop dx
    jnc .invalid
    call basic_signed_divide
    jnc .invalid
    ; basic_signed_divide leaves the signed remainder in DX.
    jmp .operator
.invalid:
    clc
    ret

; Divide signed DX by signed BX. AX is the quotient and DX the remainder.
basic_signed_divide:
    test bx, bx
    jz .invalid
    mov ax, dx
    cmp ax, 0x8000
    jne .divide
    cmp bx, -1
    je .invalid
.divide:
    cwd
    idiv bx
    stc
    ret
.invalid:
    clc
    ret

; Parse one optional unary sign followed by a literal, variable/array element,
; or parenthesized expression.
basic_factor:
    call basic_skip_spaces
    xor bx, bx
    cmp byte [si], '-'
    je .negative
    cmp byte [si], '+'
    jne .primary
    inc si
    call basic_skip_spaces
    jmp .primary
.negative:
    inc si
    inc bx
    call basic_skip_spaces
.primary:
    push bx
    cmp byte [si], '('
    je .parenthesized
    cmp byte [si], '0'
    jb .variable
    cmp byte [si], '9'
    ja .variable
    call basic_parse_uint
    jnc .invalid_pop
    pop bx
    test bx, bx
    jnz .negative_literal
    cmp ax, 0x7fff
    ja .invalid
    stc
    ret
.negative_literal:
    cmp ax, 0x8000
    ja .invalid
    neg ax
    stc
    ret
.variable:
    call basic_variable_address
    jnc .invalid_pop
    mov ax, [di]
    pop bx
    test bx, bx
    jz .valid
    neg ax
.valid:
    stc
    ret
.parenthesized:
    inc si
    call basic_expression
    jnc .invalid_pop
    call basic_skip_spaces
    cmp byte [si], ')'
    jne .invalid_pop
    inc si
    pop bx
    test bx, bx
    jz .valid
    neg ax
    stc
    ret
.invalid_pop:
    pop bx
.invalid:
    clc
    ret

; Return the address of a case-insensitive scalar or array element in DI.
; The only indexed name accepted is the one established by DIM, and its signed
; index must remain within the declared inclusive maximum.
basic_variable_address:
    call basic_parse_letter
    jnc .invalid
    xor bx, bx
    mov bl, al
    call basic_skip_spaces
    cmp byte [si], '('
    jne .scalar
    cmp bl, [array_name]
    jne .invalid
    inc si
    push bx
    call basic_expression
    pop bx
    jnc .invalid
    call basic_skip_spaces
    cmp byte [si], ')'
    jne .invalid
    inc si
    test ax, ax
    js .invalid
    test ah, ah
    jnz .invalid
    cmp al, [array_max]
    ja .invalid
    xor ah, ah
    shl ax, 1
    mov di, BASIC_ARRAY
    add di, ax
    stc
    ret
.scalar:
    shl bx, 1
    mov di, basic_vars
    add di, bx
    stc
    ret
.invalid:
    clc
    ret

; Return the address of a scalar A-Z variable. KEY and TIMER intentionally do
; not accept array elements, keeping their assignment syntax unambiguous.
basic_scalar_address:
    call basic_parse_letter
    jnc .invalid
    xor bx, bx
    mov bl, al
    shl bx, 1
    mov di, basic_vars
    add di, bx
    stc
    ret
.invalid:
    clc
    ret

; Parse a case-insensitive A-Z name and return its zero-based index in AL.
basic_parse_letter:
    call basic_skip_spaces
    mov al, [si]
    or al, 0x20
    cmp al, 'a'
    jb .invalid
    cmp al, 'z'
    ja .invalid
    sub al, 'a'
    xor ah, ah
    inc si
    stc
    ret
.invalid:
    clc
    ret

; Parse the optional trailing PRINT semicolon and then the physical line end.
basic_print_ending:
    mov byte [print_suppressed], 0
    call basic_skip_spaces
    cmp byte [si], ';'
    jne basic_end_line
    inc si
    mov byte [print_suppressed], 1
    jmp basic_end_line

; Accept spaces through a statement boundary. A colon advances to another
; statement on the same numbered physical line; CR/LF and NUL advance through
; the normal line-number path. The dispatcher owns the resulting distinction.
basic_end_line:
    call basic_skip_spaces
    mov byte [statement_separator], 0
    cmp byte [si], ':'
    je .colon
    cmp byte [si], 0
    je .valid
    cmp byte [si], 13
    jne .invalid
    inc si
    cmp byte [si], 10
    jne .valid
    inc si
.valid:
    stc
    ret
.colon:
    inc si
    mov byte [statement_separator], 1
    stc
    ret
.invalid:
    clc
    ret

basic_next_line:
    mov al, [si]
    test al, al
    jz .done
    inc si
    cmp al, 13
    jne basic_next_line
    cmp byte [si], 10
    jne .done
    inc si
.done:
    ret

; Find the first physical line numbered DX and return its start in SI.
basic_find_line:
    mov si, [program_start]
.scan:
    call basic_skip_spaces
    cmp byte [si], 0
    je .missing
    cmp byte [si], 13
    je .advance
    mov bx, si
    push dx
    call basic_parse_uint
    pop dx
    jnc .advance
    cmp ax, dx
    je .found
.advance:
    call basic_next_line
    jmp .scan
.found:
    mov si, bx
    stc
    ret
.missing:
    clc
    ret

; Wait for a positive number of BIOS timer ticks. The 32-bit subtraction also
; handles the BIOS tick counter's reset at midnight for these short waits.
basic_wait:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    mov ah, 0
    int 0x1a
    mov [wait_start_low], dx
    mov [wait_start_high], cx
.loop:
    pushf
    sti
    hlt
    popf
    mov ah, 0
    int 0x1a
    mov ax, dx
    mov bx, cx
    sub ax, [wait_start_low]
    sbb bx, [wait_start_high]
    jnc .elapsed
    add ax, TICKS_PER_DAY_LOW
    adc bx, TICKS_PER_DAY_HIGH
.elapsed:
    test bx, bx
    jnz .done
    cmp ax, [wait_duration]
    jb .loop
.done:
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

basic_print_integer:
    test ax, ax
    jns basic_print_unsigned
    push ax
    mov al, '-'
    call serial_write
    pop ax
    neg ax
basic_print_unsigned:
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

print_string:
    lodsb
    test al, al
    jz .done
    call serial_write
    jmp print_string
.done:
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

serial_read:
    mov dx, COM1 + 5
.wait:
    in al, dx
    test al, 1
    jz .wait
    mov dx, COM1
    in al, dx
    ret

; Carry is set with AL holding a raw COM1 byte. Carry is clear when no byte is
; ready; this routine never waits.
serial_try_read:
    mov dx, COM1 + 5
    in al, dx
    test al, 1
    jz .none
    mov dx, COM1
    in al, dx
    stc
    ret
.none:
    clc
    ret

basic_output     db 27, '[2J', 27, '[H', 'Nixodria BASIC', 13, 10, 13, 10, 0
basic_finished   db 13, 10, 'Program finished. Press any key.', 0
basic_error      db 13, 10, 'BASIC error at line ', 0
basic_error_end  db '. Press any key.', 0
clear_sequence  db 27, '[2J', 27, '[H', 0
newline         db 13, 10, 0

basic_kw_print  db 'print', 0
basic_kw_let    db 'let', 0
basic_kw_if     db 'if', 0
basic_kw_goto   db 'goto', 0
basic_kw_gosub  db 'gosub', 0
basic_kw_return db 'return', 0
basic_kw_dim    db 'dim', 0
basic_kw_cls    db 'cls', 0
basic_kw_key    db 'key', 0
basic_kw_wait   db 'wait', 0
basic_kw_timer  db 'timer', 0
basic_kw_rem    db 'rem', 0
basic_kw_end    db 'end', 0
basic_kw_then   db 'then', 0
basic_kw_mod    db 'mod', 0
basic_kw_and    db 'and', 0

program_start          dw 0
program_end            dw 0
source_valid           db 0
basic_line             dw 0
basic_steps            dw 0
basic_left             dw 0
basic_operator         db 0
basic_vars             times 26 dw 0
array_name             db 0xff
array_max              db 0
array_candidate        db 0
array_bound_candidate  db 0
expression_depth       db 0
return_depth           db 0
basic_return_stack     times GOSUB_LIMIT dw 0
basic_return_line      times GOSUB_LIMIT dw 0
basic_return_same_line times GOSUB_LIMIT db 0
statement_separator    db 0
print_suppressed       db 0
wait_duration          dw 0
wait_start_low         dw 0
wait_start_high        dw 0

times (16 * 512) - ($ - $$) db 0
