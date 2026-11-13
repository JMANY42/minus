//! `minusctl`: a small, fast CLI around a running MINUS.
//!
//! It speaks the same NDJSON control protocol as the dashboard and the Python
//! CLI, so it works against any engine that owns the socket. No HTTP, no
//! plugin system, no configuration of its own — the socket and the protocol are
//! the whole interface.
//!
//!   minusctl status [--watch]      what it is doing right now
//!   minusctl say <text...>         speak as though the user had
//!   minusctl tools [--json]        each agent and its tools
//!   minusctl facts [--json]        what it remembers
//!   minusctl config [name]         live settings
//!   minusctl model [name] [--deep name]
//!   minusctl                       interactive prompt (the default)
//!
//! Colour is disabled by `NO_COLOR`, and nothing here writes files.

use std::io::{self, BufRead, Write};
use std::path::PathBuf;

use serde_json::{json, Value};

use minus_core::client::{default_socket_path, ClientError, ControlClient};

const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const CYAN: &str = "\x1b[36m";
const GREEN: &str = "\x1b[32m";
const YELLOW: &str = "\x1b[33m";
const RED: &str = "\x1b[31m";

const HELP: &str = "\
/status              what it is doing right now
/tools               each agent and its tools
/facts               what it remembers
/model [name]        show or switch the conversational model
/interrupt           stop the current reply
/help                this list
/quit                leave (alias /exit)
anything else        sent to the assistant as a spoken line";

fn paint(code: &str, text: &str) -> String {
    if std::env::var_os("NO_COLOR").is_some() {
        text.to_string()
    } else {
        format!("{code}{text}{RESET}")
    }
}

fn phase_colour(phase: &str) -> &'static str {
    match phase {
        "listening" => GREEN,
        "thinking" => YELLOW,
        "hearing" | "speaking" => CYAN,
        _ => DIM,
    }
}

fn print_status(snapshot: &Value) {
    let phase = snapshot["phase"].as_str().unwrap_or("?");
    let mut line = paint(phase_colour(phase), &format!("{phase:<9}"));

    if let Some(conversation) = snapshot.get("conversation") {
        if let Some(id) = conversation.get("id").and_then(Value::as_str) {
            let messages = conversation
                .get("message_count")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            line.push_str(&paint(DIM, &format!("  conversation {id} ({messages} msgs)")));
        }
    }
    let uptime = snapshot["uptime_seconds"].as_f64().unwrap_or(0.0);
    line.push_str(&paint(DIM, &format!("  up {uptime:.0}s")));
    println!("{line}");

    if let Some(deep) = snapshot.get("deep") {
        if deep["in_flight"].as_bool().unwrap_or(false) {
            let elapsed = deep["elapsed_seconds"].as_f64().unwrap_or(0.0);
            let question = deep["question"].as_str().unwrap_or("");
            println!(
                "  {} {:.0}s — {}",
                paint(YELLOW, "deep"),
                elapsed,
                truncate(question, 70)
            );
        }
    }
}

fn truncate(text: &str, width: usize) -> String {
    let flattened = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flattened.chars().count() <= width {
        return flattened;
    }
    let cut: String = flattened.chars().take(width.saturating_sub(1)).collect();
    format!("{cut}…")
}

fn print_tools(result: &Value, as_json: bool) {
    if as_json {
        println!("{}", serde_json::to_string_pretty(result).unwrap_or_default());
        return;
    }
    for agent in result.as_array().cloned().unwrap_or_default() {
        let title = agent["title"].as_str().unwrap_or("?");
        let note = agent["note"].as_str().unwrap_or("");
        println!("{}", paint(BOLD, title));

        let tools = agent["tools"].as_array().cloned().unwrap_or_default();
        if tools.is_empty() {
            println!("  {}", paint(DIM, if note.is_empty() { "(none)" } else { note }));
            continue;
        }
        for tool in tools {
            let name = tool["name"].as_str().unwrap_or("?");
            let enabled = tool["enabled"].as_bool().unwrap_or(true);
            let marker = if enabled {
                paint(GREEN, "on ")
            } else {
                paint(RED, "off")
            };
            println!("  {marker} {name}");
        }
    }
}

fn print_facts(result: &Value, as_json: bool) {
    if as_json {
        println!("{}", serde_json::to_string_pretty(result).unwrap_or_default());
        return;
    }
    let facts = result.as_array().cloned().unwrap_or_default();
    if facts.is_empty() {
        println!("{}", paint(DIM, "no facts stored"));
        return;
    }
    let width = facts
        .iter()
        .filter_map(|fact| fact["attribute"].as_str())
        .map(|attribute| attribute.chars().count())
        .max()
        .unwrap_or(0);
    for fact in facts {
        let attribute = fact["attribute"].as_str().unwrap_or("?");
        let value = fact["value"].as_str().unwrap_or("?");
        println!("  {attribute:<width$}  {value}");
    }
}

fn print_config(result: &Value, name: Option<&str>) {
    let values = &result["values"];
    if let Some(name) = name {
        match values.get(name) {
            Some(value) => println!("{name} = {}", scalar(value)),
            None => println!("{}", paint(RED, &format!("no such setting: {name}"))),
        }
        return;
    }
    for key in [
        "chat_model",
        "deep_model",
        "fact_extraction_model",
        "max_tool_rounds",
        "relevance_threshold",
        "llm_base_url",
        "openrouter_base_url",
    ] {
        if let Some(value) = values.get(key) {
            println!("{key:<22} {}", scalar(value));
        }
    }
}

fn scalar(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn connect(path: &PathBuf) -> ControlClient {
    match ControlClient::connect(path) {
        Ok(client) => client,
        Err(ClientError::Refused(message)) => {
            eprintln!("{message}");
            std::process::exit(1);
        }
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}

fn request(client: &mut ControlClient, command: &str, params: Value) -> Result<Value, ClientError> {
    client.request(command, params)
}

fn status_once(client: &mut ControlClient) -> Result<(), ClientError> {
    let snapshot = request(client, "get_status", json!({}))?;
    print_status(&snapshot);
    Ok(())
}

fn status_watch(client: &mut ControlClient) -> Result<(), ClientError> {
    status_once(client)?;
    client.subscribe()?;
    loop {
        if let Some(event) = client.next_event()? {
            if event["event"].as_str() == Some("status") {
                let data = event["data"].clone();
                print_status(&data);
            }
        }
    }
}

fn set_model(
    client: &mut ControlClient,
    conversational: Option<&str>,
    deep: Option<&str>,
) -> Result<(), ClientError> {
    let mut values = serde_json::Map::new();
    if let Some(name) = conversational {
        values.insert("chat_model".into(), Value::from(name));
    }
    if let Some(name) = deep {
        values.insert("deep_model".into(), Value::from(name));
    }
    if values.is_empty() {
        let config = request(client, "get_config", json!({}))?;
        let config = &config["values"];
        for key in ["chat_model", "deep_model", "fact_extraction_model"] {
            if let Some(value) = config.get(key) {
                println!("{key:<22} {}", scalar(value));
            }
        }
        return Ok(());
    }

    let result = request(client, "set_config", json!({ "values": values }))?;
    for name in result["applied"].as_array().cloned().unwrap_or_default() {
        println!("{} {}", paint(GREEN, "applied"), scalar(&name));
    }
    for name in result["restart_required"].as_array().cloned().unwrap_or_default() {
        println!("{} {}", paint(YELLOW, "pending"), scalar(&name));
    }
    for name in result["persisted"].as_array().cloned().unwrap_or_default() {
        println!("{} {}", paint(DIM, "persisted"), scalar(&name));
    }
    if let Some(rejected) = result["rejected"].as_object() {
        for (name, reason) in rejected {
            println!("{} {name}: {}", paint(RED, "rejected"), scalar(reason));
        }
    }
    Ok(())
}

fn repl(client: &mut ControlClient) -> Result<(), ClientError> {
    println!(
        "{} {}",
        paint(BOLD, "minusctl"),
        paint(DIM, "type /help for commands, /quit to leave")
    );
    status_once(client)?;

    let stdin = io::stdin();
    loop {
        print!("{}", paint(CYAN, "> "));
        let _ = io::stdout().flush();

        let mut line = String::new();
        if stdin.lock().read_line(&mut line).unwrap_or(0) == 0 {
            println!();
            return Ok(());
        }
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        match line {
            "/quit" | "/exit" | ":q" => return Ok(()),
            "/help" => {
                println!("{HELP}");
                continue;
            }
            "/status" => status_once(client)?,
            "/interrupt" => {
                let result = request(client, "interrupt", json!({}))?;
                println!("{} {}", paint(DIM, "interrupted; generation"), result["generation"]);
            }
            _ if line.starts_with("/tools") => {
                let result = request(client, "list_tools", json!({}))?;
                print_tools(&result, false);
            }
            _ if line.starts_with("/facts") => {
                let result = request(client, "list_facts", json!({ "limit": 20 }))?;
                print_facts(&result, false);
            }
            _ if line.starts_with("/model") => {
                let name = line["/model".len()..].trim();
                let name = if name.is_empty() { None } else { Some(name) };
                set_model(client, name, None)?;
                if name.is_some() {
                    status_once(client)?;
                }
            }
            _ if line.starts_with('/') => {
                println!("{}", paint(RED, "unknown command; /help for the list"));
            }
            _ => {
                request(client, "say", json!({ "text": line }))?;
                status_once(client)?;
            }
        }
    }
}

fn argument_after(args: &[String], name: &str) -> Option<String> {
    let index = args.iter().position(|argument| argument == name)?;
    args.get(index + 1).cloned()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut socket = default_socket_path();

    if let Some(path) = argument_after(&args, "--socket") {
        socket = PathBuf::from(path);
    }

    // Everything that is not `--socket <path>` is positional or a known flag.
    let mut positionals: Vec<String> = Vec::new();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--socket" => index += 2,
            "--json" | "--watch" | "--deep" => {
                if args[index] == "--deep" {
                    index += 1;
                }
                index += 1;
            }
            other => {
                positionals.push(other.to_string());
                index += 1;
            }
        }
    }

    let command = positionals.first().map(String::as_str).unwrap_or("repl");

    // Answer without a socket: asking for help must not require a running
    // assistant, and neither must a typo.
    if matches!(command, "help" | "--help" | "-h") {
        println!(
            "minusctl [--socket PATH] [command]\n\n\
             commands: status [--watch], say <text...>, tools [--json], facts [--json], \
             config [name], model [name] [--deep name], repl (default)"
        );
        return;
    }
    if !matches!(
        command,
        "status" | "say" | "tools" | "facts" | "config" | "model" | "repl"
    ) {
        eprintln!("unknown command {command:?}; try `minusctl help`");
        std::process::exit(2);
    }

    let mut client = connect(&socket);

    let outcome = match command {
        "status" => {
            if args.iter().any(|argument| argument == "--watch") {
                status_watch(&mut client)
            } else {
                status_once(&mut client)
            }
        }
        "say" => {
            let text = positionals[1..].join(" ");
            if text.trim().is_empty() {
                eprintln!("usage: minusctl say <text...>");
                std::process::exit(2);
            }
            request(&mut client, "say", json!({ "text": text })).map(|_| ())
        }
        "tools" => request(&mut client, "list_tools", json!({}))
            .map(|result| print_tools(&result, args.iter().any(|argument| argument == "--json"))),
        "facts" => request(&mut client, "list_facts", json!({ "limit": 20 }))
            .map(|result| print_facts(&result, args.iter().any(|argument| argument == "--json"))),
        "config" => request(&mut client, "get_config", json!({}))
            .map(|result| print_config(&result, positionals.get(1).map(String::as_str))),
        "model" => set_model(
            &mut client,
            positionals.get(1).map(String::as_str),
            argument_after(&args, "--deep").as_deref(),
        ),
        "repl" => repl(&mut client),
        _ => unreachable!("validated above"),
    };

    if let Err(error) = outcome {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
