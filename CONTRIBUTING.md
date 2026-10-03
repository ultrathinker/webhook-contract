# Contributing

Thanks for looking. This plugin is small on purpose: a skill, a command, one read-only agent and one
dependency-free Python script. Please keep it that way: no runtime packages, no network access, and a script that
writes only inside the folder the user names and never deletes anything.

## Development setup

Python 3.9 or newer; Node 18 or newer if you want the tests that run the generated JavaScript harness (they skip
without Node). There is nothing to install.

    python -m unittest discover -s tests -t . -v

Use `python3` where that is the name of the interpreter. The tests build their workspaces in a temporary folder.
Use synthetic data only: no real webhook payloads, keys or addresses, in tests or in examples.

## Pull requests

- One logical change per pull request, with a test that fails without it.
- Keep `README.md`, `PRIVACY.md`, `SECURITY.md` and the skill text true: if behaviour changes, the words change in
  the same pull request.
- Keep the CLI on the standard library and working on Python 3.9; keep the JavaScript harness working on Node 18.
- Run `claude plugin validate .` if you have Claude Code installed.

## Reporting problems

Bugs and ideas: open an issue. Security problems: see `SECURITY.md` and do not open a public issue.
