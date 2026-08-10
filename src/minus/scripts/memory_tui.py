"""Interactive terminal tool for pruning the semantic memory store.

Arrow through every stored fact, mark the ones you want gone with space, then
confirm to delete them all at once. Invoked as `minus memory`.

Keys:
    up/down or j/k   move cursor
    space            toggle mark on the current fact
    a                mark all visible facts
    n                clear all marks
    enter            delete marked facts (asks for confirmation)
    q / esc          quit without deleting
"""

import curses

from minus.memory.facts.models import Fact
from minus.memory.facts.store import MemoryStore


def format_fact(fact: Fact) -> str:
    status = "" if fact.active else " [inactive]"
    return f"{fact.attribute} = {fact.value}{status}"


def run(stdscr, facts: list[Fact]) -> set[str]:
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # cursor row
    curses.init_pair(2, curses.COLOR_RED, -1)  # marked-for-deletion text
    curses.init_pair(3, curses.COLOR_RED, curses.COLOR_YELLOW)  # marked row under cursor

    cursor = 0
    top = 0
    marked: set[str] = set()

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        header = f"minus memory manager -- {len(facts)} facts, {len(marked)} marked for deletion"
        stdscr.addnstr(0, 0, header, width - 1, curses.A_BOLD)
        footer = "space: mark  a: mark all  n: clear  enter: delete marked  q: quit"
        stdscr.addnstr(height - 1, 0, footer, width - 1, curses.A_DIM)

        list_height = height - 2
        if cursor < top:
            top = cursor
        elif cursor >= top + list_height:
            top = cursor - list_height + 1

        for row, idx in enumerate(range(top, min(top + list_height, len(facts)))):
            fact = facts[idx]
            checkbox = "[x]" if fact.id in marked else "[ ]"
            line = f"{checkbox} {format_fact(fact)}"
            if fact.id in marked and idx == cursor:
                attr = curses.color_pair(3)
            elif fact.id in marked:
                attr = curses.color_pair(2)
            elif idx == cursor:
                attr = curses.color_pair(1)
            else:
                attr = 0
            stdscr.addnstr(row + 1, 0, line.ljust(width - 1), width - 1, attr)

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(facts) - 1, cursor + 1)
        elif key == ord(" "):
            if facts:
                fid = facts[cursor].id
                if fid in marked:
                    marked.remove(fid)
                else:
                    marked.add(fid)
        elif key == ord("a"):
            marked = {f.id for f in facts}
        elif key == ord("n"):
            marked.clear()
        elif key in (ord("q"), 27):
            return set()
        elif key in (curses.KEY_ENTER, 10, 13):
            if not marked:
                continue
            if confirm(stdscr, len(marked)):
                return marked


def confirm(stdscr, count: int) -> bool:
    height, width = stdscr.getmaxyx()
    prompt = f"Delete {count} fact(s)? This cannot be undone. [y/N] "
    stdscr.addnstr(height - 1, 0, prompt.ljust(width - 1), width - 1, curses.A_REVERSE)
    stdscr.refresh()
    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, ord("q"), 10, 13):
            return False


def run_memory_tui(db_path: str, include_inactive: bool = False) -> int:
    """Interactively prune the semantic memory store. Returns the delete count."""
    store = MemoryStore(db_path)
    try:
        facts = store.get_all_facts(only_active=not include_inactive)
        if not facts:
            print("No facts found in the memory store.")
            return 0

        to_delete = curses.wrapper(run, facts)
        if not to_delete:
            print("No facts deleted.")
            return 0

        lookup = {f.id: f for f in facts}
        for fact_id in to_delete:
            store.delete_fact(fact_id)

        print(f"Deleted {len(to_delete)} fact(s):")
        for fact_id in to_delete:
            print(f"  - {format_fact(lookup[fact_id])}")
        return len(to_delete)
    finally:
        store.close()
