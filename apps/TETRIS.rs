//! Nixodria's editable, standalone Rust Tetris application.
//!
//! Compile and run inside Nixodria: `run TETRIS.rs`.
//! Run its rules tests: `rustc --test TETRIS.rs -o tetris-tests && ./tetris-tests`.
//! Uses only Rust's standard library and the system's `stty` terminal utility.

use std::io::{self, IsTerminal, Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const WIDTH: usize = 10;
const HEIGHT: usize = 20;
const FULL_ROW: u16 = (1 << WIDTH) - 1;
// The original BASIC game's seven pieces, in a four-bit-wide row representation.
const SHAPES: [u16; 7] = [15, 51, 39, 54, 99, 113, 116];

#[derive(Clone, Copy, Debug, PartialEq)]
struct Piece {
    shape: u16,
    kind: usize,
    x: i32,
    y: i32,
}

impl Piece {
    fn cells(self) -> impl Iterator<Item = (i32, i32)> {
        (0..16).filter_map(move |bit| {
            (self.shape & (1 << bit) != 0).then_some((self.x + bit % 4, self.y + bit / 4))
        })
    }

    fn rotated(self) -> Self {
        if self.kind == 1 {
            return self; // The square does not change its position when rotated.
        }
        let size = if self.kind == 0 { 4 } else { 3 };
        let mut shape = 0;
        for y in 0..size {
            for x in 0..size {
                if self.shape & (1 << (y * 4 + x)) != 0 {
                    shape |= 1 << (x * 4 + size - 1 - y);
                }
            }
        }
        Self { shape, ..self }
    }
}

struct Game {
    rows: [u16; HEIGHT],
    piece: Piece,
    next_kind: usize,
    score: u64,
    lines: u64,
    over: bool,
}

impl Game {
    fn new(start_kind: usize) -> Self {
        let mut game = Self {
            rows: [0; HEIGHT],
            piece: Piece {
                shape: 0,
                kind: 0,
                x: 3,
                y: 0,
            },
            next_kind: start_kind % SHAPES.len(),
            score: 0,
            lines: 0,
            over: false,
        };
        game.spawn();
        game
    }

    fn fits(&self, piece: Piece) -> bool {
        piece.cells().all(|(x, y)| {
            x >= 0
                && x < WIDTH as i32
                && y >= 0
                && y < HEIGHT as i32
                && self.rows[y as usize] & (1 << x) == 0
        })
    }

    fn spawn(&mut self) {
        let kind = self.next_kind;
        self.next_kind = (kind + 1) % SHAPES.len();
        self.piece = Piece {
            shape: SHAPES[kind],
            kind,
            x: 3,
            y: 0,
        };
        self.over = !self.fits(self.piece);
    }

    fn shift(&mut self, dx: i32, dy: i32) -> bool {
        let candidate = Piece {
            x: self.piece.x + dx,
            y: self.piece.y + dy,
            ..self.piece
        };
        if self.over || !self.fits(candidate) {
            return false;
        }
        self.piece = candidate;
        true
    }

    fn rotate(&mut self) {
        if self.over {
            return;
        }
        let rotated = self.piece.rotated();
        // Small horizontal wall kicks make turns next to a wall usable.
        for dx in [0, -1, 1, -2, 2, -3, 3] {
            let candidate = Piece {
                x: rotated.x + dx,
                ..rotated
            };
            if self.fits(candidate) {
                self.piece = candidate;
                break;
            }
        }
    }

    fn clear_lines(&mut self) {
        let mut write = HEIGHT;
        for read in (0..HEIGHT).rev() {
            if self.rows[read] == FULL_ROW {
                self.lines += 1;
                self.score += 100;
            } else {
                write -= 1;
                self.rows[write] = self.rows[read];
            }
        }
        self.rows[..write].fill(0);
    }

    fn lock(&mut self) {
        for (x, y) in self.piece.cells() {
            self.rows[y as usize] |= 1 << x;
        }
        self.clear_lines();
        self.spawn();
    }

    fn step(&mut self) {
        if !self.over && !self.shift(0, 1) {
            self.lock();
        }
    }

    fn hard_drop(&mut self) {
        if !self.over {
            while self.shift(0, 1) {}
            self.lock();
        }
    }

    fn draw(&self, out: &mut impl Write) -> io::Result<()> {
        write!(out, "\x1b[2J\x1b[H")?;
        if self.over {
            write!(
                out,
                "GAME OVER\r\nS {}\r\nL {}\r\nr restart | q/Esc/Ctrl-C exit\r\n",
                self.score, self.lines
            )?;
        } else {
            write!(out, "TETRIS\r\n")?;
            let mut rows = self.rows;
            for (x, y) in self.piece.cells() {
                rows[y as usize] |= 1 << x;
            }
            for row in rows {
                write!(out, "|")?;
                for x in 0..WIDTH {
                    write!(out, "{}", if row & (1 << x) != 0 { "#" } else { "." })?;
                }
                write!(out, "|\r\n")?;
            }
            write!(
                out,
                "S {}\r\nL {}\r\na d w s space q | Esc/Ctrl-C exit\r\n",
                self.score, self.lines
            )?;
        }
        out.flush()
    }
}

struct Terminal {
    saved: String,
}

impl Terminal {
    fn enter() -> io::Result<Self> {
        if !io::stdin().is_terminal() {
            return Err(io::Error::other("Tetris needs an interactive terminal"));
        }
        let settings = Command::new("stty")
            .arg("-g")
            .stdin(Stdio::inherit())
            .output()?;
        if !settings.status.success() {
            return Err(io::Error::other("cannot read terminal settings using stty"));
        }
        let saved = String::from_utf8(settings.stdout)
            .map_err(|_| io::Error::other("stty returned invalid terminal settings"))?;
        let terminal = Self {
            saved: saved.trim().to_owned(),
        };
        // Disable signal processing so Ctrl-C reaches the normal, restoring exit path.
        // A 100ms read timeout permits automatic gravity without an input thread.
        let status = Command::new("stty")
            .args(["raw", "-echo", "min", "0", "time", "1"])
            .stdin(Stdio::inherit())
            .status()?;
        if !status.success() {
            return Err(io::Error::other(
                "cannot enable raw terminal input using stty",
            ));
        }
        print!("\x1b[?25l");
        io::stdout().flush()?;
        Ok(terminal)
    }
}

impl Drop for Terminal {
    fn drop(&mut self) {
        let _ = Command::new("stty")
            .arg(&self.saved)
            .stdin(Stdio::inherit())
            .status();
        let mut out = io::stdout();
        let _ = write!(out, "\x1b[?25h\r\n");
        let _ = out.flush();
    }
}

fn play() -> io::Result<()> {
    let _terminal = Terminal::enter()?;
    let seed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    let mut game = Game::new(seed as usize);
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let mut fall = Instant::now();
    game.draw(&mut output)?;
    loop {
        let mut key = [0];
        let count = match input.read(&mut key) {
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        let mut redraw = count > 0;
        if count > 0 {
            match key[0] {
                b'q' | b'Q' | 3 | 4 | 27 => break,
                b'r' | b'R' if game.over => {
                    game = Game::new(game.next_kind);
                    fall = Instant::now();
                }
                b'a' | b'A' => {
                    game.shift(-1, 0);
                }
                b'd' | b'D' => {
                    game.shift(1, 0);
                }
                b'w' | b'W' => game.rotate(),
                b's' | b'S' => {
                    game.step();
                    fall = Instant::now();
                }
                b' ' => {
                    game.hard_drop();
                    fall = Instant::now();
                }
                _ => redraw = false,
            }
        }
        let fall_ms = 500_u64
            .saturating_sub((game.lines / 10).saturating_mul(40))
            .max(100);
        if !game.over && fall.elapsed() >= Duration::from_millis(fall_ms) {
            game.step();
            fall = Instant::now();
            redraw = true;
        }
        if redraw {
            game.draw(&mut output)?;
        }
    }
    Ok(())
}

fn main() {
    if let Err(error) = play() {
        eprintln!("Tetris: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn four_rotations_restore_each_tetromino() {
        for (kind, shape) in SHAPES.iter().copied().enumerate() {
            let original = Piece {
                shape,
                kind,
                x: 3,
                y: 0,
            };
            let mut rotated = original;
            for _ in 0..4 {
                rotated = rotated.rotated();
                assert_eq!(rotated.shape.count_ones(), 4);
            }
            assert_eq!(rotated, original);
        }
    }

    #[test]
    fn boundaries_and_locked_blocks_prevent_motion() {
        let mut game = Game::new(0);
        while game.shift(-1, 0) {}
        assert_eq!(game.piece.x, 0);
        assert!(!game.shift(-1, 0));
        while game.shift(1, 0) {}
        assert_eq!(game.piece.x, 6);
        game.rows[1] = FULL_ROW;
        assert!(!game.shift(0, 1));
        assert!(game.fits(game.piece));
    }

    #[test]
    fn hard_drop_locks_and_advances_to_the_next_piece() {
        let mut game = Game::new(0);
        game.hard_drop();
        assert_eq!(game.rows[HEIGHT - 1], 15 << 3);
        assert_eq!(game.piece.kind, 1);
        assert_eq!(game.piece.y, 0);
    }

    #[test]
    fn clears_adjacent_and_separated_rows_without_losing_blocks() {
        let mut game = Game::new(0);
        game.rows[19] = FULL_ROW;
        game.rows[18] = FULL_ROW;
        game.rows[17] = 5;
        game.rows[16] = FULL_ROW;
        game.rows[15] = 3;
        game.clear_lines();
        assert_eq!(game.lines, 3);
        assert_eq!(game.score, 300);
        assert_eq!(game.rows[19], 5);
        assert_eq!(game.rows[18], 3);
        assert!(game.rows[..18].iter().all(|row| *row == 0));
    }

    #[test]
    fn spawning_on_a_locked_block_ends_game_and_stops_actions() {
        let mut game = Game::new(0);
        game.rows[0] = FULL_ROW;
        game.spawn();
        assert!(game.over);
        let original = game.piece;
        let rows = game.rows;
        game.step();
        game.hard_drop();
        game.rotate();
        assert!(!game.shift(1, 0));
        assert_eq!(game.piece, original);
        assert_eq!(game.rows, rows);
    }

    #[test]
    fn rotation_near_a_wall_kicks_into_valid_space() {
        let mut game = Game::new(2);
        game.piece = game.piece.rotated();
        while game.shift(-1, 0) {}
        let shape = game.piece.shape;
        game.rotate();
        assert_ne!(shape, game.piece.shape);
        assert!(game.fits(game.piece));
    }

    #[test]
    fn rotation_rejects_all_colliding_positions() {
        let mut game = Game::new(0);
        game.rows[1] = FULL_ROW;
        let original = game.piece;
        game.rotate();
        assert_eq!(game.piece, original);
    }

    #[test]
    fn vertical_bar_at_left_wall_can_rotate_back_to_horizontal() {
        let mut game = Game::new(0);
        game.rotate();
        while game.shift(-1, 0) {}
        assert_eq!(game.piece.x, -3);
        game.rotate();
        assert_eq!(game.piece.shape, 0xf000);
        assert_eq!(game.piece.x, 0);
        assert!(game.fits(game.piece));
    }
}
