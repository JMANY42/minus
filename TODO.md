## New Features
- Always online
    - [x] Runs as a systemd --user service (`minus serve`)
    - Always listening
    - When idle, perform background tasks (execute plan)
- [x] Dashboard (`minus dash`)
    - [x] Show current status
    - [x] Show logs and any info that is helpful
    - [x] btop-style, 16 colours and ASCII borders for the attached VT
    - Fill in the expanded panels -- each `options()` returns [] today
- Conversation splitting
    - [x] Start seperate conversations after pauses (30s idle, configurable)
    - Allow to jump back into previous conversations
- Persistant memory
    - Save all conversations
    - Save extra details that might be relevent
    - Searchable memory through tool call
- Plan out tasks
    - Planning mode
    - Save plan, when idle execute plan
- Idle tasks:
    - Summarize memory to make future recall faster
- Multi Modal
    - Route different tasks to different quality of LLM
    - Start with simple LLM to classify, then select model to do tasks if needed and keep the conversation going.
        - Current groq model shouuld not keep doing any thinking, it kinda sucks. It's only good for conversations bc its fast.

## Quality of Life
- Generate first chunk of speech in cloud to make playback faster. VPS or dedicated TTS model (or Mark's GPU in a couple months)
- Test external mics

## Bugs
- None yet (hopefully)
