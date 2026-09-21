#!/bin/sh
set -eu
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
rustc --version
cargo --version
printf 'Nixodria guest printing\n' > "$work/print.txt"
cupsfilter -i text/plain -m application/pdf "$work/print.txt" > "$work/print.pdf"
test "$(head -c 4 "$work/print.pdf")" = '%PDF'
printf 'NIXODRIA_CUPS_FILTER_OK\n'
cat > hello.rs <<'EOF'
use std::collections::HashMap;
fn main() {
    let values = HashMap::from([("answer", 42)]);
    let thread = std::thread::spawn(move || values["answer"]);
    assert_eq!(thread.join().unwrap(), 42);
    println!("NIXODRIA_RUST_STD_OK");
}
EOF
rustc --edition=2024 hello.rs -o hello
./hello
/usr/local/bin/nixodria -c 'run hello.rs'
cargo new --name native_project native_project
cd native_project
cat > src/main.rs <<'EOF'
fn main() { println!("NIXODRIA_CARGO_OK"); }
#[test] fn native_test() { assert_eq!((0..10).sum::<u32>(), 45); }
EOF
cargo test --offline
cargo run --offline
cd /usr/src/nixodria
cargo test --offline
rustc --edition=2024 --test apps/TETRIS.rs -o "$work/tetris-tests"
"$work/tetris-tests"
printf 'NIXODRIA_GUEST_TESTS_PASSED\n'
