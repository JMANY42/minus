# MINUS

Personal AI Assistant + Home Integration

## What is Minus?

**MINUS** is a personal AI assistant. It is (for now) designed for my personal use. The core gimmick I'm aiming for is to make it like Jarvis from Iron Man. I want to be able to talk outloud and then have minus intelligently take action to assist me. If my hardware allows, I want it to run locally on my server.

## Current Status

This project is a work in progress and currently focused on tool calls.

Currently, Minus is essentially just a custom harness around a Groq model for fast responses. A more robust archeticture to be more generally useful is in progress.

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