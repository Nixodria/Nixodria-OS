//! Nixodria's guest shell. All compilers, editors, programs, and print clients
//! invoked here run inside the Linux guest and share its ordinary filesystem.

use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{self, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const HELP: &str = "Nixodria — native Rust development\n\
  files [directory]        List files in the workspace\n\
  edit [filename]          Edit a file with vi (or $EDITOR)\n\
  run <file.rs> [args...]  Compile with guest rustc, then run\n\
  cargo <args...>          Run the guest Cargo toolchain\n\
  rustc <args...>          Run the guest Rust compiler\n\
  pkg list                List bundled editable Rust programs\n\
  pkg install <file.rs>    Copy a bundled program into this directory\n\
  pkg remove <file.rs>     Delete an installed program in this directory\n\
  printer [queue|IP|URI]   Show or configure a guest CUPS printer\n\
  print <filename>        Submit a saved file through guest CUPS\n\
  cd [directory]          Change directory (default: home)\n\
  pwd | clear | echo ...  Basic shell commands\n\
  help | version | exit   Shell information and exit\n\
  halt | poweroff         Shut down the guest and exit QEMU\n\
  reboot                  Restart the guest\n\n\
Other commands run directly. Quotes and backslash escapes are supported.\n\
Use sh -c '...' for pipes, redirection, or environment expansion.\n\
In vi: press i to edit, Escape then :wq to save and quit.\n\
Ctrl-C interrupts a running program and returns here. Escape belongs to\n\
the running program; it is not a system-wide interrupt.\n";

static READING_INPUT: AtomicBool = AtomicBool::new(false);
static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[cfg(unix)]
mod signals {
    use super::{Ordering, READING_INPUT};
    use std::ffi::{c_int, c_void};

    // signal() is POSIX on the Linux guest and macOS development hosts. Caught
    // handlers are reset to SIG_DFL by exec, so child programs retain normal
    // Ctrl-C handling while this shell survives the same foreground signal.
    unsafe extern "C" {
        fn signal(number: c_int, handler: usize) -> usize;
        fn write(fd: c_int, data: *const c_void, length: usize) -> isize;
    }

    extern "C" fn interrupt(_: c_int) {
        if READING_INPUT.load(Ordering::Relaxed) {
            let prompt = b"\nnix> ";
            // write is async-signal-safe; do not use Rust's locked stdout here.
            unsafe { write(1, prompt.as_ptr().cast(), prompt.len()) };
        }
    }

    pub fn install() -> std::io::Result<()> {
        const SIGINT: c_int = 2;
        const SIG_ERR: usize = usize::MAX;
        if unsafe { signal(SIGINT, interrupt as *const () as usize) } == SIG_ERR {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}

fn main() {
    if let Err(error) = main_result() {
        eprintln!("nixodria: {error}");
        process::exit(1);
    }
}

fn main_result() -> io::Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    match args.as_slice() {
        [arg] if arg == "--version" || arg == "-V" => {
            println!("Nixodria {VERSION}");
            return Ok(());
        }
        [arg] if arg == "--help" || arg == "-h" => {
            print!("{HELP}\nUsage: nixodria [-c 'command']\n");
            return Ok(());
        }
        [] => {}
        [option, _] if option == "-c" => {}
        _ => return Err(invalid("usage: nixodria [-c 'command'] | --version")),
    }

    #[cfg(unix)]
    signals::install()?;

    if args.len() == 2 {
        let action = execute(&args[1])?;
        process::exit(action.code());
    }

    println!("Nixodria {VERSION} — Rust and Cargo run inside this guest.");
    println!(
        "Type help for commands. Your workspace: {}",
        env::current_dir()?.display()
    );
    let stdin = io::stdin();
    loop {
        print!("nix> ");
        io::stdout().flush()?;
        let mut line = String::new();
        READING_INPUT.store(true, Ordering::Relaxed);
        let read = stdin.read_line(&mut line);
        READING_INPUT.store(false, Ordering::Relaxed);
        match read {
            Ok(0) => {
                println!();
                return Ok(());
            }
            Ok(_) => match execute(&line) {
                Ok(Action::Exit(code)) => process::exit(code),
                Ok(Action::Continue(_)) => {}
                Err(error) => eprintln!("nixodria: {error}"),
            },
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

#[derive(Debug, PartialEq)]
enum Action {
    Continue(i32),
    Exit(i32),
}

impl Action {
    fn code(self) -> i32 {
        match self {
            Self::Continue(code) | Self::Exit(code) => code,
        }
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

/// Split a command without evaluating shell operators or environment values.
/// Keeping argument boundaries avoids option/shell injection in our builtins.
fn split_command(line: &str) -> io::Result<Vec<String>> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut started = false;
    let mut escaped = false;
    for character in line.trim_end_matches(['\r', '\n']).chars() {
        if escaped {
            word.push(character);
            escaped = false;
            continue;
        }
        match character {
            '\\' if quote != Some('\'') => {
                escaped = true;
                started = true;
            }
            '\'' | '"' if quote == Some(character) => quote = None,
            '\'' | '"' if quote.is_none() => {
                quote = Some(character);
                started = true;
            }
            value if value.is_whitespace() && quote.is_none() => {
                if started {
                    words.push(std::mem::take(&mut word));
                    started = false;
                }
            }
            value => {
                word.push(value);
                started = true;
            }
        }
    }
    if escaped || quote.is_some() {
        return Err(invalid("unfinished quote or backslash in command"));
    }
    if started {
        words.push(word);
    }
    Ok(words)
}

fn execute(line: &str) -> io::Result<Action> {
    let words = split_command(line)?;
    let Some((name, args)) = words.split_first() else {
        return Ok(Action::Continue(0));
    };
    let code = match name.as_str() {
        "help" => {
            require_count(args, 0, "help")?;
            print!("{HELP}");
            0
        }
        "version" => {
            require_count(args, 0, "version")?;
            println!("Nixodria {VERSION}");
            0
        }
        "exit" => {
            if args.len() > 1 {
                return Err(invalid("usage: exit [status]"));
            }
            let code = args.first().map_or(Ok(0), |arg| {
                arg.parse::<u8>()
                    .map(i32::from)
                    .map_err(|_| invalid("exit status must be 0..255"))
            })?;
            return Ok(Action::Exit(code));
        }
        "files" => {
            if args.len() > 1 {
                return Err(invalid("usage: files [directory]"));
            }
            list_files(Path::new(args.first().map_or(".", String::as_str)))?;
            0
        }
        "pwd" => {
            require_count(args, 0, "pwd")?;
            println!("{}", env::current_dir()?.display());
            0
        }
        "cd" => {
            if args.len() > 1 {
                return Err(invalid("usage: cd [directory]"));
            }
            let path = args.first().map(PathBuf::from).unwrap_or_else(home);
            env::set_current_dir(path)?;
            0
        }
        "clear" => {
            require_count(args, 0, "clear")?;
            print!("\x1b[2J\x1b[H");
            io::stdout().flush()?;
            0
        }
        "echo" => {
            println!("{}", args.join(" "));
            0
        }
        "edit" => edit(args)?,
        "run" => run_source(args)?,
        "pkg" => package(args)?,
        "printer" => printer(args)?,
        "print" => print_file(args)?,
        "halt" => {
            require_count(args, 0, "halt")?;
            run_command(&mut Command::new("poweroff"))?
        }
        _ => run_command(Command::new(name).args(args))?,
    };
    Ok(Action::Continue(code))
}

fn require_count(args: &[String], count: usize, usage: &str) -> io::Result<()> {
    if args.len() == count {
        Ok(())
    } else {
        Err(invalid(format!("usage: {usage}")))
    }
}

fn home() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/root"))
}

fn list_files(directory: &Path) -> io::Result<()> {
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(|entry| entry.file_name());
    if entries.is_empty() {
        println!("(no files)");
    }
    for entry in entries {
        let suffix = if entry.file_type()?.is_dir() { "/" } else { "" };
        println!("{}{suffix}", entry.file_name().to_string_lossy());
    }
    Ok(())
}

/// A child may change terminal modes and then be interrupted before restoring
/// them. Snapshot and restore the terminal around every foreground command.
struct TerminalState(Option<String>);

impl TerminalState {
    fn save() -> Self {
        let state = Command::new("stty")
            .arg("-g")
            .stdin(Stdio::inherit())
            .stderr(Stdio::null())
            .output()
            .ok()
            .filter(|output| output.status.success())
            .and_then(|output| String::from_utf8(output.stdout).ok());
        Self(state)
    }
}

impl Drop for TerminalState {
    fn drop(&mut self) {
        if let Some(state) = &self.0 {
            let _ = Command::new("stty")
                .arg(state.trim())
                .stdin(Stdio::inherit())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    }
}

fn run_command(command: &mut Command) -> io::Result<i32> {
    io::stdout().flush()?;
    let _terminal = TerminalState::save();
    let program = command.get_program().to_string_lossy().into_owned();
    let status = command.status().map_err(|error| {
        io::Error::new(error.kind(), format!("could not start {program}: {error}"))
    })?;
    Ok(status_code(status))
}

fn status_code(status: ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        128 + status.signal().unwrap_or(1)
    }
    #[cfg(not(unix))]
    {
        1
    }
}

fn edit(args: &[String]) -> io::Result<i32> {
    if args.len() > 1 {
        return Err(invalid("usage: edit [filename]"));
    }
    let configured = env::var("EDITOR").unwrap_or_else(|_| "vi".into());
    let editor = split_command(&configured)?;
    let Some((program, editor_args)) = editor.split_first() else {
        return Err(invalid("EDITOR must name an editor executable"));
    };
    // An absolute filename cannot be mistaken for a vi option, even for a
    // workspace filename beginning with '-'. BusyBox vi need not support --.
    let filename = env::current_dir()?.join(args.first().map_or("untitled.rs", String::as_str));
    run_command(Command::new(program).args(editor_args).arg(filename))
}

struct TempDirectory(PathBuf);

impl TempDirectory {
    fn create() -> io::Result<Self> {
        let builder = fs::DirBuilder::new();
        #[cfg(unix)]
        let builder = {
            use std::os::unix::fs::DirBuilderExt;
            let mut builder = builder;
            // Compiled output belongs only to this user, even when /tmp is
            // shared. Set permissions at creation, before rustc writes files.
            builder.mode(0o700);
            builder
        };
        for _ in 0..100 {
            let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let path = env::temp_dir().join(format!("nixodria-{}-{sequence}", process::id()));
            match builder.create(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "could not allocate a compiler output directory",
        ))
    }
}

impl Drop for TempDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn run_source(args: &[String]) -> io::Result<i32> {
    let Some((filename, program_args)) = args.split_first() else {
        return Err(invalid("usage: run <file.rs> [args...]"));
    };
    let source = fs::canonicalize(filename)?;
    if !source.is_file() || source.extension().and_then(|part| part.to_str()) != Some("rs") {
        return Err(invalid(
            "run expects a saved .rs source file; use cargo run for a Cargo project",
        ));
    }
    let temporary = TempDirectory::create()?;
    let executable = temporary.0.join("app");
    let code = run_command(
        Command::new("rustc")
            .arg("--edition=2024")
            .arg("--crate-name=nixodria_app")
            .arg(&source)
            .arg("-o")
            .arg(&executable),
    )?;
    if code != 0 {
        return Ok(code);
    }
    run_command(Command::new(&executable).args(program_args))
}

fn package_directory() -> PathBuf {
    env::var_os("NIXODRIA_APPS")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/share/nixodria/apps"))
}

fn package_name(name: &str) -> io::Result<&Path> {
    let path = Path::new(name);
    let mut parts = path.components();
    if !matches!(parts.next(), Some(Component::Normal(_)))
        || parts.next().is_some()
        || path.extension().and_then(|extension| extension.to_str()) != Some("rs")
        || name.starts_with('.')
        || name.starts_with('-')
    {
        return Err(invalid(
            "package name must be a plain .rs filename, such as hello.rs",
        ));
    }
    Ok(path)
}

fn install_package(catalog: &Path, workspace: &Path, name: &str) -> io::Result<()> {
    let name = package_name(name)?;
    let source = catalog.join(name);
    if !source.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("no bundled package named {}", name.display()),
        ));
    }
    let bytes = fs::read(source)?;
    let target = workspace.join(name);
    let mut destination = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&target)
        .map_err(|error| {
            if error.kind() == io::ErrorKind::AlreadyExists {
                io::Error::new(
                    error.kind(),
                    format!(
                        "{} already exists; keeping your local edits",
                        target.display()
                    ),
                )
            } else {
                error
            }
        })?;
    if let Err(error) = destination
        .write_all(&bytes)
        .and_then(|_| destination.sync_all())
    {
        drop(destination);
        let _ = fs::remove_file(target);
        return Err(error);
    }
    Ok(())
}

fn package(args: &[String]) -> io::Result<i32> {
    match args {
        [action] if action == "list" => {
            let mut names = fs::read_dir(package_directory())?
                .filter_map(|entry| entry.ok())
                .filter(|entry| entry.path().is_file())
                .map(|entry| entry.file_name())
                .filter(|name| name.to_str().is_some_and(|name| package_name(name).is_ok()))
                .collect::<Vec<_>>();
            names.sort();
            for name in names {
                println!("{}", name.to_string_lossy());
            }
        }
        [action, name] if action == "install" => {
            install_package(&package_directory(), &env::current_dir()?, name)?;
            println!("Installed {name}. Edit it with edit {name}; run it with run {name}.");
        }
        [action, name] if action == "remove" => {
            fs::remove_file(package_name(name)?)?;
            println!("Removed {name}.");
        }
        _ => {
            return Err(invalid(
                "usage: pkg list | pkg install <file.rs> | pkg remove <file.rs>",
            ));
        }
    }
    Ok(0)
}

fn printer_config() -> PathBuf {
    home().join(".config/nixodria/printer")
}

fn printer_destination() -> io::Result<Option<String>> {
    match fs::read_to_string(printer_config()) {
        Ok(value) => Ok(Some(value.trim().to_owned())),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

/// Return a validated IPP URI, or a CUPS queue name. No input is passed through
/// a shell, and destination names cannot be interpreted as client options.
fn parse_printer(value: &str) -> io::Result<(String, bool)> {
    if let Ok(address) = value.parse::<std::net::Ipv4Addr>() {
        return Ok((format!("ipp://{address}:631/ipp/print"), true));
    }
    if value.starts_with("ipp://") || value.starts_with("ipps://") {
        let authority = value
            .split_once("://")
            .unwrap()
            .1
            .split('/')
            .next()
            .unwrap();
        if authority.is_empty()
            || value
                .chars()
                .any(|character| character.is_whitespace() || character.is_control())
        {
            return Err(invalid(
                "printer URI must be an ipp:// or ipps:// address without whitespace",
            ));
        }
        return Ok((value.to_owned(), true));
    }
    if value.is_empty()
        || value.starts_with('-')
        || !value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "_.-".contains(character))
    {
        return Err(invalid(
            "printer expects a CUPS queue name, IPv4 address, or ipp(s):// URI",
        ));
    }
    Ok((value.to_owned(), false))
}

fn printer(args: &[String]) -> io::Result<i32> {
    match args {
        [] => match printer_destination()? {
            Some(destination) => println!("Printer: {destination}"),
            None => println!("Printer: CUPS default (set one with printer <queue|IP|URI>)"),
        },
        [value] => {
            let (destination, uri) = parse_printer(value)?;
            let queue = if uri {
                let code = run_command(Command::new("lpadmin").args([
                    "-p",
                    "nixodria",
                    "-E",
                    "-v",
                    &destination,
                    "-m",
                    "everywhere",
                ]))?;
                if code != 0 {
                    eprintln!(
                        "nixodria: CUPS could not configure this printer; the previous setting was kept"
                    );
                    return Ok(code);
                }
                "nixodria"
            } else {
                destination.as_str()
            };
            let config = printer_config();
            fs::create_dir_all(config.parent().unwrap())?;
            fs::write(config, format!("{queue}\n"))?;
            println!("Printer configured: {queue}");
        }
        _ => return Err(invalid("usage: printer [CUPS-queue|IPv4|ipp(s)://URI]")),
    }
    Ok(0)
}

fn executable_on_path(name: &str) -> bool {
    env::split_paths(&env::var_os("PATH").unwrap_or_default())
        .any(|directory| directory.join(name).is_file())
}

fn print_file(args: &[String]) -> io::Result<i32> {
    require_count(args, 1, "print <filename>")?;
    let source = fs::canonicalize(&args[0])?;
    if !source.is_file() {
        return Err(invalid("print expects a saved regular file"));
    }
    let destination = printer_destination()?;
    let mut command = if executable_on_path("lp") {
        let mut command = Command::new("lp");
        if let Some(destination) = destination {
            command.args(["-d", &destination]);
        }
        command
    } else if executable_on_path("lpr") {
        let mut command = Command::new("lpr");
        if let Some(destination) = destination {
            command.args(["-P", &destination]);
        }
        command
    } else {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "CUPS clients lp/lpr are not installed in this guest",
        ));
    };
    // canonicalize provides an absolute path, avoiding client option parsing.
    run_command(command.arg(source))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn compiler_output_directory_is_private() {
        use std::os::unix::fs::PermissionsExt;
        let temporary = TempDirectory::create().unwrap();
        assert_eq!(
            fs::metadata(&temporary.0).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn arguments_preserve_quotes_empty_strings_and_literal_shell_syntax() {
        assert_eq!(
            split_command("run 'my file.rs' \"\" a\\ b '$HOME;$(id)'\r\n").unwrap(),
            ["run", "my file.rs", "", "a b", "$HOME;$(id)"]
        );
        assert_eq!(
            split_command("echo 'a\\b'\"c d\"").unwrap(),
            ["echo", "a\\bc d"]
        );
        assert!(split_command("run 'unfinished").is_err());
        assert!(split_command("run trailing\\").is_err());
        assert!(split_command(" \t\r\n").unwrap().is_empty());
    }

    #[test]
    fn package_names_cannot_escape_the_workspace() {
        for name in [
            "../hello.rs",
            "/hello.rs",
            "sub/hello.rs",
            ".hidden.rs",
            "-bad.rs",
            "hello.bas",
            "",
        ] {
            assert!(package_name(name).is_err(), "accepted {name}");
        }
        assert_eq!(package_name("hello.rs").unwrap(), Path::new("hello.rs"));
    }

    #[test]
    fn install_preserves_existing_edits_and_rejects_missing_packages() {
        let temporary = TempDirectory::create().unwrap();
        let catalog = temporary.0.join("catalog");
        let workspace = temporary.0.join("workspace");
        fs::create_dir(&catalog).unwrap();
        fs::create_dir(&workspace).unwrap();
        fs::write(catalog.join("hello.rs"), "fn main() {}\n").unwrap();
        install_package(&catalog, &workspace, "hello.rs").unwrap();
        fs::write(workspace.join("hello.rs"), "my local edits").unwrap();
        assert_eq!(
            install_package(&catalog, &workspace, "hello.rs")
                .unwrap_err()
                .kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(
            fs::read_to_string(workspace.join("hello.rs")).unwrap(),
            "my local edits"
        );
        assert!(install_package(&catalog, &workspace, "missing.rs").is_err());
        assert!(install_package(&catalog, &workspace, "../hello.rs").is_err());
    }

    #[test]
    fn printer_values_distinguish_queues_and_ipp_destinations() {
        assert_eq!(
            parse_printer("192.168.1.42").unwrap(),
            ("ipp://192.168.1.42:631/ipp/print".into(), true)
        );
        assert_eq!(
            parse_printer("office-2").unwrap(),
            ("office-2".into(), false)
        );
        assert!(parse_printer("ipps://printer.local/ipp/print").unwrap().1);
        for value in [
            "-E",
            "",
            "ipp:///print",
            "office;reboot",
            "ipp://host/ bad",
            "ipp://host/\n",
        ] {
            assert!(parse_printer(value).is_err(), "accepted {value:?}");
        }
    }

    #[test]
    fn run_uses_real_rustc_and_preserves_arguments() {
        let temporary = TempDirectory::create().unwrap();
        let source = temporary.0.join("full rust.rs");
        let output = temporary.0.join("result.txt");
        fs::write(&source, r#"
            use std::{env, fs};
            fn main() {
                let values: Vec<u32> = (1..=4).map(|n| n * n).collect();
                let answer = match values.as_slice() { [1, 4, 9, 16] => "native Rust", _ => panic!("bad result") };
                fs::write(env::args().nth(1).unwrap(), answer).unwrap();
            }
        "#).unwrap();
        assert_eq!(
            run_source(&[
                source.to_string_lossy().into(),
                output.to_string_lossy().into()
            ])
            .unwrap(),
            0
        );
        assert_eq!(fs::read_to_string(output).unwrap(), "native Rust");
    }

    #[test]
    fn compilation_error_does_not_execute_a_stale_program() {
        let temporary = TempDirectory::create().unwrap();
        let source = temporary.0.join("broken.rs");
        fs::write(&source, "fn main() { this is not Rust }").unwrap();
        assert_ne!(run_source(&[source.to_string_lossy().into()]).unwrap(), 0);
    }
}
