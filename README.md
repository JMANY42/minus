# MINUS

Personal AI Assistant + Home Integration

## What is Minus?

**MINUS** is a personal AI assistant. It is (for now) designed for my personal use. The core gimmick I'm aiming for is to make it like Jarvis from Iron Man. I want to be able to talk outloud and then have minus intelligently take action to assist me. If my hardware allows, I want it to run locally on my server.

## Current Status

This project is a work in progress and currently focused on giving Minus a persistent memory.

Currently, Minus is essentially just a custom harness around a Groq model for fast responses. A more robust archeticture to be more generally useful is in progress.

## What's New?

I just gave Minus semantic memory. Minus now can remember facts and preferences between conversation sessions. It works as follows.

- Every user message is appended with potentially relevent facts ranked by comparing the embedding of the user message vs the fact
- Every time a conversation ends and is condensed, Groq extracts facts from the transcript. 
- Duplicate facts (facts with the same attribute) are not allowed. Multi-valued facts are allowed.
- Facts can be superceded by new facts.

Improvements:
- Similar attributes (e.g. preferred_language vs programming_language) are treated as seperate facts
- Sometimes Groq makes a ton of unnecessary tool calls. Tweak system prompt or look into better LLMs (OpenRouter?)
- Seperate script to look at all current facts and manually go through and modify/delete them. More of a QOL feature. Could be incorporated into future dashboard.


## Run

- Microphone mode: `python3 src/main.py`
- No-mic mode: `python3 src/main.py --no-mic`


## Features

### Home Integration (hardware required)

- [ ] Play music
- [ ] Control lights
- [ ] Build a dedicated MINUS dashboard screen

### General Assistance

- [ ] Create calendar events and tasks
- [ ] Set reminders
- [ ] Set alarms

### Project Assistance

- [ ] Spawn agents
- [ ] Talk through problems

## Design Guidelines

- Be funny 
- Be helpful 
- Call out bad ideas 
- Avoid unnecessary refusals 
- Prioritize fast responses over in depth analysis for conversations. 