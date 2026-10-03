import subprocess

print(subprocess.run(
        ["alarm", "set-alarm", "00:00:00"],
        capture_output=True,
        text=True,
        timeout=10,
    ))