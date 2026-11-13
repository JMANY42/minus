//! A control-socket client, for `minusctl` and for anything else that wants to
//! drive a running assistant without reimplementing the frame handling.
//!
//! Same framing as `control/protocol.py`: one JSON object per line, requests
//! correlated by `id`. Events are buffered rather than dropped, because a
//! subscription and a request share one connection.

use std::collections::VecDeque;
use std::fmt;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use serde_json::{json, Value};

use crate::protocol;

const READ_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug)]
pub enum ClientError {
    /// Nothing is listening on the socket.
    Refused(String),
    /// The connection failed or died mid-request.
    Io(String),
    /// The peer's frame did not speak the protocol.
    Protocol(String),
    /// The assistant answered with a structured error.
    Remote { code: String, message: String },
    /// The read timed out (only meaningful for event polling).
    Timeout,
}

impl fmt::Display for ClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(message) | Self::Io(message) | Self::Protocol(message) => {
                f.write_str(message)
            }
            Self::Remote { code, message } => write!(f, "{code}: {message}"),
            Self::Timeout => f.write_str("timed out"),
        }
    }
}

impl std::error::Error for ClientError {}

impl From<std::io::Error> for ClientError {
    fn from(error: std::io::Error) -> Self {
        match error.kind() {
            std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => Self::Timeout,
            _ => Self::Io(error.to_string()),
        }
    }
}

/// The socket a running assistant listens on: `MINUS_CONTROL_SOCKET`, else the
/// same `$XDG_RUNTIME_DIR/minus/control.sock` the Python side uses.
pub fn default_socket_path() -> std::path::PathBuf {
    if let Ok(value) = std::env::var("MINUS_CONTROL_SOCKET") {
        if !value.trim().is_empty() {
            return std::path::PathBuf::from(value);
        }
    }
    match std::env::var("XDG_RUNTIME_DIR") {
        Ok(runtime) if !runtime.trim().is_empty() => {
            std::path::PathBuf::from(runtime).join("minus").join("control.sock")
        }
        _ => std::path::PathBuf::from("/tmp").join("minus").join("control.sock"),
    }
}

pub struct ControlClient {
    stream: UnixStream,
    reader: BufReader<UnixStream>,
    next_id: u64,
    pending_events: VecDeque<Value>,
}

impl ControlClient {
    /// Connect, or say plainly that nothing is running.
    pub fn connect(path: &Path) -> Result<Self, ClientError> {
        let stream = UnixStream::connect(path).map_err(|error| match error.kind() {
            std::io::ErrorKind::NotFound
            | std::io::ErrorKind::ConnectionRefused
            | std::io::ErrorKind::PermissionDenied => ClientError::Refused(format!(
                "MINUS is not running (no usable socket at {})",
                path.display()
            )),
            _ => ClientError::Io(format!("cannot reach {}: {error}", path.display())),
        })?;
        stream.set_read_timeout(Some(READ_TIMEOUT))?;
        let reader = BufReader::new(stream.try_clone()?);
        Ok(Self {
            stream,
            reader,
            next_id: 0,
            pending_events: VecDeque::new(),
        })
    }

    pub fn request(&mut self, command: &str, params: Value) -> Result<Value, ClientError> {
        self.next_id += 1;
        let id = self.next_id.to_string();
        self.stream
            .write_all(&protocol::encode(&protocol::request(&id, command, params)))?;

        loop {
            let payload = self.read_frame()?;
            if payload.get("type").and_then(Value::as_str) == Some(protocol::EVENT) {
                self.pending_events.push_back(payload);
                continue;
            }
            if payload.get("id").and_then(Value::as_str) != Some(id.as_str()) {
                continue;
            }
            if payload.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                return Ok(payload.get("result").cloned().unwrap_or(Value::Null));
            }
            let error = payload.get("error").cloned().unwrap_or(Value::Null);
            return Err(ClientError::Remote {
                code: error
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown")
                    .to_string(),
                message: error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            });
        }
    }

    pub fn subscribe(&mut self) -> Result<(), ClientError> {
        self.request("subscribe", json!({}))?;
        Ok(())
    }

    /// The next event, or `None` if the read timed out (a quiet assistant).
    pub fn next_event(&mut self) -> Result<Option<Value>, ClientError> {
        if let Some(event) = self.pending_events.pop_front() {
            return Ok(Some(event));
        }
        match self.read_frame() {
            Ok(payload) if payload.get("type").and_then(Value::as_str) == Some(protocol::EVENT) => {
                Ok(Some(payload))
            }
            Ok(_) => Ok(None),
            Err(ClientError::Timeout) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn read_frame(&mut self) -> Result<Value, ClientError> {
        let mut line = Vec::new();
        self.reader.read_until(b'\n', &mut line)?;
        if line.iter().all(|byte| byte.is_ascii_whitespace()) {
            return Err(ClientError::Io("MINUS closed the connection".to_string()));
        }
        protocol::decode(&line).map_err(|error| ClientError::Protocol(error.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::control::{default_handlers, ControlServer, State};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};
    use std::thread;

    /// A real socket, our own real server, no stand-ins: the frame handling on
    /// both sides is the thing under test.
    #[test]
    fn a_client_can_drive_the_control_server() {
        let dir = std::env::temp_dir().join(format!(
            "minus-core-client-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|elapsed| elapsed.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("control.sock");

        let state = Arc::new(State::new());
        let config = Arc::new(Mutex::new(Config::new()));
        let handlers = default_handlers(Arc::clone(&config), Arc::clone(&state), dir.join(".env"));
        let server = Arc::new(ControlServer::bind(path.clone(), handlers, state));
        let stop = Arc::new(AtomicBool::new(false));
        let serving = Arc::clone(&server);
        let stopper = Arc::clone(&stop);
        let handle = thread::spawn(move || serving.serve(&stopper));

        let mut client = None;
        for _ in 0..200 {
            match ControlClient::connect(&path) {
                Ok(candidate) => {
                    client = Some(candidate);
                    break;
                }
                Err(_) => thread::sleep(Duration::from_millis(10)),
            }
        }
        let mut client = client.expect("socket answers");

        assert_eq!(client.request("ping", json!({})).unwrap()["pong"], true);
        let config = client.request("get_config", json!({})).unwrap();
        assert_eq!(config["values"]["chat_model"], "openai/gpt-oss-20b:nitro");

        let error = client
            .request("not_a_command", json!({}))
            .expect_err("unknown command");
        match error {
            ClientError::Remote { code, .. } => assert_eq!(code, "unknown_command"),
            other => panic!("unexpected {other:?}"),
        }

        stop.store(true, Ordering::SeqCst);
        handle.join().ok();
        std::fs::remove_dir_all(&dir).ok();
    }
}
