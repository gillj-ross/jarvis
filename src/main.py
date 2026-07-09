"""
main.py

The actual thing you run. A simple command-line loop: type a message,
get a response back, type "exit" to quit.

Also supports one explicit command:
    remember: <something>
which saves that line straight to memory as a preference/note, instead
of relying on Jarvis to guess what's worth keeping. That's a deliberate
simplification for v1 - having Claude decide on its own what to save is
a reasonable upgrade later, but this way you always know exactly what's
in memory and why.
"""

import memory
from assistant import ask


def main():
    memory.init_db()
    print("Jarvis is online. Type 'exit' to quit.\n")

    while True:
        user_input = input("you: ").strip()

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("Jarvis: Shutting down.")
            break

        if user_input.lower().startswith("remember:"):
            note = user_input[len("remember:"):].strip()
            memory.remember("preference", note)
            print("Jarvis: Noted, sir.\n")
            continue

        response = ask(user_input)
        print(f"Jarvis: {response}\n")

        # Keep a running log of the conversation itself, separate from
        # explicit "remember:" notes - gives Jarvis some awareness of
        # what you've recently talked about even if you didn't flag it.
        memory.remember("log", f"You asked: {user_input}\nJarvis said: {response}")


if __name__ == "__main__":
    main()
