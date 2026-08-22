bits 16
org 0x1000

; Nixodria guest-native network printer module.
;
; The resident kernel loads this flat binary at 0000:1000 and calls offset 0.
; No host print service is involved: this module drives a polled NE2000, opens
; TCP port 631, submits an IPP Print-Job, and streams 300-dpi sGray PWG Raster.
;
; Entry ABI (near call to 0000:1000):
;   DS = ES = 0
;   SI -> file bytes, CX = byte length (0..2047)
;   DI -> NUL-terminated display filename
;   BX -> four printer IPv4 octets in network order
; Return:
;   CF clear, AL=0  IPP returned a successful status
;   CF set,   AL=1  NIC, ARP, TCP, or timeout failure
;   CF set,   AL=2  HTTP/IPP rejection or malformed response
;   CF set,   AL=3  invalid input or unsupported firmware environment
; BP, SP, DS, and ES are preserved. Other registers are caller-clobbered.
;
; Platform contract:
;   NE2000 I/O 0x300, MAC 52:54:00:12:34:56
;   IPv4 10.0.2.15/24, gateway 10.0.2.2 (QEMU user networking defaults)
;   One outstanding TCP segment at a time, bounded retransmission, no retries
;   after an IPP success response. A timeout after submission is deliberately
;   reported as unknown/failure so the shell will not duplicate a print job.

jmp near print_entry
module_signature db 'NIXPRINT1'
; tools/build_image.py writes the CRC-16/CCITT-FALSE of this complete 16 KiB
; module here after calculating it with these two bytes set to zero. The
; resident loader performs the same calculation before executing offset 0.
module_checksum dw 0
%if module_signature - $$ != 3
    %error "printer module signature moved"
%endif
%if module_checksum - $$ != 12
    %error "printer module checksum moved"
%endif

NE_BASE             equ 0x0300
NE_DATA             equ NE_BASE + 0x10
NE_RESET            equ NE_BASE + 0x1f
NE_TX_PAGE          equ 0x40
NE_RX_START         equ 0x46
NE_RX_STOP          equ 0x80

RX_BUFFER           equ 0x5000
RX_BUFFER_SIZE      equ 1600
TX_BUFFER           equ 0x5700
TX_BUFFER_SIZE      equ 512
STREAM_BUFFER       equ 0x5900
STREAM_CAPACITY     equ 384
RESPONSE_BUFFER     equ 0x5b00
RESPONSE_CAPACITY   equ 1536
PAGE_HEADER         equ 0x6100
PAGE_HEADER_SIZE    equ 1796
LINE_BUFFER         equ 0x6900
IP_ASCII_BUFFER     equ 0x6970
URI_BUFFER          equ 0x6990
CHUNK_BUFFER        equ 0x6a00
STACK_GUARD         equ 0x7000

%if CHUNK_BUFFER + STREAM_CAPACITY + 7 > STACK_GUARD
    %error "printer scratch space reached the stack guard"
%endif

MAX_FILE_LENGTH     equ 2047
MAX_FILENAME        equ 63
MAX_COLUMNS         equ 93
LINES_PER_PAGE      equ 96
RASTER_WIDTH        equ 2550
RASTER_HEIGHT       equ 3300
TOP_MARGIN          equ 150
FONT_SCALE          equ 3
TEXT_ROW_HEIGHT     equ 30

ARP_TIMEOUT         equ 18       ; roughly one second at 18.2 BIOS ticks/sec
TCP_TIMEOUT         equ 36
RESPONSE_TIMEOUT    equ 182
RESET_TIMEOUT       equ 9
RETRY_LIMIT         equ 4

print_entry:
    push bp
    push ds
    push es
    xor ax, ax
    mov ds, ax
    mov es, ax
    cld

    mov [file_pointer], si
    mov [file_length], cx
    mov [file_name], di
    cmp cx, MAX_FILE_LENGTH
    ja .invalid
    test bx, bx
    jz .invalid

    mov si, bx
    mov di, target_ip
    movsw
    movsw
    cmp byte [target_ip], 0
    je .invalid
    cmp byte [target_ip], 255
    je .invalid

    call measure_filename
    jc .invalid
    call acquire_font
    jc .invalid
    call count_pages
    call build_addresses

    call nic_init
    jc .network_error
    call arp_resolve
    jc .network_error
    call tcp_connect
    jc .network_error
    call send_http_header
    jc .network_error

    mov word [stream_length], 0
    mov byte [io_failed], 0
    mov byte [response_collecting], 0
    mov word [response_length], 0
    call write_ipp_request
    call write_pwg_document
    call stream_flush
    cmp byte [io_failed], 0
    jne .network_error

    ; Only responses to a complete request are authoritative. Collection starts
    ; before the terminating chunk because a small server response can share
    ; the ACK for that segment.
    mov byte [response_collecting], 1
    mov si, chunk_terminator
    mov cx, chunk_terminator_end - chunk_terminator
    call tcp_send_bytes
    jc .network_error
    call tcp_wait_response
    cmp al, 0
    je .accepted
    cmp al, 2
    je .printer_error
    jmp .network_error

.accepted:
    call tcp_close
    xor ax, ax
    clc
    jmp .return
.network_error:
    mov al, 1
    stc
    jmp .return
.printer_error:
    mov al, 2
    stc
    jmp .return
.invalid:
    mov al, 3
    stc
.return:
    pop es
    pop ds
    pop bp
    ret

; ---------------------------------------------------------------------------
; Input preparation and text pagination

measure_filename:
    mov si, [file_name]
    xor cx, cx
.loop:
    cmp cx, MAX_FILENAME
    jae .bad
    lodsb
    test al, al
    jz .done
    inc cx
    jmp .loop
.done:
    test cx, cx
    jz .bad
    mov [file_name_length], cx
    clc
    ret
.bad:
    stc
    ret

acquire_font:
    ; VGA BIOS function 1130h/BH=3 returns the 8x8 ROM font at ES:BP. The
    ; pointer is saved and ES is immediately restored to the flat data segment.
    mov ax, 0x1130
    mov bh, 3
    int 0x10
    mov bx, bp
    mov dx, es
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov [font_offset], bx
    mov [font_segment], dx
    cmp word [font_segment], 0
    jne .ok
    cmp word [font_offset], 0
    je .bad
.ok:
    clc
    ret
.bad:
    stc
    ret

count_pages:
    mov ax, [file_pointer]
    mov [scan_pointer], ax
    mov ax, [file_length]
    mov [scan_remaining], ax
    mov word [page_count], 1
    mov word [count_page_lines], 0
    test ax, ax
    jz .done
.line:
    call next_line
    inc word [count_page_lines]
    cmp word [count_page_lines], LINES_PER_PAGE
    jb .more
    cmp word [scan_remaining], 0
    je .done
    inc word [page_count]
    mov word [count_page_lines], 0
.more:
    cmp word [scan_remaining], 0
    jne .line
.done:
    mov ax, [file_pointer]
    mov [scan_pointer], ax
    mov ax, [file_length]
    mov [scan_remaining], ax
    ret

; Consume one normalized logical line into LINE_BUFFER. Long lines wrap at
; MAX_COLUMNS; CRLF is one break, tabs advance to the next four-column stop.
next_line:
    mov si, [scan_pointer]
    mov cx, [scan_remaining]
    mov di, LINE_BUFFER
    xor bx, bx
.scan:
    test cx, cx
    jz .done
    cmp bx, MAX_COLUMNS
    jae .full
    mov al, [si]
    cmp al, 13
    je .cr
    cmp al, 10
    je .lf
    inc si
    dec cx
    cmp al, 9
    je .tab
    cmp al, 32
    jae .printable
    mov al, '?'
.printable:
    cmp al, 126
    jbe .store
    mov al, '?'
.store:
    stosb
    inc bx
    jmp .scan
.tab:
    mov dx, bx
    and dx, 3
    mov ax, 4
    sub ax, dx
.tab_loop:
    cmp bx, MAX_COLUMNS
    jae .full_after_consumed
    mov byte [di], ' '
    inc di
    inc bx
    dec ax
    jnz .tab_loop
    jmp .scan
.cr:
    inc si
    dec cx
    test cx, cx
    jz .done
    cmp byte [si], 10
    jne .done
    inc si
    dec cx
    jmp .done
.lf:
    inc si
    dec cx
    jmp .done
.full:
    ; Avoid manufacturing an empty line when an exactly full line is followed
    ; by its line ending.
    cmp byte [si], 13
    je .cr
    cmp byte [si], 10
    je .lf
    jmp .done
.full_after_consumed:
    test cx, cx
    jz .done
    cmp byte [si], 13
    je .cr
    cmp byte [si], 10
    je .lf
.done:
    mov [scan_pointer], si
    mov [scan_remaining], cx
    mov [line_length], bx
    ret

build_addresses:
    mov di, IP_ASCII_BUFFER
    mov si, target_ip
    mov cx, 4
.octet:
    lodsb
    call append_decimal_byte
    dec cx
    jz .ip_done
    mov al, '.'
    stosb
    jmp .octet
.ip_done:
    mov byte [di], 0
    mov ax, di
    sub ax, IP_ASCII_BUFFER
    mov [ip_ascii_length], ax

    mov di, URI_BUFFER
    mov si, uri_prefix
    call append_cstring
    mov si, IP_ASCII_BUFFER
    call append_cstring
    mov si, uri_suffix
    call append_cstring
    mov byte [di], 0
    mov ax, di
    sub ax, URI_BUFFER
    mov [uri_length], ax
    ret

append_decimal_byte:
    xor ah, ah
    cmp ax, 100
    jb .tens
    xor dx, dx
    mov bx, 100
    div bx
    add al, '0'
    stosb
    mov ax, dx
    xor dx, dx
    mov bx, 10
    div bx
    add al, '0'
    stosb
    mov ax, dx
    jmp .ones
.tens:
    cmp ax, 10
    jb .ones
    xor dx, dx
    mov bx, 10
    div bx
    add al, '0'
    stosb
    mov ax, dx
.ones:
    add al, '0'
    stosb
    ret

append_cstring:
.loop:
    lodsb
    test al, al
    jz .done
    stosb
    jmp .loop
.done:
    ret

; ---------------------------------------------------------------------------
; NE2000 driver

nic_init:
    mov dx, NE_RESET
    in al, dx
    cmp al, 0xff
    je .bad
    out dx, al
    call ticks
    mov [wait_start], ax
.reset_wait:
    mov dx, NE_BASE + 7
    in al, dx
    test al, 0x80
    jnz .reset_done
    call reset_expired
    jnc .reset_wait
    jmp .bad
.reset_done:
    mov al, 0xff
    out dx, al
    mov dx, NE_BASE
    mov al, 0x21               ; stopped, page 0, remote DMA aborted
    out dx, al
    mov dx, NE_BASE + 0x0e
    mov al, 0x49               ; word transfers, little-endian, 8-byte FIFO
    out dx, al
    mov dx, NE_BASE + 0x0a
    xor al, al
    out dx, al
    inc dx
    out dx, al
    mov dx, NE_BASE + 0x0c
    mov al, 0x20               ; monitor while the ring is initialized
    out dx, al
    inc dx
    mov al, 0x02               ; internal loopback
    out dx, al
    mov dx, NE_BASE + 4
    mov al, NE_TX_PAGE
    out dx, al
    mov dx, NE_BASE + 1
    mov al, NE_RX_START
    out dx, al
    inc dx
    mov al, NE_RX_STOP
    out dx, al
    inc dx
    mov al, NE_RX_START
    out dx, al
    mov dx, NE_BASE + 7
    mov al, 0xff
    out dx, al
    mov dx, NE_BASE + 0x0f
    xor al, al                  ; polling only
    out dx, al

    mov dx, NE_BASE
    mov al, 0x61               ; stopped, page 1
    out dx, al
    mov dx, NE_BASE + 1
    mov si, local_mac
    mov cx, 6
.mac:
    lodsb
    out dx, al
    inc dx
    loop .mac
    mov dx, NE_BASE + 7
    mov al, NE_RX_START + 1
    out dx, al

    mov dx, NE_BASE
    mov al, 0x21               ; page 0 before leaving loopback
    out dx, al
    mov dx, NE_BASE + 0x0d
    xor al, al
    out dx, al
    mov dx, NE_BASE + 0x0c
    mov al, 0x04               ; accept broadcast plus our programmed MAC
    out dx, al
    mov dx, NE_BASE
    mov al, 0x22               ; started, remote DMA aborted
    out dx, al
    clc
    ret
.bad:
    stc
    ret

reset_expired:
    call ticks
    sub ax, [wait_start]
    cmp ax, RESET_TIMEOUT
    jb .no
    stc
    ret
.no:
    clc
    ret

; Send the Ethernet frame at TX_BUFFER. CX is the unpadded length.
nic_send_frame:
    mov [tx_frame_length], cx
    cmp cx, 60
    jae .minimum_done
    mov di, TX_BUFFER
    add di, cx
    mov ax, 60
    sub ax, cx
    mov cx, ax
    xor al, al
    rep stosb
    mov word [tx_frame_length], 60
.minimum_done:
    mov cx, [tx_frame_length]
    test cx, 1
    jz .even
    mov di, TX_BUFFER
    add di, cx
    mov byte [di], 0
    inc cx
.even:
    mov [dma_length], cx

    mov dx, NE_BASE + 7
    mov al, 0x4a               ; clear RDC, PTX, and TXE
    out dx, al
    mov dx, NE_BASE + 8
    xor al, al
    out dx, al
    inc dx
    mov al, NE_TX_PAGE
    out dx, al
    mov dx, NE_BASE + 0x0a
    mov ax, [dma_length]
    out dx, al
    inc dx
    mov al, ah
    out dx, al
    mov dx, NE_BASE
    mov al, 0x12               ; start remote write
    out dx, al
    mov dx, NE_DATA
    mov si, TX_BUFFER
    mov cx, [dma_length]
    shr cx, 1
    rep outsw

    call ticks
    mov [wait_start], ax
.rdc_wait:
    mov dx, NE_BASE + 7
    in al, dx
    test al, 0x40
    jnz .rdc_done
    call reset_expired
    jnc .rdc_wait
    stc
    ret
.rdc_done:
    mov al, 0x40
    out dx, al
    mov dx, NE_BASE + 4
    mov al, NE_TX_PAGE
    out dx, al
    inc dx
    mov ax, [tx_frame_length]
    out dx, al
    inc dx
    mov al, ah
    out dx, al
    mov dx, NE_BASE
    mov al, 0x26               ; transmit, started, DMA aborted
    out dx, al

    call ticks
    mov [wait_start], ax
.tx_wait:
    mov dx, NE_BASE + 7
    in al, dx
    test al, 0x02
    jnz .sent
    test al, 0x08
    jnz .bad
    call reset_expired
    jnc .tx_wait
.bad:
    stc
    ret
.sent:
    mov al, 0x02
    out dx, al
    clc
    ret

; Remote-DMA read CX bytes from NIC address AX to ES:DI.
nic_remote_read:
    mov [remote_address], ax
    mov [remote_length], cx
    test cx, 1
    jz .even
    inc cx
.even:
    mov [dma_length], cx
    mov dx, NE_BASE + 7
    mov al, 0x40
    out dx, al
    mov dx, NE_BASE + 8
    mov ax, [remote_address]
    out dx, al
    inc dx
    mov al, ah
    out dx, al
    mov dx, NE_BASE + 0x0a
    mov ax, [dma_length]
    out dx, al
    inc dx
    mov al, ah
    out dx, al
    mov dx, NE_BASE
    mov al, 0x0a               ; start remote read
    out dx, al
    mov dx, NE_DATA
    mov cx, [dma_length]
    shr cx, 1
    rep insw
    mov dx, NE_BASE + 7
    mov al, 0x40
    out dx, al
    clc
    ret

; Ring reads split at PSTOP because the remote-DMA counter itself does not
; understand receive-ring wrapping.
nic_ring_read:
    mov [ring_address], ax
    mov [ring_length], cx
    mov [ring_destination], di
    mov dx, NE_RX_STOP << 8
    sub dx, ax
    cmp cx, dx
    jbe .single
    mov [ring_split_length], dx
    mov cx, dx
    call nic_remote_read
    jc .bad
    mov ax, [ring_destination]
    add ax, [ring_split_length]
    mov di, ax
    mov cx, [ring_length]
    sub cx, [ring_split_length]
    mov ax, NE_RX_START << 8
    call nic_remote_read
    ret
.single:
    call nic_remote_read
    ret
.bad:
    stc
    ret

; Return the next complete Ethernet frame in RX_BUFFER, CX=length. CF means
; there is currently no usable frame.
nic_receive:
    mov dx, NE_BASE + 7
    in al, dx
    test al, 0x10
    jz .ring_state
    call nic_init               ; overflow recovery discards ambiguous frames
    stc
    ret
.ring_state:
    mov dx, NE_BASE
    mov al, 0x22
    out dx, al
    mov dx, NE_BASE + 3
    in al, dx
    inc al
    cmp al, NE_RX_STOP
    jb .page_ok
    mov al, NE_RX_START
.page_ok:
    mov [rx_page], al
    mov dx, NE_BASE
    mov al, 0x62
    out dx, al
    mov dx, NE_BASE + 7
    in al, dx
    mov [rx_current], al
    mov dx, NE_BASE
    mov al, 0x22
    out dx, al
    mov al, [rx_page]
    cmp al, [rx_current]
    je .none

    xor ah, ah
    xchg al, ah
    mov di, RX_BUFFER
    mov cx, 4
    call nic_ring_read
    jc .none
    test byte [RX_BUFFER], 1
    jz .discard
    mov al, [RX_BUFFER + 1]
    cmp al, NE_RX_START
    jb .discard
    cmp al, NE_RX_STOP
    jae .discard
    mov [rx_next], al
    mov cx, [RX_BUFFER + 2]
    cmp cx, 64
    jb .discard
    cmp cx, 1522
    ja .discard
    sub cx, 4
    mov [rx_frame_length], cx
    mov al, [rx_page]
    xor ah, ah
    xchg al, ah
    add ax, 4
    mov di, RX_BUFFER
    call nic_ring_read
    jc .discard
    call release_rx_page
    mov cx, [rx_frame_length]
    clc
    ret
.discard:
    mov al, [RX_BUFFER + 1]
    mov [rx_next], al
    call release_rx_page
.none:
    stc
    ret

release_rx_page:
    mov al, [rx_next]
    dec al
    cmp al, NE_RX_START
    jae .set
    mov al, NE_RX_STOP - 1
.set:
    mov dx, NE_BASE + 3
    out dx, al
    mov dx, NE_BASE + 7
    mov al, 0x05               ; PRX/RXE are edge hints; ring pointers decide
    out dx, al
    ret

; ---------------------------------------------------------------------------
; ARP and IPv4/TCP

arp_resolve:
    ; Route directly inside 10.0.2/24, otherwise resolve the fixed gateway.
    mov si, target_ip
    mov di, local_ip
    mov cx, 3
    repe cmpsb
    jne .gateway
    mov si, target_ip
    jmp .copy_route
.gateway:
    mov si, gateway_ip
.copy_route:
    mov di, next_hop_ip
    movsw
    movsw
    mov byte [retry_count], RETRY_LIMIT
.attempt:
    call send_arp_request
    jc .retry
    call ticks
    mov [wait_start], ax
.wait:
    call nic_receive
    jc .time
    cmp cx, 42
    jb .time
    cmp word [RX_BUFFER + 12], 0x0608
    jne .time
    cmp word [RX_BUFFER + 14], 0x0100
    jne .time
    cmp word [RX_BUFFER + 16], 0x0008
    jne .time
    cmp word [RX_BUFFER + 18], 0x0406
    jne .time
    cmp word [RX_BUFFER + 20], 0x0200
    jne .time
    mov si, RX_BUFFER + 28
    mov di, next_hop_ip
    mov cx, 4
    repe cmpsb
    jne .time
    mov si, RX_BUFFER + 38
    mov di, local_ip
    mov cx, 4
    repe cmpsb
    jne .time
    mov si, RX_BUFFER + 22
    mov di, peer_mac
    movsw
    movsw
    movsw
    clc
    ret
.time:
    call ticks
    sub ax, [wait_start]
    cmp ax, ARP_TIMEOUT
    jb .wait
.retry:
    dec byte [retry_count]
    jnz .attempt
    stc
    ret

send_arp_request:
    mov di, TX_BUFFER
    mov cx, 60
    xor al, al
    rep stosb
    mov di, TX_BUFFER
    mov cx, 6
    mov al, 0xff
    rep stosb
    mov si, local_mac
    mov cx, 6
    rep movsb
    mov word [TX_BUFFER + 12], 0x0608
    mov word [TX_BUFFER + 14], 0x0100
    mov word [TX_BUFFER + 16], 0x0008
    mov byte [TX_BUFFER + 18], 6
    mov byte [TX_BUFFER + 19], 4
    mov word [TX_BUFFER + 20], 0x0100
    mov si, local_mac
    mov di, TX_BUFFER + 22
    movsw
    movsw
    movsw
    mov si, local_ip
    movsw
    movsw
    mov di, TX_BUFFER + 38
    mov si, next_hop_ip
    movsw
    movsw
    mov cx, 42
    call nic_send_frame
    ret

tcp_connect:
    call ticks
    mov bx, ax
    and bx, 0x0fff
    add bx, 49152
    mov [local_port], bx
    xor ax, 0x584e
    mov [local_sequence_low], ax
    mov word [local_sequence_high], 0x4e49
    mov word [remote_sequence_low], 0
    mov word [remote_sequence_high], 0
    mov byte [remote_fin], 0
    mov ax, [local_sequence_low]
    add ax, 1
    mov [expected_ack_low], ax
    mov ax, [local_sequence_high]
    adc ax, 0
    mov [expected_ack_high], ax
    mov byte [retry_count], RETRY_LIMIT
.attempt:
    xor cx, cx
    mov dl, 0x02
    call tcp_build_and_send
    jc .retry
    call ticks
    mov [wait_start], ax
.wait:
    call tcp_receive
    jc .time
    test byte [rx_tcp_flags], 0x04
    jnz .bad
    mov al, [rx_tcp_flags]
    and al, 0x12
    cmp al, 0x12
    jne .time
    mov ax, [rx_ack_low]
    cmp ax, [expected_ack_low]
    jne .time
    mov ax, [rx_ack_high]
    cmp ax, [expected_ack_high]
    jne .time
    mov ax, [rx_sequence_low]
    add ax, 1
    mov [remote_sequence_low], ax
    mov ax, [rx_sequence_high]
    adc ax, 0
    mov [remote_sequence_high], ax
    mov ax, [expected_ack_low]
    mov [local_sequence_low], ax
    mov ax, [expected_ack_high]
    mov [local_sequence_high], ax
    call tcp_ack_now
    clc
    ret
.time:
    call ticks
    sub ax, [wait_start]
    cmp ax, TCP_TIMEOUT
    jb .wait
.retry:
    dec byte [retry_count]
    jnz .attempt
.bad:
    stc
    ret

; SI/CX is one TCP payload. Stop-and-wait bounds memory and makes retransmission
; unambiguous on a system without a clock-driven network task.
tcp_send_bytes:
    mov [send_pointer], si
    mov [send_length], cx
    mov ax, [local_sequence_low]
    add ax, cx
    mov [expected_ack_low], ax
    mov ax, [local_sequence_high]
    adc ax, 0
    mov [expected_ack_high], ax
    mov byte [retry_count], RETRY_LIMIT
.attempt:
    mov si, [send_pointer]
    mov cx, [send_length]
    mov dl, 0x18               ; PSH|ACK
    call tcp_build_and_send
    jc .retry
    call ticks
    mov [wait_start], ax
.wait:
    call tcp_receive
    jc .time
    call tcp_handle_received
    test byte [rx_tcp_flags], 0x04
    jnz .bad
    test byte [rx_tcp_flags], 0x10
    jz .time
    mov ax, [rx_ack_low]
    cmp ax, [expected_ack_low]
    jne .time
    mov ax, [rx_ack_high]
    cmp ax, [expected_ack_high]
    jne .time
    mov ax, [expected_ack_low]
    mov [local_sequence_low], ax
    mov ax, [expected_ack_high]
    mov [local_sequence_high], ax
    clc
    ret
.time:
    call ticks
    sub ax, [wait_start]
    cmp ax, TCP_TIMEOUT
    jb .wait
.retry:
    dec byte [retry_count]
    jnz .attempt
.bad:
    stc
    ret

; Build Ethernet + IPv4 + a 20-byte TCP header at TX_BUFFER and transmit it.
; SI/CX payload, DL flags. Payloads are kept below the 512-byte work buffer.
tcp_build_and_send:
    mov [send_flags], dl
    mov [packet_payload_length], cx
    cmp cx, TX_BUFFER_SIZE - 54
    ja .bad
    push si
    mov di, TX_BUFFER
    mov ax, 54
    add ax, cx
    push cx
    mov cx, ax
    xor al, al
    rep stosb
    pop cx

    mov di, TX_BUFFER
    mov si, peer_mac
    movsw
    movsw
    movsw
    mov si, local_mac
    movsw
    movsw
    movsw
    mov word [TX_BUFFER + 12], 0x0008

    mov byte [TX_BUFFER + 14], 0x45
    mov ax, cx
    add ax, 40
    xchg al, ah
    mov [TX_BUFFER + 16], ax
    inc word [ip_identifier]
    mov ax, [ip_identifier]
    xchg al, ah
    mov [TX_BUFFER + 18], ax
    mov word [TX_BUFFER + 20], 0x0040
    mov byte [TX_BUFFER + 22], 64
    mov byte [TX_BUFFER + 23], 6
    mov si, local_ip
    mov di, TX_BUFFER + 26
    movsw
    movsw
    mov si, target_ip
    movsw
    movsw

    mov ax, [local_port]
    xchg al, ah
    mov [TX_BUFFER + 34], ax
    mov word [TX_BUFFER + 36], 0x7702  ; 631 in network byte order
    mov ax, [local_sequence_high]
    xchg al, ah
    mov [TX_BUFFER + 38], ax
    mov ax, [local_sequence_low]
    xchg al, ah
    mov [TX_BUFFER + 40], ax
    mov ax, [remote_sequence_high]
    xchg al, ah
    mov [TX_BUFFER + 42], ax
    mov ax, [remote_sequence_low]
    xchg al, ah
    mov [TX_BUFFER + 44], ax
    mov byte [TX_BUFFER + 46], 0x50
    mov al, [send_flags]
    mov [TX_BUFFER + 47], al
    mov word [TX_BUFFER + 48], 0x0010  ; 4096-byte advertised window

    pop si
    mov di, TX_BUFFER + 54
    mov cx, [packet_payload_length]
    rep movsb

    mov word [checksum_sum], 0
    mov si, TX_BUFFER + 26
    mov cx, 8
    call checksum_accumulate
    mov ax, [checksum_sum]
    add ax, 6
    adc ax, 0
    mov [checksum_sum], ax
    mov ax, [packet_payload_length]
    add ax, 20
    mov [tcp_length], ax
    add [checksum_sum], ax
    adc word [checksum_sum], 0
    mov si, TX_BUFFER + 34
    mov cx, [tcp_length]
    call checksum_accumulate
    mov ax, [checksum_sum]
    not ax
    xchg al, ah
    mov [TX_BUFFER + 50], ax

    mov word [TX_BUFFER + 24], 0
    mov word [checksum_sum], 0
    mov si, TX_BUFFER + 14
    mov cx, 20
    call checksum_accumulate
    mov ax, [checksum_sum]
    not ax
    xchg al, ah
    mov [TX_BUFFER + 24], ax

    mov cx, [packet_payload_length]
    add cx, 54
    call nic_send_frame
    ret
.bad:
    stc
    ret

checksum_accumulate:
    push ax
    push bx
    push cx
    push si
    mov bx, [checksum_sum]
.words:
    cmp cx, 2
    jb .odd
    lodsw
    xchg al, ah
    add bx, ax
    adc bx, 0
    sub cx, 2
    jmp .words
.odd:
    test cx, cx
    jz .done
    lodsb
    xor ah, ah
    xchg al, ah
    add bx, ax
    adc bx, 0
.done:
    mov [checksum_sum], bx
    pop si
    pop cx
    pop bx
    pop ax
    ret

; Parse one connection-matching IPv4/TCP packet into rx_* fields.
tcp_receive:
    call nic_receive
    jc .none
    cmp cx, 54
    jb .none
    cmp word [RX_BUFFER + 12], 0x0008
    jne .none
    mov al, [RX_BUFFER + 14]
    mov ah, al
    and ah, 0xf0
    cmp ah, 0x40
    jne .none
    and al, 0x0f
    cmp al, 5
    jb .none
    xor ah, ah
    shl ax, 1
    shl ax, 1
    mov [rx_ip_header_length], ax
    mov dx, [RX_BUFFER + 16]
    xchg dl, dh
    mov [rx_ip_total_length], dx
    mov bx, ax
    add bx, 20
    cmp dx, bx
    jb .none
    add dx, 14
    cmp dx, [rx_frame_length]
    ja .none
    mov ax, [RX_BUFFER + 20]
    xchg al, ah
    test ax, 0x3fff             ; reject MF and all non-zero fragment offsets
    jnz .none
    mov word [checksum_sum], 0
    mov si, RX_BUFFER + 14
    mov cx, [rx_ip_header_length]
    call checksum_accumulate
    cmp word [checksum_sum], 0xffff
    jne .none
    cmp byte [RX_BUFFER + 23], 6
    jne .none
    mov si, RX_BUFFER + 26
    mov di, target_ip
    mov cx, 4
    repe cmpsb
    jne .none
    mov si, RX_BUFFER + 30
    mov di, local_ip
    mov cx, 4
    repe cmpsb
    jne .none
    mov bx, RX_BUFFER + 14
    add bx, [rx_ip_header_length]
    mov ax, [rx_ip_total_length]
    sub ax, [rx_ip_header_length]
    mov [rx_tcp_segment_length], ax
    mov word [checksum_sum], 0
    mov si, RX_BUFFER + 26
    mov cx, 8
    call checksum_accumulate
    mov ax, [checksum_sum]
    add ax, 6
    adc ax, 0
    mov [checksum_sum], ax
    mov ax, [rx_tcp_segment_length]
    add [checksum_sum], ax
    adc word [checksum_sum], 0
    mov si, bx
    mov cx, [rx_tcp_segment_length]
    call checksum_accumulate
    cmp word [checksum_sum], 0xffff
    jne .none
    cmp word [bx], 0x7702
    jne .none
    mov ax, [bx + 2]
    xchg al, ah
    cmp ax, [local_port]
    jne .none

    mov ax, [bx + 4]
    xchg al, ah
    mov [rx_sequence_high], ax
    mov ax, [bx + 6]
    xchg al, ah
    mov [rx_sequence_low], ax
    mov ax, [bx + 8]
    xchg al, ah
    mov [rx_ack_high], ax
    mov ax, [bx + 10]
    xchg al, ah
    mov [rx_ack_low], ax
    mov al, [bx + 13]
    mov [rx_tcp_flags], al
    mov al, [bx + 12]
    and al, 0xf0
    xor ah, ah
    shr ax, 1
    shr ax, 1
    mov [rx_tcp_header_length], ax
    cmp ax, 20
    jb .none

    mov ax, [RX_BUFFER + 16]
    xchg al, ah
    mov dx, ax
    sub dx, [rx_ip_header_length]
    sub dx, [rx_tcp_header_length]
    jc .none
    mov [rx_data_length], dx
    mov ax, bx
    add ax, [rx_tcp_header_length]
    mov [rx_data_pointer], ax
    clc
    ret
.none:
    stc
    ret

tcp_handle_received:
    mov bx, [rx_data_length]
    test bx, bx
    jz .fin
    mov ax, [rx_sequence_low]
    cmp ax, [remote_sequence_low]
    jne .ack
    mov ax, [rx_sequence_high]
    cmp ax, [remote_sequence_high]
    jne .ack
    cmp byte [response_collecting], 0
    je .advance
    mov ax, RESPONSE_CAPACITY
    sub ax, [response_length]
    cmp bx, ax
    jbe .copy_size
    mov bx, ax
.copy_size:
    test bx, bx
    jz .advance
    mov si, [rx_data_pointer]
    mov di, RESPONSE_BUFFER
    add di, [response_length]
    mov cx, bx
    rep movsb
    add [response_length], bx
.advance:
    mov ax, [remote_sequence_low]
    add ax, [rx_data_length]
    mov [remote_sequence_low], ax
    mov ax, [remote_sequence_high]
    adc ax, 0
    mov [remote_sequence_high], ax
.fin:
    test byte [rx_tcp_flags], 0x01
    jz .ack_if_data
    mov ax, [rx_sequence_low]
    add ax, [rx_data_length]
    mov dx, [rx_sequence_high]
    adc dx, 0
    cmp ax, [remote_sequence_low]
    jne .ack
    cmp dx, [remote_sequence_high]
    jne .ack
    inc word [remote_sequence_low]
    jnz .mark_fin
    inc word [remote_sequence_high]
.mark_fin:
    mov byte [remote_fin], 1
.ack:
    call tcp_ack_now
    ret
.ack_if_data:
    cmp word [rx_data_length], 0
    jne .ack
    ret

tcp_ack_now:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    xor cx, cx
    mov dl, 0x10
    call tcp_build_and_send
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

tcp_wait_response:
    call parse_response
    cmp al, 1
    jne .done
    call ticks
    mov [wait_start], ax
.wait:
    call tcp_receive
    jc .time
    call tcp_handle_received
    test byte [rx_tcp_flags], 0x04
    jnz .protocol
    call parse_response
    cmp al, 1
    jne .done
    cmp byte [remote_fin], 0
    jne .protocol
.time:
    call ticks
    sub ax, [wait_start]
    cmp ax, RESPONSE_TIMEOUT
    jb .wait
    cmp word [response_length], 0
    je .network
.protocol:
    mov al, 2
    stc
    ret
.network:
    mov al, 1
    stc
.done:
    ret

; AL=0 successful IPP status, AL=2 explicit/malformed rejection, AL=1 need
; more bytes. A successful result requires a valid HTTP/1.0 or HTTP/1.1 status
; line followed by a structurally complete IPP response entity.
parse_response:
    mov cx, [response_length]
    cmp cx, 13
    jb .incomplete
    mov si, RESPONSE_BUFFER
    mov di, http_response_prefix
    mov cx, 7
    repe cmpsb
    jne .malformed
    mov al, [RESPONSE_BUFFER + 7]
    cmp al, '0'
    je .http_version
    cmp al, '1'
    jne .malformed
.http_version:
    cmp byte [RESPONSE_BUFFER + 8], ' '
    jne .malformed
    cmp byte [RESPONSE_BUFFER + 9], '2'
    jne .rejected
    cmp byte [RESPONSE_BUFFER + 10], '0'
    jne .rejected
    cmp byte [RESPONSE_BUFFER + 11], '0'
    jne .rejected
    cmp byte [RESPONSE_BUFFER + 12], ' '
    jne .malformed

    mov si, RESPONSE_BUFFER
    mov cx, [response_length]
.headers:
    cmp cx, 4
    jb .incomplete
    cmp word [si], 0x0a0d
    jne .next_header
    cmp word [si + 2], 0x0a0d
    je .body
.next_header:
    inc si
    dec cx
    jmp .headers
.body:
    add si, 4
    sub cx, 4
    cmp cx, 8
    jb .incomplete
    mov al, [si]
    cmp al, 1
    je .version_1
    cmp al, 2
    jne .rejected
    cmp byte [si + 1], 2
    ja .rejected
    jmp .version_ok
.version_1:
    cmp byte [si + 1], 1
    ja .rejected
.version_ok:
    cmp dword [si + 4], 0x01000000
    jne .rejected
    cmp byte [si + 2], 0
    jne .rejected
    add si, 8
    sub cx, 8

; Walk the IPP attribute encoding rather than searching arbitrary body bytes.
; Delimiter tags are one byte. Value tags carry big-endian name/value lengths.
; End-of-attributes (03h) is mandatory before success can be reported.
.attributes:
    cmp cx, 1
    jb .incomplete
    lodsb
    dec cx
    cmp al, 0x03
    je .accepted
    cmp al, 0x10
    jb .delimiter
    cmp cx, 4
    jb .incomplete
    mov dx, [si]
    xchg dl, dh
    add si, 2
    sub cx, 2
    cmp dx, cx
    ja .incomplete
    add si, dx
    sub cx, dx
    cmp cx, 2
    jb .incomplete
    mov dx, [si]
    xchg dl, dh
    add si, 2
    sub cx, 2
    cmp dx, cx
    ja .incomplete
    add si, dx
    sub cx, dx
    jmp .attributes
.delimiter:
    test al, al
    jz .rejected
    jmp .attributes
.accepted:
    xor al, al
    clc
    ret
.malformed:
    ; Once a full status line exists, a non-HTTP response is a protocol error.
    mov cx, [response_length]
    cmp cx, 13
    jae .rejected
.incomplete:
    mov al, 1
    stc
    ret
.rejected:
    mov al, 2
    stc
    ret

tcp_close:
    cmp byte [remote_fin], 0
    jne .done
    xor cx, cx
    mov dl, 0x11
    call tcp_build_and_send
.done:
    ret

ticks:
    mov ah, 0
    int 0x1a
    mov ax, dx
    ret

; ---------------------------------------------------------------------------
; HTTP/IPP and chunked document stream

send_http_header:
    mov di, STREAM_BUFFER
    mov si, http_post
    call append_cstring
    mov si, IP_ASCII_BUFFER
    call append_cstring
    mov si, http_headers
    call append_cstring
    mov cx, di
    sub cx, STREAM_BUFFER
    mov si, STREAM_BUFFER
    call tcp_send_bytes
    ret

write_ipp_request:
    mov si, ipp_prefix
    mov cx, ipp_prefix_end - ipp_prefix
    call stream_put_block
    mov al, 0x45               ; uri
    call stream_put_byte
    mov al, 0
    call stream_put_byte
    mov al, 11
    call stream_put_byte
    mov si, ipp_printer_uri_name
    mov cx, 11
    call stream_put_block
    mov ax, [uri_length]
    call stream_put_be16
    mov si, URI_BUFFER
    mov cx, [uri_length]
    call stream_put_block
    mov si, ipp_middle
    mov cx, ipp_middle_end - ipp_middle
    call stream_put_block
    mov al, 0x42               ; nameWithoutLanguage
    call stream_put_byte
    mov al, 0
    call stream_put_byte
    mov al, 8
    call stream_put_byte
    mov si, ipp_job_name
    mov cx, 8
    call stream_put_block
    mov ax, [file_name_length]
    call stream_put_be16
    mov si, [file_name]
    mov cx, [file_name_length]
    call stream_put_block
    mov al, 0x03               ; end-of-attributes-tag
    call stream_put_byte
    ret

stream_put_be16:
    xchg al, ah
    call stream_put_byte
    xchg al, ah
    call stream_put_byte
    ret

stream_put_block:
.loop:
    test cx, cx
    jz .done
    lodsb
    call stream_put_byte
    dec cx
    jmp .loop
.done:
    ret

stream_put_byte:
    push bx
    push di
    cmp byte [io_failed], 0
    jne .done
    mov bx, [stream_length]
    cmp bx, STREAM_CAPACITY
    jb .space
    call stream_flush
    cmp byte [io_failed], 0
    jne .done
    xor bx, bx
.space:
    mov di, STREAM_BUFFER
    add di, bx
    mov [di], al
    inc bx
    mov [stream_length], bx
.done:
    pop di
    pop bx
    ret

stream_flush:
    push ax
    push bx
    push cx
    push dx
    push si
    push di
    cmp byte [io_failed], 0
    jne .done
    mov bx, [stream_length]
    test bx, bx
    jz .done
    mov di, CHUNK_BUFFER
    mov ax, bx
    mov cl, 8
    shr ax, cl
    and al, 0x0f
    call hex_digit
    stosb
    mov ax, bx
    mov cl, 4
    shr ax, cl
    and al, 0x0f
    call hex_digit
    stosb
    mov ax, bx
    and al, 0x0f
    call hex_digit
    stosb
    mov ax, 0x0a0d
    stosw
    mov si, STREAM_BUFFER
    mov cx, bx
    rep movsb
    mov ax, 0x0a0d
    stosw
    mov cx, bx
    add cx, 7
    mov si, CHUNK_BUFFER
    call tcp_send_bytes
    jnc .sent
    mov byte [io_failed], 1
    jmp .done
.sent:
    mov word [stream_length], 0
.done:
    pop di
    pop si
    pop dx
    pop cx
    pop bx
    pop ax
    ret

hex_digit:
    cmp al, 10
    jb .number
    add al, 'A' - 10
    ret
.number:
    add al, '0'
    ret

; ---------------------------------------------------------------------------
; PWG Raster generation

write_pwg_document:
    mov si, pwg_magic
    mov cx, 4
    call stream_put_block
    mov ax, [file_pointer]
    mov [scan_pointer], ax
    mov ax, [file_length]
    mov [scan_remaining], ax
    mov word [page_index], 0
.page:
    call build_page_header
    mov si, PAGE_HEADER
    mov cx, PAGE_HEADER_SIZE
    call stream_put_block
    mov ax, TOP_MARGIN
    call write_blank_rows
    mov word [page_y], TOP_MARGIN
    xor bx, bx
.line:
    cmp bx, LINES_PER_PAGE
    jae .finish_page
    cmp word [scan_remaining], 0
    je .finish_page
    push bx
    call next_line
    call write_text_line
    pop bx
    inc bx
    add word [page_y], TEXT_ROW_HEIGHT
    cmp byte [io_failed], 0
    jne .done
    jmp .line
.finish_page:
    mov ax, RASTER_HEIGHT
    sub ax, [page_y]
    call write_blank_rows
    inc word [page_index]
    mov ax, [page_index]
    cmp ax, [page_count]
    jb .page
.done:
    ret

build_page_header:
    mov di, PAGE_HEADER
    mov cx, PAGE_HEADER_SIZE
    xor al, al
    rep stosb
    mov si, pwg_media_class
    mov di, PAGE_HEADER
    call copy_header_string
    mov si, pwg_media_color
    mov di, PAGE_HEADER + 64
    call copy_header_string
    mov si, pwg_media_type
    mov di, PAGE_HEADER + 128
    call copy_header_string
    mov si, pwg_output_type
    mov di, PAGE_HEADER + 192
    call copy_header_string
    mov si, pwg_rendering_intent
    mov di, PAGE_HEADER + 1668
    call copy_header_string
    mov si, pwg_page_name
    mov di, PAGE_HEADER + 1732
    call copy_header_string

    mov di, PAGE_HEADER + 276
    mov ax, 300
    call put_be32_u16
    mov di, PAGE_HEADER + 280
    mov ax, 300
    call put_be32_u16
    mov di, PAGE_HEADER + 340
    mov ax, 1
    call put_be32_u16
    mov di, PAGE_HEADER + 352
    mov ax, 612
    call put_be32_u16
    mov di, PAGE_HEADER + 356
    mov ax, 792
    call put_be32_u16
    mov di, PAGE_HEADER + 372
    mov ax, RASTER_WIDTH
    call put_be32_u16
    mov di, PAGE_HEADER + 376
    mov ax, RASTER_HEIGHT
    call put_be32_u16
    mov di, PAGE_HEADER + 384
    mov ax, 8
    call put_be32_u16
    mov di, PAGE_HEADER + 388
    mov ax, 8
    call put_be32_u16
    mov di, PAGE_HEADER + 392
    mov ax, RASTER_WIDTH
    call put_be32_u16
    ; ColorOrder is zero (chunked), all reserved words remain zero.
    mov di, PAGE_HEADER + 400
    mov ax, 18                 ; CUPS_CSPACE_SW (sGray)
    call put_be32_u16
    mov di, PAGE_HEADER + 420
    mov ax, 1
    call put_be32_u16
    mov di, PAGE_HEADER + 452
    mov ax, [page_count]
    call put_be32_u16
    mov di, PAGE_HEADER + 456
    mov ax, 1
    call put_be32_u16
    mov di, PAGE_HEADER + 460
    mov ax, 1
    call put_be32_u16
    ret

copy_header_string:
.loop:
    lodsb
    stosb
    test al, al
    jnz .loop
    ret

put_be32_u16:
    mov byte [di], 0
    mov byte [di + 1], 0
    mov [di + 2], ah
    mov [di + 3], al
    ret

write_blank_rows:
    mov bx, ax
.group:
    test bx, bx
    jz .done
    mov ax, bx
    cmp ax, 256
    jbe .size
    mov ax, 256
.size:
    push bx
    push ax
    dec ax
    call stream_put_byte
    mov cx, 19
.full_run:
    mov al, 127
    call stream_put_byte
    mov al, 255
    call stream_put_byte
    loop .full_run
    mov al, 117                ; final 118 pixels: 19*128+118=2550
    call stream_put_byte
    mov al, 255
    call stream_put_byte
    pop ax
    pop bx
    sub bx, ax
    jmp .group
.done:
    ret

write_text_line:
    mov byte [glyph_row], 0
.raster_row:
    mov al, FONT_SCALE - 1
    call stream_put_byte
    mov word [run_length], 0
    mov al, 255
    mov cx, TOP_MARGIN
    call raster_add_run
    xor di, di
.character:
    cmp di, [line_length]
    jae .right_margin
    mov bx, LINE_BUFFER
    add bx, di
    mov al, [bx]
    xor ah, ah
    shl ax, 1
    shl ax, 1
    shl ax, 1
    add ax, [font_offset]
    xor bx, bx
    mov bl, [glyph_row]
    add bx, ax
    mov ax, [font_segment]
    mov es, ax
    mov al, [es:bx]
    mov [glyph_bits], al
    xor ax, ax
    mov es, ax
    mov cx, 8
.bit:
    mov al, 255
    test byte [glyph_bits], 0x80
    jz .pixel
    xor al, al
.pixel:
    push cx
    mov cx, FONT_SCALE
    call raster_add_run
    pop cx
    shl byte [glyph_bits], 1
    loop .bit
    inc di
    jmp .character
.right_margin:
    mov ax, [line_length]
    mov bx, 8 * FONT_SCALE
    mul bx
    add ax, TOP_MARGIN
    mov cx, RASTER_WIDTH
    sub cx, ax
    mov al, 255
    call raster_add_run
    call raster_flush_run
    inc byte [glyph_row]
    cmp byte [glyph_row], 8
    jb .raster_row
    mov ax, TEXT_ROW_HEIGHT - 8 * FONT_SCALE
    call write_blank_rows
    ret

raster_add_run:
    push ax
    push bx
    push cx
    cmp word [run_length], 0
    je .new
    cmp al, [run_color]
    je .extend
    call raster_flush_run
.new:
    mov [run_color], al
    mov [run_length], cx
    jmp .done
.extend:
    add [run_length], cx
.done:
    pop cx
    pop bx
    pop ax
    ret

raster_flush_run:
    push ax
    push bx
    mov bx, [run_length]
.part:
    test bx, bx
    jz .done
    mov ax, bx
    cmp ax, 128
    jbe .size
    mov ax, 128
.size:
    push ax
    dec ax
    call stream_put_byte
    mov al, [run_color]
    call stream_put_byte
    pop ax
    sub bx, ax
    jmp .part
.done:
    mov word [run_length], 0
    pop bx
    pop ax
    ret

; ---------------------------------------------------------------------------
; Static protocol data and module state

local_mac       db 0x52, 0x54, 0x00, 0x12, 0x34, 0x56
local_ip        db 10, 0, 2, 15
gateway_ip      db 10, 0, 2, 2
target_ip       times 4 db 0
next_hop_ip     times 4 db 0
peer_mac        times 6 db 0

uri_prefix db 'ipp://', 0
uri_suffix db ':631/ipp/print', 0
http_post db 'POST /ipp/print HTTP/1.1', 13, 10, 'Host: ', 0
http_headers db ':631', 13, 10
             db 'Content-Type: application/ipp', 13, 10
             db 'Transfer-Encoding: chunked', 13, 10
             db 'Connection: close', 13, 10
             db 'User-Agent: Nixodria/1', 13, 10, 13, 10, 0
http_response_prefix db 'HTTP/1.'
chunk_terminator db '0', 13, 10, 13, 10
chunk_terminator_end:

ipp_prefix:
    db 1, 1, 0, 2, 0, 0, 0, 1, 1
    db 0x47, 0, 18, 'attributes-charset', 0, 5, 'utf-8'
    db 0x48, 0, 27, 'attributes-natural-language', 0, 2, 'en'
ipp_prefix_end:
ipp_printer_uri_name db 'printer-uri'
ipp_middle:
    db 0x42, 0, 20, 'requesting-user-name', 0, 8, 'nixodria'
    db 0x49, 0, 15, 'document-format', 0, 16, 'image/pwg-raster'
ipp_middle_end:
ipp_job_name db 'job-name'

pwg_magic db 'RaS2'
pwg_media_class db 'PwgRaster', 0
pwg_media_color db 'white', 0
pwg_media_type db 'stationery', 0
pwg_output_type db 'text', 0
pwg_rendering_intent db 'auto', 0
pwg_page_name db 'na_letter_8.5x11in', 0

file_pointer            dw 0
file_length             dw 0
file_name               dw 0
file_name_length        dw 0
font_segment            dw 0
font_offset             dw 0
scan_pointer            dw 0
scan_remaining          dw 0
line_length             dw 0
count_page_lines        dw 0
page_count              dw 1
page_index              dw 0
page_y                  dw 0
ip_ascii_length         dw 0
uri_length              dw 0

stream_length           dw 0
response_length         dw 0
io_failed               db 0
response_collecting     db 0
glyph_row               db 0
glyph_bits              db 0
run_color               db 0
run_length              dw 0

wait_start              dw 0
retry_count             db 0
ip_identifier           dw 0x4e49
local_port              dw 0
local_sequence_low      dw 0
local_sequence_high     dw 0
remote_sequence_low     dw 0
remote_sequence_high    dw 0
expected_ack_low        dw 0
expected_ack_high       dw 0
remote_fin              db 0

send_pointer            dw 0
send_length             dw 0
send_flags              db 0
packet_payload_length   dw 0
tcp_length              dw 0
checksum_sum            dw 0

tx_frame_length         dw 0
dma_length              dw 0
remote_address          dw 0
remote_length           dw 0
ring_address            dw 0
ring_length             dw 0
ring_destination        dw 0
ring_split_length       dw 0
rx_page                 db 0
rx_current              db 0
rx_next                 db NE_RX_START + 1
rx_frame_length         dw 0

rx_ip_header_length     dw 0
rx_ip_total_length      dw 0
rx_tcp_header_length    dw 0
rx_tcp_segment_length   dw 0
rx_sequence_low         dw 0
rx_sequence_high        dw 0
rx_ack_low              dw 0
rx_ack_high             dw 0
rx_tcp_flags            db 0
rx_data_pointer         dw 0
rx_data_length          dw 0

times (32 * 512) - ($ - $$) db 0
