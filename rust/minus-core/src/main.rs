//! `minus-cored`: the Rust control-plane process.
//!
//! Serves the NDJSON control socket with `hello`, `ping`, `get_status`,
//! `get_config` and `set_config`, so the existing Python `ControlClient`, the
//! dashboard's settings panel and `minus model` can all drive it unchanged.
//!
//! It deliberately does *not* answer `say`, `interrupt`, `end_conversation` or
//! `reload_plugins`: there is no conversation loop and no Lua host in this
//! build, and answering those with a success would be a lie. They come back as
//! `unknown_command`, which a client can tell apart from a refusal.
//!
//! Run it with `--socket PATH`, or `MINUS_CONTROL_SOCKET`, or the same
//! `$XDG_RUNTIME_DIR/minus/control.sock` the Python assistant uses. Only one
//! process may own that path; the probe in `ControlServer::claim` enforces it.

use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

use minus_core::config::{self, Config};
use minus_core::control::{default_handlers, ControlServer, State};

fn socket_path() -> PathBuf {
    if let Ok(value) = std::env::var("MINUS_CONTROL_SOCKET") {
        if !value.trim().is_empty() {
            return PathBuf::from(value);
        }
    }
    match std::env::var("XDG_RUNTIME_DIR") {
        Ok(runtime) if !runtime.trim().is_empty() => {
            PathBuf::from(runtime).join("minus").join("control.sock")
        }
        _ => PathBuf::from("/tmp").join("minus").join("control.sock"),
    }
}

fn main() {
    let mut socket = socket_path();
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--socket" => match args.next() {
                Some(path) => socket = PathBuf::from(path),
                None => {
                    eprintln!("--socket needs a path");
                    std::process::exit(2);
                }
            },
            "--help" | "-h" => {
                println!(
                    "minus-cored [--socket PATH]\n\n\
                     Serves the MINUS NDJSON control protocol. Commands: hello, ping, \
                     get_status, get_config, set_config, subscribe."
                );
                return;
            }
            other => {
                eprintln!("unknown argument {other:?}; try --help");
                std::process::exit(2);
            }
        }
    }

    let settings = match Config::load() {
        Ok(settings) => settings,
        Err(problems) => {
            eprintln!("Refusing to start: {problems}");
            std::process::exit(1);
        }
    };
    let env_path = config::project_root().join(".env");

    let state = Arc::new(State::new());
    let config = Arc::new(Mutex::new(settings));
    let handlers = default_handlers(Arc::clone(&config), Arc::clone(&state), env_path);
    let server = Arc::new(ControlServer::bind(socket.clone(), handlers, state));

    println!("minus-cored listening on {}", socket.display());
    use std::io::Write;
    let _ = std::io::stdout().flush();

    install_signal_handlers();
    if let Err(error) = server.serve(&STOP) {
        eprintln!("control server stopped: {error}");
        std::process::exit(1);
    }
    println!("minus-cored stopped");
}

/// Set from the signal handler, read by the accept loop. A signal handler may
/// only touch async-signal-safe state, and a static atomic is the one thing
/// here that is.
static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_signal: libc::c_int) {
    STOP.store(true, std::sync::atomic::Ordering::SeqCst);
}

fn install_signal_handlers() {
    // SIGTERM is what `systemctl stop` sends, and SIGINT what Ctrl-C sends.
    // Both end the loop so the socket is unlinked rather than left behind for
    // the next start to clean up.
    unsafe {
        let handler = on_signal as *const () as libc::sighandler_t;
        libc::signal(libc::SIGTERM, handler);
        libc::signal(libc::SIGINT, handler);
    }
}
