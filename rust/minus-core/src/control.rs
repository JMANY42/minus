//! The control socket, mirrored from `src/minus/control/server.py`.
//!
//! Knows nothing about conversations or models: it is handed a table of
//! `command -> handler` and a `State` to report, which keeps every decision
//! about *what* a command means in the caller. Threads rather than an event
//! loop because there is one dashboard and occasionally two, and a thread per
//! connection is easier to read than a poll loop.
//!
//! This build has no event subscribers beyond the single status frame a
//! `subscribe` receives: nothing here yet has a phase that could change. The
//! frame shape is the same, so a dashboard reading it needs no special case.

use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::config::{self, Config};
use crate::envfile::update_env_file;
use crate::protocol::{self, ProtocolError};

pub const SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");

pub type Handler = Arc<dyn Fn(&Value) -> Result<Value, ProtocolError> + Send + Sync>;

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

/// What the assistant is doing, as far as this process knows.
pub struct State {
    pub pid: u32,
    pub started_at: f64,
    phase: Mutex<String>,
    seq: AtomicU64,
}

impl State {
    pub fn new() -> Self {
        Self {
            pid: std::process::id(),
            started_at: now(),
            phase: Mutex::new("idle".to_string()),
            seq: AtomicU64::new(0),
        }
    }

    pub fn snapshot(&self) -> Value {
        let phase = self.phase.lock().map(|value| value.clone()).unwrap_or_default();
        let seq = self.seq.load(Ordering::SeqCst);
        json!({
            "phase": phase,
            "seq": seq,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_seconds": now() - self.started_at,
        })
    }
}

impl Default for State {
    fn default() -> Self {
        Self::new()
    }
}

pub struct ControlServer {
    path: PathBuf,
    handlers: HashMap<String, Handler>,
    state: Arc<State>,
}

impl ControlServer {
    /// Build the server. Nothing is bound until `serve`, so constructing one is
    /// free of side effects beyond creating the socket's parent directory.
    pub fn bind(
        path: impl Into<PathBuf>,
        handlers: HashMap<String, Handler>,
        state: Arc<State>,
    ) -> Self {
        Self {
            path: path.into(),
            handlers,
            state,
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn state(&self) -> &Arc<State> {
        &self.state
    }

    /// One decoded request in, one response frame out.
    pub fn handle(&self, payload: &Value) -> Value {
        let request_id = payload.get("id").and_then(Value::as_str).map(str::to_string);

        let Some(command) = payload.get("command").and_then(Value::as_str) else {
            return protocol::error(
                request_id.as_deref(),
                protocol::BAD_PARAMS,
                "command must be a string",
            );
        };

        let params = payload.get("params").cloned().unwrap_or_else(|| json!({}));
        if !params.is_object() {
            return protocol::error(
                request_id.as_deref(),
                protocol::BAD_PARAMS,
                "params must be an object",
            );
        }

        match self.handlers.get(command) {
            None => protocol::error(
                request_id.as_deref(),
                protocol::UNKNOWN_COMMAND,
                format!("Unknown command {command:?}"),
            ),
            Some(handler) => match handler(&params) {
                Ok(result) => protocol::response(request_id.as_deref(), result),
                Err(error) => protocol::error(request_id.as_deref(), error.code, error.message),
            },
        }
    }

    /// Take ownership of the socket path, or refuse to start.
    ///
    /// A socket file left by a crashed process is indistinguishable from a live
    /// one by inspection, so ask it: anything that accepts the connection owns
    /// the microphone, and a second assistant fighting it for the device is a
    /// failure mode worth making impossible.
    fn claim(&self) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).ok();
        }

        if self.path.exists() {
            match UnixStream::connect(&self.path) {
                Ok(_) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::AddrInUse,
                        format!(
                            "Another MINUS is already listening on {}. Stop it first, \
                             or pass --no-control to run without one.",
                            self.path.display()
                        ),
                    ));
                }
                Err(_) => {
                    fs::remove_file(&self.path).ok();
                }
            }
        }
        Ok(())
    }

    /// Accept connections until `stop` is set, then unlink the socket.
    pub fn serve(self: &Arc<Self>, stop: &AtomicBool) -> std::io::Result<()> {
        self.claim()?;

        let listener = UnixListener::bind(&self.path)?;
        // Best effort: the 0700 parent directory is the real protection, and
        // there is no way to bind atomically with a mode.
        fs::set_permissions(&self.path, fs::Permissions::from_mode(0o600)).ok();
        listener.set_nonblocking(true)?;

        while !stop.load(Ordering::SeqCst) {
            match listener.accept() {
                Ok((stream, _)) => {
                    let server = Arc::clone(self);
                    thread::spawn(move || {
                        if let Err(error) = serve_connection(stream, server) {
                            eprintln!("control connection ended: {error}");
                        }
                    });
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(20));
                }
                Err(error) => {
                    fs::remove_file(&self.path).ok();
                    return Err(error);
                }
            }
        }

        fs::remove_file(&self.path).ok();
        Ok(())
    }
}

fn serve_connection(stream: UnixStream, server: Arc<ControlServer>) -> std::io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut writer = stream;

    loop {
        let mut buffer = Vec::new();
        let read = reader.read_until(b'\n', &mut buffer)?;
        if read == 0 {
            return Ok(());
        }
        if buffer.len() > protocol::MAX_LINE_BYTES {
            let frame = protocol::error(
                None,
                protocol::OVERSIZED,
                "Frame exceeds the maximum line length",
            );
            writer.write_all(&protocol::encode(&frame))?;
            return Ok(());
        }
        if buffer.iter().all(|byte| byte.is_ascii_whitespace()) {
            continue;
        }

        let payload = match protocol::decode(&buffer) {
            Ok(payload) => payload,
            Err(error) => {
                let frame = protocol::error(None, error.code, error.message);
                writer.write_all(&protocol::encode(&frame))?;
                continue;
            }
        };

        // `subscribe` is the connection's own business rather than a handler's:
        // it is about this socket, not about the assistant.
        if payload.get("command").and_then(Value::as_str) == Some("subscribe") {
            let frame = protocol::response(
                payload.get("id").and_then(Value::as_str),
                json!({"subscribed": true}),
            );
            writer.write_all(&protocol::encode(&frame))?;
            let event = protocol::event("status", server.state.snapshot(), 1);
            writer.write_all(&protocol::encode(&event))?;
            continue;
        }

        let frame = server.handle(&payload);
        writer.write_all(&protocol::encode(&frame))?;
    }
}

/// The commands this slice answers, with the same semantics as
/// `assembly.build_control_handlers`. Everything else is `unknown_command`
/// rather than a lie: the conversation loop and the Lua host are not here yet.
pub fn default_handlers(
    config: Arc<Mutex<Config>>,
    state: Arc<State>,
    env_path: PathBuf,
) -> HashMap<String, Handler> {
    let mut handlers: HashMap<String, Handler> = HashMap::new();

    let hello_state = Arc::clone(&state);
    handlers.insert(
        "hello".into(),
        Arc::new(move |_params: &Value| {
            Ok(json!({
                "server_version": SERVER_VERSION,
                "protocol": protocol::PROTOCOL_VERSION,
                "pid": hello_state.pid,
                "started_at": hello_state.started_at,
            }))
        }),
    );

    handlers.insert(
        "ping".into(),
        Arc::new(|_params: &Value| Ok(json!({"pong": true}))),
    );

    let status_state = Arc::clone(&state);
    handlers.insert(
        "get_status".into(),
        Arc::new(move |_params: &Value| Ok(status_state.snapshot())),
    );

    let describe_config = Arc::clone(&config);
    handlers.insert(
        "get_config".into(),
        Arc::new(move |_params: &Value| {
            let config = describe_config
                .lock()
                .map_err(|_| ProtocolError::new("config lock poisoned", protocol::INTERNAL))?;
            Ok(config.describe())
        }),
    );

    let write_config = Arc::clone(&config);
    handlers.insert(
        "set_config".into(),
        Arc::new(move |params: &Value| {
            set_config(&write_config, params, &env_path)
        }),
    );

    handlers
}

fn set_config(
    config: &Mutex<Config>,
    params: &Value,
    env_path: &Path,
) -> Result<Value, ProtocolError> {
    let Some(values) = params.get("values").and_then(Value::as_object) else {
        return Err(ProtocolError::new(
            "values must be a non-empty object",
            protocol::BAD_PARAMS,
        ));
    };
    if values.is_empty() {
        return Err(ProtocolError::new(
            "values must be a non-empty object",
            protocol::BAD_PARAMS,
        ));
    }
    let persist = params.get("persist").and_then(Value::as_bool).unwrap_or(true);

    let mut applied: Vec<Value> = Vec::new();
    let mut restart_required: Vec<Value> = Vec::new();
    let mut rejected = Map::new();
    let mut changes = Map::new();

    {
        let mut config = config
            .lock()
            .map_err(|_| ProtocolError::new("config lock poisoned", protocol::INTERNAL))?;

        for (name, value) in values {
            match config.set(name, value) {
                Ok(coerced) => {
                    if config::is_live(name) {
                        applied.push(Value::from(name.clone()));
                    } else {
                        restart_required.push(Value::from(name.clone()));
                    }
                    changes.insert(name.clone(), coerced);
                }
                Err(reason) => {
                    rejected.insert(name.clone(), Value::from(reason));
                }
            }
        }
    }

    let mut persisted: Vec<Value> = Vec::new();
    if persist && !changes.is_empty() {
        let updates = Config::env_updates(&changes);
        match update_env_file(env_path, &updates, None) {
            Ok(written) => persisted = written.into_iter().map(Value::from).collect(),
            Err(error) => {
                rejected.insert("_persist".into(), Value::from(error.to_string()));
            }
        }
    }

    Ok(json!({
        "applied": applied,
        "persisted": persisted,
        "restart_required": restart_required,
        "rejected": rejected,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture {
        server: Arc<ControlServer>,
        config: Arc<Mutex<Config>>,
        dir: PathBuf,
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.dir).ok();
        }
    }

    fn fixture(tag: &str) -> Fixture {
        let dir = std::env::temp_dir().join(format!(
            "minus-core-{tag}-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();

        let config = Arc::new(Mutex::new(Config::new()));
        let state = Arc::new(State::new());
        let handlers = default_handlers(Arc::clone(&config), state.clone(), dir.join(".env"));
        let server = Arc::new(ControlServer::bind(dir.join("control.sock"), handlers, state));
        Fixture {
            server,
            config,
            dir,
        }
    }

    fn call(server: &ControlServer, command: &str, params: Value) -> Value {
        server.handle(&protocol::request("1", command, params))
    }

    #[test]
    fn hello_and_ping_answer() {
        let fixture = fixture("hello");
        assert_eq!(call(&fixture.server, "ping", json!({}))["result"]["pong"], true);
        let hello = call(&fixture.server, "hello", json!({}));
        assert_eq!(hello["result"]["protocol"], protocol::PROTOCOL_VERSION);
    }

    #[test]
    fn an_unknown_command_is_a_structured_error() {
        let fixture = fixture("unknown");
        let response = call(&fixture.server, "not_a_command", json!({}));
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], protocol::UNKNOWN_COMMAND);
    }

    #[test]
    fn a_missing_command_is_bad_params() {
        let fixture = fixture("no-command");
        let response = fixture
            .server
            .handle(&json!({"v": 1, "type": "request", "id": "9"}));
        assert_eq!(response["error"]["code"], protocol::BAD_PARAMS);
    }

    #[test]
    fn set_config_applies_live_settings_and_persists_them() {
        let fixture = fixture("set");

        let response = call(
            &fixture.server,
            "set_config",
            json!({"values": {"chat_model": "openai/gpt-oss-20b", "max_retries": "4"}}),
        );

        assert_eq!(response["result"]["applied"], json!(["chat_model", "max_retries"]));
        assert_eq!(response["result"]["rejected"], json!({}));
        assert_eq!(
            response["result"]["persisted"],
            json!(["MINUS_CHAT_MODEL", "MINUS_MAX_RETRIES"])
        );

        let text = fs::read_to_string(fixture.dir.join(".env")).unwrap();
        assert!(text.contains("MINUS_CHAT_MODEL=openai/gpt-oss-20b"));
        assert!(text.contains("MINUS_MAX_RETRIES=4"));
        assert_eq!(
            fixture.config.lock().unwrap().get("chat_model").unwrap(),
            "openai/gpt-oss-20b"
        );
    }

    #[test]
    fn a_restart_only_setting_is_persisted_but_not_applied() {
        let fixture = fixture("restart");

        let response = call(
            &fixture.server,
            "set_config",
            json!({"values": {"stt_device": "cpu"}}),
        );

        assert_eq!(response["result"]["applied"], json!([]));
        assert_eq!(response["result"]["restart_required"], json!(["stt_device"]));
        assert_eq!(
            fixture.config.lock().unwrap().get("stt_device").unwrap(),
            "cuda",
            "a restart-only write must not change the running value"
        );
        assert!(fs::read_to_string(fixture.dir.join(".env"))
            .unwrap()
            .contains("MINUS_STT_DEVICE=cpu"));
    }

    #[test]
    fn secrets_and_unknown_settings_are_rejected_not_written() {
        let fixture = fixture("reject");

        let response = call(
            &fixture.server,
            "set_config",
            json!({"values": {"openrouter_api_key": "leak", "nonsense": 1}}),
        );

        assert_eq!(response["result"]["applied"], json!([]));
        assert!(response["result"]["rejected"]["openrouter_api_key"]
            .as_str()
            .unwrap()
            .contains("Secrets"));
        assert!(!fixture.dir.join(".env").exists());
    }

    #[test]
    fn two_servers_cannot_own_one_socket() {
        let fixture = fixture("busy");
        let stop = Arc::new(AtomicBool::new(false));
        let serving = Arc::clone(&stop);
        let server = Arc::clone(&fixture.server);
        let handle = thread::spawn(move || server.serve(&serving));

        wait_for_socket(&fixture.server.path().to_path_buf());

        let state = Arc::new(State::new());
        let second = Arc::new(ControlServer::bind(
            fixture.server.path(),
            HashMap::new(),
            state,
        ));
        let error = second.serve(&AtomicBool::new(false)).expect_err("busy");
        assert_eq!(error.kind(), std::io::ErrorKind::AddrInUse);

        stop.store(true, Ordering::SeqCst);
        handle.join().ok();
    }

    #[test]
    fn the_socket_answers_a_real_client() {
        let fixture = fixture("socket");
        let path = fixture.server.path().to_path_buf();
        let stop = Arc::new(AtomicBool::new(false));
        let serving = Arc::clone(&stop);
        let server = Arc::clone(&fixture.server);
        let handle = thread::spawn(move || server.serve(&serving));

        let mut stream = connect(&path);
        stream
            .write_all(&protocol::encode(&protocol::request("42", "ping", json!({}))))
            .unwrap();

        let mut reader = BufReader::new(stream);
        let mut line = Vec::new();
        reader.read_until(b'\n', &mut line).unwrap();
        let frame = protocol::decode(&line).expect("decodes");
        assert_eq!(frame["id"], "42");
        assert_eq!(frame["result"]["pong"], true);

        stop.store(true, Ordering::SeqCst);
        handle.join().ok();
    }

    fn wait_for_socket(path: &Path) {
        for _ in 0..200 {
            if path.exists() {
                return;
            }
            thread::sleep(Duration::from_millis(10));
        }
        panic!("socket never appeared at {}", path.display());
    }

    fn connect(path: &Path) -> UnixStream {
        wait_for_socket(path);
        for _ in 0..200 {
            if let Ok(stream) = UnixStream::connect(path) {
                return stream;
            }
            thread::sleep(Duration::from_millis(10));
        }
        panic!("could not connect to {}", path.display());
    }
}
