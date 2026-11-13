//! The NDJSON control protocol, mirrored from `src/minus/control/protocol.py`.
//!
//! Frame format is one JSON object per line. `serde_json` never emits a literal
//! newline inside a string, so a raw newline is an unambiguous terminator and
//! framing needs neither a length prefix nor an escaping pass -- the same
//! argument the Python module makes.
//!
//! The error codes are a closed set on purpose: a peer branches on the code,
//! never on the message text.

use std::fmt;

use serde_json::{Map, Value};

pub const PROTOCOL_VERSION: i64 = 1;

/// Frames longer than this are refused rather than buffered: nothing legitimate
/// comes close, and reading one to its end would let a peer choose our memory
/// use.
pub const MAX_LINE_BYTES: usize = 1 << 20;

pub const REQUEST: &str = "request";
pub const RESPONSE: &str = "response";
pub const EVENT: &str = "event";

pub const UNSUPPORTED_VERSION: &str = "unsupported_version";
pub const MALFORMED: &str = "malformed";
pub const OVERSIZED: &str = "oversized";
pub const UNKNOWN_COMMAND: &str = "unknown_command";
pub const BAD_PARAMS: &str = "bad_params";
pub const INTERNAL: &str = "internal";

#[derive(Debug, Clone)]
pub struct ProtocolError {
    pub message: String,
    pub code: &'static str,
}

impl ProtocolError {
    pub fn new(message: impl Into<String>, code: &'static str) -> Self {
        Self {
            message: message.into(),
            code,
        }
    }

    pub fn malformed(message: impl Into<String>) -> Self {
        Self::new(message, MALFORMED)
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ProtocolError {}

/// One frame, ready to write: compact JSON plus the terminating newline.
pub fn encode(payload: &Value) -> Vec<u8> {
    let mut line = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
    line.push(b'\n');
    line
}

/// Parse one frame, enforcing the size cap and the version.
pub fn decode(line: &[u8]) -> Result<Value, ProtocolError> {
    if line.len() > MAX_LINE_BYTES {
        return Err(ProtocolError::new(
            format!("Frame exceeds {MAX_LINE_BYTES} bytes"),
            OVERSIZED,
        ));
    }

    let text = std::str::from_utf8(line)
        .map_err(|exc| ProtocolError::malformed(format!("Frame is not UTF-8: {exc}")))?;

    let payload: Value = serde_json::from_str(text)
        .map_err(|exc| ProtocolError::malformed(format!("Frame is not JSON: {exc}")))?;

    if !payload.is_object() {
        return Err(ProtocolError::malformed("Frame is not a JSON object"));
    }

    let version = payload.get("v");
    if version != Some(&Value::from(PROTOCOL_VERSION)) {
        return Err(ProtocolError::new(
            format!(
                "Unsupported protocol version {}; this build speaks {PROTOCOL_VERSION}",
                version.map(|value| value.to_string()).unwrap_or_else(|| "None".into())
            ),
            UNSUPPORTED_VERSION,
        ));
    }

    Ok(payload)
}

fn envelope(kind: &str) -> Map<String, Value> {
    let mut map = Map::new();
    map.insert("v".into(), Value::from(PROTOCOL_VERSION));
    map.insert("type".into(), Value::from(kind));
    map
}

pub fn request(request_id: &str, command: &str, params: Value) -> Value {
    let mut map = envelope(REQUEST);
    map.insert("id".into(), Value::from(request_id));
    map.insert("command".into(), Value::from(command));
    map.insert("params".into(), params);
    Value::Object(map)
}

pub fn response(request_id: Option<&str>, result: Value) -> Value {
    let mut map = envelope(RESPONSE);
    map.insert(
        "id".into(),
        request_id.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert("ok".into(), Value::Bool(true));
    map.insert("result".into(), result);
    Value::Object(map)
}

pub fn error(request_id: Option<&str>, code: &str, message: impl Into<String>) -> Value {
    let mut failure = Map::new();
    failure.insert("code".into(), Value::from(code));
    failure.insert("message".into(), Value::from(message.into()));

    let mut map = envelope(RESPONSE);
    map.insert(
        "id".into(),
        request_id.map(Value::from).unwrap_or(Value::Null),
    );
    map.insert("ok".into(), Value::Bool(false));
    map.insert("error".into(), Value::Object(failure));
    Value::Object(map)
}

pub fn event(name: &str, data: Value, seq: u64) -> Value {
    let mut map = envelope(EVENT);
    map.insert("event".into(), Value::from(name));
    map.insert("seq".into(), Value::from(seq));
    map.insert("data".into(), data);
    Value::Object(map)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn encode_is_one_line_with_a_terminator() {
        let frame = request("1", "ping", json!({}));
        let bytes = encode(&frame);
        assert_eq!(bytes.last(), Some(&b'\n'));
        assert_eq!(bytes.iter().filter(|byte| **byte == b'\n').count(), 1);
    }

    #[test]
    fn a_newline_inside_a_string_survives_a_round_trip() {
        let frame = response(Some("7"), json!({"text": "line one\nline two"}));
        let decoded = decode(&encode(&frame)).expect("decodes");
        assert_eq!(decoded["result"]["text"], "line one\nline two");
    }

    #[test]
    fn multibyte_text_is_measured_in_bytes_not_chars() {
        // 4 bytes per character, well under the cap: the cap must not trip on
        // the character count.
        let big = "é".repeat(1000);
        let frame = response(None, json!({"text": big}));
        assert!(decode(&encode(&frame)).is_ok());
    }

    #[test]
    fn oversized_frames_are_refused() {
        let line = vec![b'a'; MAX_LINE_BYTES + 1];
        let error = decode(&line).expect_err("refused");
        assert_eq!(error.code, OVERSIZED);
    }

    #[test]
    fn version_is_enforced_here_rather_than_at_each_call_site() {
        let error = decode(br#"{"v":2,"type":"request","command":"ping"}"#).expect_err("refused");
        assert_eq!(error.code, UNSUPPORTED_VERSION);
    }

    #[test]
    fn a_missing_version_is_refused() {
        let error = decode(br#"{"type":"request","command":"ping"}"#).expect_err("refused");
        assert_eq!(error.code, UNSUPPORTED_VERSION);
    }

    #[test]
    fn a_non_object_frame_is_malformed() {
        let error = decode(b"[1,2,3]").expect_err("refused");
        assert_eq!(error.code, MALFORMED);
    }

    #[test]
    fn a_non_utf8_frame_is_malformed() {
        let error = decode(&[0xff, 0xfe]).expect_err("refused");
        assert_eq!(error.code, MALFORMED);
    }
}
