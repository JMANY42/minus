//! The OpenRouter HTTP client, ported from `src/minus/llm/`.
//!
//! Blocking `reqwest` because the loop is synchronous; the retry rule is the
//! Python one — only a tool-call generation failure is retried, only when a
//! `retry_note` is supplied, and only for the substrings providers actually
//! use. A response that arrives HTTP 200 with an `error` on its choice is a
//! failure and is rejected here, because the SDK-equivalent never raises for it.

use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};

use crate::chat::{ChatModel, Completion, CompletionRequest, LlmError, Message, ToolCall};
use crate::config::Config;

pub const RETRYABLE_ERROR_MARKERS: &[&str] = &[
    "failed_generation",
    "tool_use_failed",
    "tool call arguments do not satisfy the declared schema",
];

fn non_empty(value: String) -> Option<String> {
    if value.trim().is_empty() {
        None
    } else {
        Some(value)
    }
}

pub fn is_retryable(error: &LlmError) -> bool {
    let text = error.to_string();
    RETRYABLE_ERROR_MARKERS
        .iter()
        .any(|marker| text.contains(marker))
}

pub struct OpenRouterClient {
    client: reqwest::blocking::Client,
    base_url: String,
    api_key: String,
    chat_model: String,
    max_retries: usize,
}

impl OpenRouterClient {
    pub fn from_config(config: &Config) -> Result<Self, LlmError> {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|error| LlmError::Transport(format!("cannot build HTTP client: {error}")))?;

        let base_url = non_empty(config.text("llm_base_url"))
            .unwrap_or_else(|| config.text("openrouter_base_url"));
        let api_key = non_empty(config.text("llm_api_key"))
            .unwrap_or_else(|| config.text("openrouter_api_key"));

        Ok(Self {
            client,
            base_url,
            api_key,
            chat_model: config.text("chat_model"),
            max_retries: config.integer("max_retries").max(1) as usize,
        })
    }

    pub fn build_payload(&self, request: &CompletionRequest) -> Value {
        let model = request
            .model
            .clone()
            .unwrap_or_else(|| self.chat_model.clone());

        let mut messages: Vec<Value> = Vec::new();
        if let Some(system_prompt) = &request.system_prompt {
            messages.push(json!({"role": "system", "content": system_prompt}));
        }
        for message in &request.messages {
            messages.push(serde_json::to_value(message).unwrap_or_else(|_| json!({})));
        }

        let mut payload = json!({
            "model": model,
            "messages": messages,
        });

        let object = payload.as_object_mut().expect("payload is an object");
        if let Some(tools) = &request.tools {
            if !tools.is_empty() {
                object.insert("tools".into(), Value::Array(tools.clone()));
                object.insert("tool_choice".into(), Value::from(request.tool_choice.clone()));
            }
        }
        if let Some(effort) = &request.reasoning_effort {
            object.insert("reasoning_effort".into(), Value::from(effort.clone()));
        }
        payload
    }

    fn post(&self, payload: &Value) -> Result<Value, LlmError> {
        let response = self
            .client
            .post(format!("{}/chat/completions", self.base_url.trim_end_matches('/')))
            .bearer_auth(&self.api_key)
            .json(payload)
            .send()
            .map_err(|error| LlmError::Transport(format!("request failed: {error}")))?;

        let status = response.status();
        let body: Value = response
            .json()
            .map_err(|error| LlmError::Http(format!("response was not JSON: {error}")))?;

        if !status.is_success() {
            return Err(LlmError::Http(format!("HTTP {status}: {body}")));
        }
        Ok(body)
    }
}

/// Everything a completion must satisfy to be usable, mirroring
/// `src/minus/llm/retry.py::validate_completion`.
pub fn validate_completion(body: &Value) -> Result<Completion, LlmError> {
    let choices = body
        .get("choices")
        .and_then(Value::as_array)
        .filter(|choices| !choices.is_empty())
        .ok_or_else(|| LlmError::Malformed("LLM returned an empty response.".into()))?;

    let choice = &choices[0];
    if let Some(error) = choice.get("error").filter(|error| !error.is_null()) {
        return Err(LlmError::Malformed(format!(
            "LLM returned an error choice: {error}"
        )));
    }

    let message = choice
        .get("message")
        .ok_or_else(|| LlmError::Malformed("LLM returned a response without a message.".into()))?;

    let content = message
        .get("content")
        .and_then(Value::as_str)
        .map(str::to_string);
    let tool_calls: Vec<ToolCall> = match message.get("tool_calls") {
        None | Some(Value::Null) => Vec::new(),
        Some(value) => serde_json::from_value(value.clone()).map_err(|error| {
            LlmError::Malformed(format!("LLM returned malformed tool calls: {error}"))
        })?,
    };

    let has_content = content.as_deref().unwrap_or("").trim().len() > 0;
    if !has_content && tool_calls.is_empty() {
        return Err(LlmError::Malformed(
            "LLM returned an invalid response with no content or tool calls.".into(),
        ));
    }

    Ok(Completion {
        message: Message {
            role: "assistant".into(),
            content,
            tool_calls,
            tool_call_id: None,
        },
    })
}

fn retry_message(note: &str, error: &LlmError, tools: Option<&[Value]>, attempt: usize) -> Message {
    let mut parts = vec![note.to_string(), format!("The previous attempt failed with: {error}")];
    if let Some(tools) = tools {
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|tool| tool.get("function")?.get("name")?.as_str())
            .collect();
        if !names.is_empty() {
            parts.push(format!(
                "The only tools that exist are: {}. No other tool is available.",
                names.join(", ")
            ));
        }
    }
    if attempt >= 2 {
        parts.push(
            "You have now failed more than once. If you cannot produce a valid tool call, \
             answer the user in plain text instead of calling a tool."
                .to_string(),
        );
    }
    Message::system(parts.join("\n\n"))
}

impl ChatModel for OpenRouterClient {
    fn complete(&self, request: &CompletionRequest) -> Result<Completion, LlmError> {
        let mut attempt_request = request.clone();
        let mut last_error: Option<LlmError> = None;

        for attempt in 1..=self.max_retries {
            let payload = self.build_payload(&attempt_request);

            match self.post(&payload).and_then(|body| validate_completion(&body)) {
                Ok(completion) => return Ok(completion),
                Err(error) => {
                    let retryable = request.retry_note.is_some() && is_retryable(&error);
                    last_error = Some(error.clone());
                    if !retryable || attempt == self.max_retries {
                        return Err(error);
                    }
                    let note = request.retry_note.clone().unwrap_or_default();
                    attempt_request
                        .messages
                        .push(retry_message(&note, &error, request.tools.as_deref(), attempt));
                }
            }
        }

        Err(last_error.unwrap_or_else(|| LlmError::Http("no attempt was made".into())))
    }
}

pub fn client_from_config(config: &Config) -> Result<Arc<OpenRouterClient>, LlmError> {
    Ok(Arc::new(OpenRouterClient::from_config(config)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Config {
        Config::new()
    }

    fn request() -> CompletionRequest {
        CompletionRequest {
            messages: vec![Message::user("hello")],
            model: None,
            system_prompt: Some("be brief".into()),
            tools: None,
            tool_choice: "auto".into(),
            reasoning_effort: None,
            retry_note: None,
        }
    }

    #[test]
    fn payload_prepends_the_system_prompt_and_defaults_the_model() {
        let client = OpenRouterClient::from_config(&config()).unwrap();
        let payload = client.build_payload(&request());

        assert_eq!(payload["model"], "openai/gpt-oss-20b:nitro");
        assert_eq!(payload["messages"][0]["role"], "system");
        assert_eq!(payload["messages"][0]["content"], "be brief");
        assert_eq!(payload["messages"][1]["role"], "user");
        assert!(payload.get("tools").is_none());
        assert!(payload.get("reasoning_effort").is_none());
    }

    #[test]
    fn a_request_model_and_effort_are_forwarded() {
        let client = OpenRouterClient::from_config(&config()).unwrap();
        let mut request = request();
        request.model = Some("other/model".into());
        request.reasoning_effort = Some("high".into());

        let payload = client.build_payload(&request);

        assert_eq!(payload["model"], "other/model");
        assert_eq!(payload["reasoning_effort"], "high");
    }

    #[test]
    fn empty_tools_are_omitted_but_real_ones_are_sent() {
        let client = OpenRouterClient::from_config(&config()).unwrap();
        let mut request = request();
        request.tools = Some(vec![]);
        assert!(client.build_payload(&request).get("tools").is_none());

        request.tools = Some(vec![json!({"type": "function", "function": {"name": "x"}})]);
        let payload = client.build_payload(&request);
        assert_eq!(payload["tools"][0]["function"]["name"], "x");
        assert_eq!(payload["tool_choice"], "auto");
    }

    #[test]
    fn an_in_band_error_choice_is_a_failure() {
        let body = json!({"choices": [{"error": {"message": "upstream died"}, "message": null}]});
        assert!(validate_completion(&body).is_err());
    }

    #[test]
    fn an_empty_choice_list_is_a_failure() {
        assert!(validate_completion(&json!({"choices": []})).is_err());
    }

    #[test]
    fn reasoning_without_content_or_tools_is_a_failure() {
        let body = json!({"choices": [{"message": {"role": "assistant", "content": "   "}}]});
        assert!(validate_completion(&body).is_err());
    }

    #[test]
    fn a_tool_call_only_response_is_valid() {
        let body = json!({"choices": [{"message": {"role": "assistant", "content": null,
            "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "echo_text", "arguments": "{\"text\":\"hi\"}"}}]}}]});
        let completion = validate_completion(&body).unwrap();
        assert_eq!(completion.message.tool_calls[0].function.name, "echo_text");
        assert!(completion.message.content.is_none());
    }

    #[test]
    fn retryable_markers_match_the_python_list() {
        assert!(is_retryable(&LlmError::Http(
            "error body: failed_generation".into()
        )));
        assert!(is_retryable(&LlmError::Malformed(
            "tool_use_failed".into()
        )));
        assert!(!is_retryable(&LlmError::Transport("connection reset".into())));
    }
}
